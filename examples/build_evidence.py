"""Rebuild public synthetic evidence. No model API calls or external writes."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import tempfile

from agent_failure_lab import __version__
from agent_failure_lab.explore import campaign, minimize
from agent_failure_lab.providers import ScriptedAgent
from agent_failure_lab.runner import run_experiment
from agent_failure_lab.schema import Scenario
from agent_failure_lab.storage import Database
from agent_failure_lab.wire import run_wire_demo


def main():
    root = Path(__file__).resolve().parents[1]
    evidence = root / "docs" / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    simulated = campaign(root / "reports" / "campaign", count=64, seed=7, max_steps=20)
    wire = run_wire_demo(root / "reports" / "wire")
    files = {}
    for name, data in (("campaign", simulated), ("wire", wire)):
        target = evidence / f"v0.2-{name}.json.gz"
        target.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
        files[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    row = max(
        (r for r in simulated["results"] if r["mode"] == "plain" and r["metrics"]["duplicates"]),
        key=lambda r: len(r["action_replay"]),
    )
    reduced = minimize(row, "duplicates", root / "reports" / "repro")
    target = evidence / "reduced-duplicate.json"
    target.write_text(json.dumps(reduced, indent=2) + "\n")
    files[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    before = json.loads((evidence / "revocation-before-fix.json").read_text())
    scenario = Scenario.from_dict(before["result"]["scenario"])
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "lab.sqlite3")
        experiment = run_experiment(db, [scenario], ScriptedAgent(), ["guarded"])
        after = db.results(experiment)[0]
    target = evidence / "revocation-after-fix.json"
    target.write_text(json.dumps({"package_version": __version__, "result": after}, indent=2) + "\n")
    files[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    (evidence / "manifest.json").write_text(
        json.dumps(
            {
                "package_version": __version__,
                "sha256": files,
                "campaign": {"cases": 64, "seed": 7, "max_steps": 20},
                "wire": {"cases": 24, "max_steps": 16},
                "live_models": False,
                "note": "Synthetic tasks; actual local HTTP faults in wire dataset. Hashes cover compressed bytes.",
            },
            indent=2,
        )
        + "\n"
    )
    # A portable public report containing synthetic data only; no database or local paths.
    (root / "docs" / "index.html").write_bytes((root / "reports" / "wire" / "index.html").read_bytes())
    print(
        json.dumps(
            {
                "campaign": simulated["summary"],
                "wire": wire["summary"],
                "reduced_actions": [reduced["original_actions"], reduced["reduced_actions"]],
                "before_false_failure": before["result"]["metrics"]["false_failure"],
                "after_false_failure": after["metrics"]["false_failure"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
