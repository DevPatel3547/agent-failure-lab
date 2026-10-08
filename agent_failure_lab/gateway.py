"""A bounded recovery layer, not a universal exactly-once guarantee."""

from __future__ import annotations

import json
from typing import Callable, Protocol

from .schema import Contract, Observation, digest
from .storage import Database


class Connector(Protocol):
    contract: Contract

    def clock(self) -> float: ...
    def create(self, operation_id: str, title: str, key: str | None = None) -> Observation: ...
    def lookup(self, operation_id: str) -> Observation: ...
    def wait(self) -> Observation: ...


class WorkerRestart(Exception):
    """Controlled fault between external dispatch and local acknowledgement."""


class RecoveryGateway:
    def __init__(
        self,
        connector: Connector,
        db: Database,
        operation_id: str,
        title: str,
        after_dispatch: Callable[[], None] | None = None,
    ):
        self.connector, self.db = connector, db
        self.operation_id, self.title = operation_id, title
        self.after_dispatch = after_dispatch
        self.key = "afl-" + digest({"operation_id": operation_id, "title": title})[:40]

    def _persist(self, response: Observation, prior_uncertainty: bool = False) -> Observation:
        if prior_uncertainty and response.status in {"rejected", "rate_limited"}:
            response = Observation(
                "uncertain", "This retry did not commit, but an earlier dispatch remains unresolved."
            )
        if response.status in {"created", "found"} and len(set(response.ticket_ids)) > 1:
            response = Observation(
                "conflict",
                "Multiple committed tickets were observed; manual reconciliation is required.",
                ticket_ids=response.ticket_ids,
            )
        if response.status in {"created", "found"}:
            status = "confirmed"
        elif response.status == "rate_limited":
            status = "retryable"
        elif response.status == "rejected":
            status = "rejected"
        else:
            status = "unknown"
        observation = response.to_dict()
        if response.status == "rate_limited":
            observation["retry_at"] = self.connector.clock() + response.retry_after
        self.db.update_operation(self.operation_id, status, observation)
        return response

    def create(self, title: str) -> Observation:
        if title != self.title:
            return Observation("rejected", "Requested payload differs from the authorized task")
        clock = self.connector.clock()
        first, record = self.db.claim(self.operation_id, digest({"title": title}), self.key, clock)
        prior = json.loads(record["observation"]) if record.get("observation") else {}
        if prior.get("retry_at", 0) > clock:
            return Observation(
                "rate_limited",
                "Retry deferred until the provider backoff expires",
                retry_after=max(1, int(prior["retry_at"] - clock)),
            )
        if not first:
            if record["status"] in {"confirmed", "rejected"} and record["observation"]:
                return Observation.from_dict(json.loads(record["observation"]))
            if self.connector.contract.supports_lookup:
                observed = self.connector.lookup(self.operation_id)
                if observed.status == "found":
                    return self._persist(observed)
            if not self.connector.contract.supports_idempotency:
                return Observation(
                    "uncertain",
                    "An earlier dispatch may have committed. Unkeyed retry blocked; reconcile or escalate.",
                )
            ttl = self.connector.contract.key_ttl
            if ttl is not None and self.connector.clock() - record["started"] + 1 >= ttl:
                return Observation(
                    "uncertain", "The provider key may expire before the retry arrives; retry blocked."
                )
        key = record["key"] if self.connector.contract.supports_idempotency else None
        response = self.connector.create(self.operation_id, title, key)
        if self.after_dispatch:
            self.after_dispatch()
        return self._persist(response, prior_uncertainty=not first)

    def lookup(self) -> Observation:
        return self.connector.lookup(self.operation_id)

    def wait(self) -> Observation:
        return self.connector.wait()
