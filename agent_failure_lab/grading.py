"""Grade authoritative effects, never an agent's self-assigned score."""

from __future__ import annotations


def grade(world: dict, decision: dict, operation_id: str, title: str) -> dict:
    effects = [item for item in world["effects"] if item["operation_id"] == operation_id]
    ids = [item["ticket_id"] for item in effects]
    correct = [item for item in effects if item["title"] == title]
    exactly_one = len(effects) == 1 and len(correct) == 1
    says_success = decision["status"] == "succeeded"
    identity_matches = decision.get("ticket_id") in ids
    return {
        "effects": len(effects),
        "duplicates": len(effects) > 1,
        "missing": not effects,
        "wrong_payload": any(item["title"] != title for item in effects),
        "false_success": says_success and (not exactly_one or not identity_matches),
        "false_failure": decision["status"] == "failed" and bool(effects),
        "unknown": decision["status"] == "unknown",
        "task_success": exactly_one and says_success and identity_matches,
        "exactly_one_effect": exactly_one,
        "create_calls": world["calls"],
        "final_tick": world["tick"],
    }


def summarize(results: list[dict]) -> list[dict]:
    groups = {}
    keys = (
        "duplicates",
        "missing",
        "false_success",
        "false_failure",
        "unknown",
        "wrong_payload",
        "task_success",
    )
    for result in results:
        mode = result.get("mode", "unknown")
        row = groups.setdefault(
            mode,
            {"mode": mode, "episodes": 0, "graded": 0, "errors": 0, "limits": 0, **{key: 0 for key in keys}},
        )
        row["episodes"] += 1
        status = result["execution_status"]
        row["errors"] += status in {"error", "interrupted"}
        row["limits"] += status in {"budget", "step_limit"}
        metrics = result.get("metrics", {})
        if metrics:
            row["graded"] += 1
            for key in keys:
                row[key] += bool(metrics.get(key))
    return list(groups.values())
