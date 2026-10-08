"""Seeded fault exploration and bounded delta debugging of public action traces."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import random
import tempfile
import uuid

from .providers import ScriptedAgent
from .report import bundle, write_report
from .runner import ReplayAgent, run_episode, run_experiment
from .schema import Action, Scenario, digest
from .storage import Database
from . import __version__

FAILURE_METRICS = ("duplicates", "false_success", "false_failure", "wrong_payload")


def generated_scenarios(count: int, seed: int) -> list[Scenario]:
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("Campaign size must be 1–1000")
    randomizer = random.Random(seed)
    faults = (
        "none",
        "timeout_before",
        "timeout_after",
        "late_commit",
        "malformed_ack",
        "redelivery",
        "rate_limit",
        "lookup_unavailable",
    )
    scenarios = []
    for index in range(count):
        fault = randomizer.choice(faults)
        keys = randomizer.choice((True, False))
        values = {
            "fault": fault,
            "idempotency": keys,
            "lookup": randomizer.choice((True, False)),
            "visibility_delay": randomizer.choice((0, 1, 3, 8)),
            "commit_delay": randomizer.choice((1, 2, 5, 10)),
            "fault_count": randomizer.choice((1, 2, 3)),
            "key_ttl": randomizer.choice((None, None, 2, 5)) if keys else None,
            "crash_after_write": randomizer.random() < 0.15,
            "authorization_until": randomizer.choice((None, None, None, 0, 1, 4, 12)),
            "retry_after": randomizer.choice((1, 2, 4)),
        }
        scenarios.append(
            Scenario(
                f"generated-{index:04}",
                f"Generated case {index + 1} · {fault}",
                "A seeded synthetic combination of fault, visibility, authorization, restart, and key-lifetime conditions.",
                **values,
            )
        )
    return scenarios


def campaign(output: Path, count: int = 64, seed: int = 7, max_steps: int = 20) -> dict:
    db = Database(output / "private-runs" / (uuid.uuid4().hex + ".sqlite3"))
    scenarios = generated_scenarios(count, seed)
    experiment = run_experiment(
        db, scenarios, ScriptedAgent(), ("plain", "reference", "guarded"), max_steps=max_steps, seed=seed
    )
    data = bundle(db, experiment)
    data["experiment"]["campaign"] = {
        "generator": "independent-combinations-v1",
        "count": count,
        "seed": seed,
        "interpretation": "Coverage exploration, not representative frequencies or statistical evidence.",
    }
    write_report(data, output)
    (output / "scenarios.json").write_text(json.dumps([asdict(s) for s in scenarios], indent=2) + "\n")
    return data


def replay_case(scenario: dict, actions: list[dict], mode: str) -> dict:
    if not isinstance(actions, list) or not 1 <= len(actions) <= 200:
        raise ValueError("A reproduction needs 1–200 actions")
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"name", "arguments"}:
            raise ValueError("Invalid recorded action")
        Action.parse(action["name"], action["arguments"])
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "repro.sqlite3")
        db.create_experiment("repro", {"version": __version__, "provider": "replay"})
        return run_episode(
            db,
            "repro",
            Scenario.from_dict(scenario),
            mode,
            ReplayAgent(actions),
            max_steps=len(actions),
            episode_id="repro",
        )


def reproduce(data: dict) -> dict:
    if (
        not isinstance(data, dict)
        or data.get("repro_schema") != 1
        or not isinstance(data.get("metric"), str)
        or data.get("metric") not in FAILURE_METRICS
    ):
        raise ValueError("Unsupported reproduction artifact")
    case = {k: data[k] for k in ("scenario", "actions", "mode", "metric")}
    if digest(case) != data.get("case_hash"):
        raise ValueError("Reproduction content does not match its hash")
    result = replay_case(case["scenario"], case["actions"], case["mode"])
    return {
        "failure_observed": bool(result["metrics"][case["metric"]]),
        "metric": case["metric"],
        "result": result,
    }


def minimize(row: dict, metric: str, output: Path, max_evaluations: int = 128) -> dict:
    if metric not in FAILURE_METRICS or not 2 <= max_evaluations <= 1000:
        raise ValueError("Invalid failure metric or evaluation budget")
    if not row.get("metrics", {}).get(metric):
        raise ValueError("The selected episode does not violate this invariant")
    if "scenario" not in row or "action_replay" not in row:
        raise ValueError("Only simulated episodes with portable action traces can be reduced")
    original = list(row["action_replay"])
    if not original or len(original) > 200:
        raise ValueError("Action trace must contain 1–200 actions")
    scenario, mode = row["scenario"], row["mode"]
    if Scenario.from_dict(scenario).fingerprint() != row.get("scenario_hash"):
        raise ValueError("Scenario fingerprint does not match the recorded episode")
    evaluations, cache = 0, {}
    # Retain the broad failure mechanism while reducing; a duplicate-based false
    # success must not turn into a zero-write false success during shrinking.
    required = {name: bool(row["metrics"][name]) for name in ("duplicates", "wrong_payload")}

    def test(actions):
        nonlocal evaluations
        if not actions:
            return False
        fingerprint = digest(actions)
        if fingerprint in cache:
            return cache[fingerprint][0]
        if evaluations >= max_evaluations:
            return False
        evaluations += 1
        result = replay_case(scenario, actions, mode)
        metrics = result["metrics"]
        matches = bool(metrics[metric]) and all(bool(metrics[k]) == value for k, value in required.items())
        cache[fingerprint] = (matches, result)
        return matches

    if not test(original):
        raise ValueError("The current implementation no longer reproduces this recorded failure")
    reduced = original[:]
    granularity = 2
    while len(reduced) >= 2 and evaluations < max_evaluations:
        chunk = (len(reduced) + granularity - 1) // granularity
        changed = False
        for start in range(0, len(reduced), chunk):
            candidate = reduced[:start] + reduced[start + chunk :]
            if test(candidate):
                reduced, granularity, changed = candidate, max(2, granularity - 1), True
                break
        if not changed:
            if granularity >= len(reduced):
                break
            granularity = min(len(reduced), granularity * 2)
    # A successful complete deletion sweep establishes only 1-minimality.
    minimal = True
    for index in range(len(reduced)):
        candidate = reduced[:index] + reduced[index + 1 :]
        if candidate and digest(candidate) not in cache and evaluations >= max_evaluations:
            minimal = False
            break
        if test(candidate):
            minimal = False
            break
    result = cache[digest(reduced)][1]
    case = {"scenario": scenario, "actions": reduced, "mode": mode, "metric": metric}
    artifact = {
        "repro_schema": 1,
        **case,
        "case_hash": digest(case),
        "package_version": __version__,
        "source_episode": row["episode_id"],
        "source_scenario_hash": row["scenario_hash"],
        "original_actions": len(original),
        "reduced_actions": len(reduced),
        "evaluations": evaluations,
        "minimality": "1-minimal by action deletion"
        if minimal
        else "budget-limited reduction; minimality unverified",
        "scope": "Scenario parameters and recorded decisions are fixed. No global minimality or model-behavior claim.",
        "metrics": result["metrics"],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "repro.json").write_text(json.dumps(artifact, indent=2) + "\n")
    (
        output / "test_regression.py"
    ).write_text('''"""Intentionally fails until this invariant violation is fixed. Run beside repro.json."""
import json
from pathlib import Path
import unittest
from agent_failure_lab.explore import reproduce

class Regression(unittest.TestCase):
    def test_no_recorded_invariant_violation(self):
        artifact = json.loads(Path(__file__).with_name("repro.json").read_text())
        outcome = reproduce(artifact)
        self.assertFalse(outcome["failure_observed"], outcome["result"]["metrics"])

if __name__ == "__main__":
    unittest.main()
''')
    return artifact
