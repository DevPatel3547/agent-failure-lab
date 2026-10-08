"""Durable, local experiment metadata and write-ahead operation records."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, created TEXT NOT NULL, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, spec TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, checkpoint TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(experiment_id) REFERENCES experiments(id)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                    key TEXT NOT NULL, started REAL NOT NULL, observation TEXT
                );
                CREATE TABLE IF NOT EXISTS worlds (id TEXT PRIMARY KEY, state TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self, immediate: bool = False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create_experiment(self, experiment_id: str, metadata: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO experiments VALUES (?,?,?)", (experiment_id, now(), json.dumps(metadata)))

    def experiment(self, experiment_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown experiment: {experiment_id}")
        return {"id": row["id"], "created": row["created"], **json.loads(row["metadata"])}

    def list_experiments(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row) for row in db.execute("SELECT id,created FROM experiments ORDER BY created DESC")
            ]

    def create_episode(self, episode_id: str, experiment_id: str, spec: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO episodes (id,experiment_id,spec,status) VALUES (?,?,?,'running')",
                (episode_id, experiment_id, json.dumps(spec)),
            )

    def finish_episode(self, episode_id: str, result: dict) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE episodes SET status=?,result=? WHERE id=?",
                (result["execution_status"], json.dumps(result), episode_id),
            )

    def episode(self, episode_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown episode: {episode_id}")
        return {
            "id": row["id"],
            "experiment_id": row["experiment_id"],
            "status": row["status"],
            "spec": json.loads(row["spec"]),
            "result": json.loads(row["result"]) if row["result"] else None,
            "checkpoint": json.loads(row["checkpoint"]),
        }

    def checkpoint(self, episode_id: str, value: dict) -> None:
        with self.connect() as db:
            db.execute("UPDATE episodes SET checkpoint=? WHERE id=?", (json.dumps(value), episode_id))

    def results(self, experiment_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,result,spec FROM episodes WHERE experiment_id=? ORDER BY rowid", (experiment_id,)
            ).fetchall()
        return [
            json.loads(row["result"])
            if row["result"]
            else {
                "episode_id": row["id"],
                "execution_status": "interrupted",
                "metrics": {},
                "decision": {"status": "unknown", "reason": "Runner interrupted before grading"},
                "trace": [],
                "scenario_id": json.loads(row["spec"])["scenario"]["id"],
                "scenario_name": json.loads(row["spec"])["scenario"]["name"],
                "mode": json.loads(row["spec"])["mode"],
                "trial": json.loads(row["spec"])["trial"],
            }
            for row in rows
        ]

    def claim(self, operation_id: str, fingerprint: str, key: str, clock: float) -> tuple[bool, dict]:
        """Persist intent before dispatch. Only the winner may issue an unkeyed write."""
        with self.connect(immediate=True) as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is not None:
                item = dict(row)
                if item["fingerprint"] != fingerprint:
                    raise ValueError("Operation id was reused with a different payload")
                if item["status"] != "retryable":
                    return False, item
                previous = json.loads(item["observation"]) if item["observation"] else {}
                if previous.get("retry_at", 0) > clock:
                    return False, item
                db.execute("UPDATE operations SET status='pending' WHERE id=?", (operation_id,))
                item["status"] = "pending"
                return True, item
            db.execute(
                "INSERT INTO operations VALUES (?,?, 'pending',?,?,NULL)",
                (operation_id, fingerprint, key, clock),
            )
            return True, {
                "id": operation_id,
                "fingerprint": fingerprint,
                "status": "pending",
                "key": key,
                "started": clock,
                "observation": None,
            }

    def update_operation(self, operation_id: str, status: str, observation: dict) -> None:
        with self.connect(immediate=True) as db:
            # A late uncertain response must not downgrade a concurrent confirmation.
            db.execute(
                "UPDATE operations SET status=?,observation=? WHERE id=? AND status!='confirmed'",
                (status, json.dumps(observation), operation_id),
            )

    def operation(self, operation_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        return dict(row) if row is not None else None
