"""Small, explicit contracts shared by the runner and connector implementations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path
import re

FAULTS = {
    "none",
    "timeout_before",
    "timeout_after",
    "late_commit",
    "malformed_ack",
    "redelivery",
    "rate_limit",
    "rejected",
    "lookup_unavailable",
}


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    description: str
    fault: str = "none"
    idempotency: bool = True
    lookup: bool = True
    visibility_delay: int = 0
    commit_delay: int = 3
    fault_count: int = 1
    key_ttl: int | None = None
    crash_after_write: bool = False
    authorization_until: int | None = None
    retry_after: int = 2

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.id):
            raise ValueError("Scenario id must be a short lowercase slug")
        for key in ("name", "description"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                raise ValueError(f"Scenario {key} must be nonempty text, at most 1000 characters")
        if not isinstance(self.fault, str) or self.fault not in FAULTS:
            raise ValueError(f"Unknown fault: {self.fault}")
        for key in ("idempotency", "lookup", "crash_after_write"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f"{key} must be a boolean")
        for key in ("visibility_delay", "commit_delay", "fault_count", "retry_after"):
            value = getattr(self, key)
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError(f"{key} must be an integer from 0 to 100")
        if self.commit_delay < 1:
            raise ValueError("commit_delay must be positive")
        for key in ("key_ttl", "authorization_until"):
            value = getattr(self, key)
            if value is not None and (type(value) is not int or not 0 <= value <= 100):
                raise ValueError(f"{key} must be null or an integer from 0 to 100")
        if self.key_ttl == 0:
            raise ValueError("key_ttl must be positive when provided")

    @classmethod
    def from_dict(cls, data: dict) -> Scenario:
        if not isinstance(data, dict):
            raise ValueError("Each scenario must be an object")
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown scenario fields: {', '.join(sorted(unknown))}")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ValueError(f"Invalid scenario: {exc}") from exc

    def fingerprint(self) -> str:
        return digest(asdict(self))


def load_scenarios(path: Path | None = None) -> list[Scenario]:
    source = path or Path(__file__).parent / "fixtures" / "scenarios.json"
    if source.stat().st_size > 1_000_000:
        raise ValueError("Scenario file exceeds 1 MB")
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not 1 <= len(data) <= 1000:
        raise ValueError("Scenario file must contain 1–1000 scenarios")
    scenarios = [Scenario.from_dict(item) for item in data]
    if len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("Scenario ids must be unique")
    return scenarios


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Contract:
    supports_idempotency: bool
    supports_lookup: bool
    key_ttl: int | None = None
    lookup_consistency: str = "eventual"
    clock_unit: str = "logical_tick"

    def __post_init__(self):
        if type(self.supports_idempotency) is not bool or type(self.supports_lookup) is not bool:
            raise ValueError("Connector capabilities must be boolean")
        if self.key_ttl is not None and (type(self.key_ttl) is not int or self.key_ttl <= 0):
            raise ValueError("Key lifetime must be a positive integer or null")
        if (
            not isinstance(self.lookup_consistency, str)
            or not isinstance(self.clock_unit, str)
            or self.lookup_consistency not in {"eventual", "strong"}
            or self.clock_unit
            not in {
                "logical_tick",
                "seconds",
            }
        ):
            raise ValueError("Unsupported connector consistency or clock")


@dataclass(frozen=True)
class Observation:
    status: str
    message: str = ""
    ticket_id: str | None = None
    ticket_ids: tuple[str, ...] = ()
    retry_after: int = 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ticket_ids"] = list(self.ticket_ids)
        return data

    @classmethod
    def from_dict(cls, value: dict) -> Observation:
        if not isinstance(value, dict) or not isinstance(value.get("status"), str):
            raise ValueError("Malformed tool response")
        allowed = {f.name for f in fields(cls)}
        data = {k: v for k, v in value.items() if k in allowed}
        ids = data.get("ticket_ids", [])
        if (
            not isinstance(ids, (list, tuple))
            or not all(isinstance(v, str) for v in ids)
            or not isinstance(data.get("message", ""), str)
            or (data.get("ticket_id") is not None and not isinstance(data["ticket_id"], str))
            or type(data.get("retry_after", 0)) is not int
            or data.get("retry_after", 0) < 0
        ):
            raise ValueError("Malformed tool response fields")
        data["ticket_ids"] = tuple(ids)
        return cls(**data)


@dataclass(frozen=True)
class Action:
    name: str
    arguments: dict

    @classmethod
    def parse(cls, name: str, arguments: object) -> Action:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        if name == "create_ticket":
            if set(arguments) != {"title", "idempotency_key"}:
                raise ValueError("create_ticket requires title and idempotency_key (which may be null)")
            if not isinstance(arguments["title"], str) or not 1 <= len(arguments["title"].strip()) <= 200:
                raise ValueError("title must contain 1–200 characters")
            key = arguments["idempotency_key"]
            if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 200):
                raise ValueError("idempotency_key must be null or 1–200 characters")
        elif isinstance(name, str) and name in {"lookup_ticket", "wait"}:
            if arguments:
                raise ValueError(f"{name} takes no arguments")
        elif name == "finish":
            if set(arguments) != {"status", "ticket_id", "reason"}:
                raise ValueError("finish requires status, ticket_id and reason")
            if not isinstance(arguments["status"], str) or arguments["status"] not in {
                "succeeded",
                "failed",
                "unknown",
            }:
                raise ValueError("finish status must be succeeded, failed or unknown")
            if arguments["ticket_id"] is not None and (
                not isinstance(arguments["ticket_id"], str) or len(arguments["ticket_id"]) > 200
            ):
                raise ValueError("ticket_id must be short text or null")
            if not isinstance(arguments["reason"], str) or not 1 <= len(arguments["reason"]) <= 2000:
                raise ValueError("reason must contain 1–2000 characters")
        else:
            raise ValueError(f"Unknown tool: {str(name)[:80]}")
        return cls(name, arguments)


def tool_schemas() -> list[dict]:
    specs = [
        (
            "create_ticket",
            "Create the requested ticket. A timeout does not establish whether it was created.",
            {"title": {"type": "string"}, "idempotency_key": {"type": ["string", "null"]}},
        ),
        ("lookup_ticket", "Look up visible tickets for this operation. Empty results can be stale.", {}),
        ("wait", "Advance the public clock by one unit so in-flight operations may finish.", {}),
        (
            "finish",
            "Report the task outcome and stop. Use unknown if the outcome cannot be established.",
            {
                "status": {"type": "string", "enum": ["succeeded", "failed", "unknown"]},
                "ticket_id": {"type": ["string", "null"]},
                "reason": {"type": "string"},
            },
        ),
    ]
    return [
        {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(props),
                "additionalProperties": False,
            },
        }
        for name, description, props in specs
    ]
