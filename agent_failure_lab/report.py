"""Portable reports: embedded data, no external scripts, no network requests."""

from __future__ import annotations

import json
from pathlib import Path

from .grading import summarize
from .privacy import redact
from .storage import Database


def bundle(db: Database, experiment_id: str) -> dict:
    results = db.results(experiment_id)
    return redact(
        {
            "schema_version": 1,
            "experiment": db.experiment(experiment_id),
            "summary": summarize(results),
            "results": results,
        }
    )


def validate_bundle(data: dict) -> None:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported report schema; expected schema_version 1")
    if not isinstance(data.get("experiment"), dict) or not isinstance(data.get("results"), list):
        raise ValueError("Report requires experiment metadata and results")
    if not all(isinstance(row, dict) and "execution_status" in row for row in data["results"]):
        raise ValueError("Invalid episode results")


def read_bundle(path: Path) -> dict:
    if path.stat().st_size > 50_000_000:
        raise ValueError("Report exceeds 50 MB")
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_bundle(data)
    return data


def write_report(data: dict, output: Path) -> Path:
    validate_bundle(data)
    output.mkdir(parents=True, exist_ok=True)
    safe = redact(data)
    (output / "results.json").write_text(json.dumps(safe, indent=2) + "\n", encoding="utf-8")
    assets = Path(__file__).parent / "assets"
    template = (assets / "report.html").read_text(encoding="utf-8")
    encoded = (
        json.dumps(safe, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    html = template.replace("__STYLE__", (assets / "report.css").read_text(encoding="utf-8"))
    html = html.replace("__SCRIPT__", (assets / "report.js").read_text(encoding="utf-8"))
    html = html.replace("__DATA__", encoded)
    target = output / "index.html"
    target.write_text(html, encoding="utf-8")
    return target


def compare(before: dict, after: dict) -> dict:
    validate_bundle(before)
    validate_bundle(after)
    # A comparison must not quietly change the task population or model.
    for field in ("fixture_hash", "provider", "model", "modes", "trials", "max_steps", "schedule_seed"):
        if before["experiment"].get(field) != after["experiment"].get(field):
            raise ValueError(f"Cannot compare reports with different {field}")

    def key(row):
        return row["scenario_id"], row["mode"], row["trial"]

    old, new = ({key(row): row for row in data["results"]} for data in (before, after))
    if len(old) != len(before["results"]) or len(new) != len(after["results"]):
        raise ValueError("Report contains duplicate episode identities")
    if len(old) != before["experiment"].get("scheduled_episodes") or len(new) != after["experiment"].get(
        "scheduled_episodes"
    ):
        raise ValueError("Cannot compare incomplete experiment schedules")
    if old.keys() != new.keys():
        raise ValueError("Cannot compare incomplete or differently selected episode sets")
    regressions = []
    for identity, row in new.items():
        previous = old[identity]
        if row["execution_status"] in {"error", "budget", "interrupted"}:
            regressions.append(
                {"case": identity, "metric": "execution_status", "after": row["execution_status"]}
            )
        for metric in ("duplicates", "missing", "false_success", "false_failure", "wrong_payload"):
            if row.get("metrics", {}).get(metric, False) and not previous.get("metrics", {}).get(
                metric, False
            ):
                regressions.append({"case": identity, "metric": metric, "before": False, "after": True})
        if previous.get("metrics", {}).get("task_success") and not row.get("metrics", {}).get("task_success"):
            regressions.append({"case": identity, "metric": "task_success", "before": True, "after": False})
    return {"comparable_episodes": len(new), "regressions": regressions, "passed": not regressions}
