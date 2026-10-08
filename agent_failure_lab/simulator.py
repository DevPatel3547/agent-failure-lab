"""Persistent mock service. World state is graded independently of agent reports."""

from __future__ import annotations

from dataclasses import asdict
import json

from .schema import Contract, Observation, Scenario, digest
from .storage import Database


class SimulatedConnector:
    def __init__(self, db: Database, namespace: str, scenario: Scenario):
        self.db, self.namespace, self.scenario = db, namespace, scenario
        self.contract = Contract(scenario.idempotency, scenario.lookup, scenario.key_ttl)
        initial = {
            "scenario_hash": scenario.fingerprint(),
            "tick": 0,
            "calls": 0,
            "effects": [],
            "pending": [],
            "keys": {},
            "events": [],
            "blocked_until": 0,
        }
        with db.connect(immediate=True) as conn:
            conn.execute("INSERT OR IGNORE INTO worlds VALUES (?,?)", (namespace, json.dumps(initial)))
            state = json.loads(
                conn.execute("SELECT state FROM worlds WHERE id=?", (namespace,)).fetchone()[0]
            )
            if state["scenario_hash"] != scenario.fingerprint():
                raise ValueError("World namespace already belongs to a different scenario")

    @staticmethod
    def _event(state: dict, scope: str, event: str, **details) -> None:
        state["events"].append(
            {
                "sequence": len(state["events"]),
                "tick": state["tick"],
                "scope": scope,
                "event": event,
                "details": details,
            }
        )

    def _transaction(self, operation):
        with self.db.connect(immediate=True) as conn:
            state = json.loads(
                conn.execute("SELECT state FROM worlds WHERE id=?", (self.namespace,)).fetchone()[0]
            )
            result = operation(state)
            conn.execute("UPDATE worlds SET state=? WHERE id=?", (json.dumps(state), self.namespace))
            return result

    def clock(self) -> float:
        with self.db.connect() as conn:
            return json.loads(
                conn.execute("SELECT state FROM worlds WHERE id=?", (self.namespace,)).fetchone()[0]
            )["tick"]

    def _authorized(self, state: dict) -> bool:
        expiry = self.scenario.authorization_until
        return expiry is None or state["tick"] <= expiry

    def _commit(self, state: dict, operation: str, title: str, key: str | None) -> Observation:
        if not self._authorized(state):
            result = Observation("rejected", "Authorization expired before the provider committed the action")
            self._event(state, "world", "commit_rejected", operation_id=operation)
        else:
            ticket_id = f"T-{len(state['effects']) + 1}"
            state["effects"].append(
                {
                    "ticket_id": ticket_id,
                    "operation_id": operation,
                    "title": title,
                    "committed_at": state["tick"],
                    "visible_at": state["tick"] + self.scenario.visibility_delay,
                }
            )
            result = Observation("created", "Provider acknowledged ticket creation", ticket_id)
            self._event(state, "world", "ticket_committed", ticket_id=ticket_id, title=title)
        if key is not None and key in state["keys"]:
            state["keys"][key]["result"] = result.to_dict()
        return result

    def _advance(self, state: dict) -> None:
        state["tick"] += 1
        due = [item for item in state["pending"] if item["due"] <= state["tick"]]
        state["pending"] = [item for item in state["pending"] if item["due"] > state["tick"]]
        for item in due:
            self._commit(state, item["operation"], item["title"], item["key"])

    def create(self, operation_id: str, title: str, key: str | None = None) -> Observation:
        def apply(state):
            self._advance(state)
            state["calls"] += 1
            self._event(state, "tool", "create_request", operation_id=operation_id, title=title, key=key)
            if key is not None and not self.contract.supports_idempotency:
                return Observation("rejected", "Idempotency keys are not supported")
            if not self._authorized(state):
                return Observation("rejected", "Authorization has expired; no new write accepted")
            fingerprint = digest({"operation": operation_id, "title": title})
            if key is not None and key in state["keys"]:
                entry = state["keys"][key]
                if entry["expires"] is None or state["tick"] < entry["expires"]:
                    if entry["fingerprint"] != fingerprint:
                        return Observation("rejected", "Idempotency key payload conflict")
                    self._event(state, "tool", "idempotency_replay", key=key)
                    return Observation.from_dict(entry["result"])
                del state["keys"][key]
                self._event(state, "world", "idempotency_key_expired", key=key)
            if state["tick"] < state["blocked_until"]:
                return Observation(
                    "rate_limited",
                    "No write accepted; respect retry_after",
                    retry_after=state["blocked_until"] - state["tick"],
                )
            fault = self.scenario.fault if state["calls"] <= self.scenario.fault_count else "none"
            if fault == "timeout_before":
                return Observation("uncertain", "Request timed out; commit status is unknown")
            if fault == "rate_limit":
                state["blocked_until"] = state["tick"] + self.scenario.retry_after
                return Observation(
                    "rate_limited",
                    "No write accepted; respect retry_after",
                    retry_after=self.scenario.retry_after,
                )
            if fault == "rejected":
                return Observation("rejected", "Provider rejected the request before any write")
            if key is not None:
                state["keys"][key] = {
                    "fingerprint": fingerprint,
                    "expires": state["tick"] + self.scenario.key_ttl if self.scenario.key_ttl else None,
                    "result": Observation("pending", "Operation is still processing").to_dict(),
                }
            if fault == "late_commit":
                state["pending"].append(
                    {
                        "due": state["tick"] + self.scenario.commit_delay,
                        "operation": operation_id,
                        "title": title,
                        "key": key,
                    }
                )
                self._event(state, "world", "write_in_flight", due=state["tick"] + self.scenario.commit_delay)
                return Observation("uncertain", "Request timed out; commit status is unknown")
            response = self._commit(state, operation_id, title, key)
            if fault == "redelivery" and key is None:
                self._commit(state, operation_id, title, key)
            if fault in {"timeout_after", "lookup_unavailable"}:
                return Observation("uncertain", "Request timed out; commit status is unknown")
            if fault == "malformed_ack":
                return Observation("uncertain", "Response could not be parsed; commit status is unknown")
            return response

        result = self._transaction(apply)
        self.record("tool", "create_response", **result.to_dict())
        return result

    def lookup(self, operation_id: str) -> Observation:
        def apply(state):
            self._advance(state)
            if not self.contract.supports_lookup:
                return Observation("unavailable", "Provider offers no lookup")
            if self.scenario.fault == "lookup_unavailable":
                return Observation("unavailable", "Lookup service is unavailable")
            ids = tuple(
                item["ticket_id"]
                for item in state["effects"]
                if item["operation_id"] == operation_id and item["visible_at"] <= state["tick"]
            )
            if ids:
                return Observation("found", "Visible tickets for this operation", ids[0], ids)
            return Observation(
                "not_found", "No ticket is currently visible; an earlier write may still appear"
            )

        result = self._transaction(apply)
        self.record("tool", "lookup_response", **result.to_dict())
        return result

    def wait(self) -> Observation:
        self._transaction(self._advance)
        result = Observation("waited", "One clock unit elapsed")
        self.record("tool", "wait_response", **result.to_dict())
        return result

    def record(self, scope: str, event: str, **details) -> None:
        self._transaction(lambda state: self._event(state, scope, event, **details))

    def settle(self) -> None:
        def apply(state):
            for _ in range(201):
                if not state["pending"]:
                    return
                self._advance(state)
            raise RuntimeError("Pending actions did not settle within the bounded scenario horizon")

        self._transaction(apply)

    def snapshot_for_grader(self) -> dict:
        with self.db.connect() as conn:
            return json.loads(
                conn.execute("SELECT state FROM worlds WHERE id=?", (self.namespace,)).fetchone()[0]
            )

    def public_contract(self) -> dict:
        return asdict(self.contract)
