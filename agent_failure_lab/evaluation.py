"""Fixed evaluation plans and durable, conservative reservations for model requests.

Prices and context limits are operator-supplied assumptions, not a billing guarantee.
No credentials or request/response bodies are stored in the request ledger.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import os
import platform
from pathlib import Path
import random
import time

from . import __version__
from .grading import summarize
from .providers import Budget, BudgetExhausted, ModelAgent
from .report import write_report
from .runner import MODES, run_episode
from .schema import Scenario, digest
from .storage import Database, now
from .transport import JSONTransport


def implementation_hash() -> str:
    root = Path(__file__).parent
    return digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))})


def decimal_value(value, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid {name}") from None
    if not result.is_finite() or not 0 < result <= 1_000_000:
        raise ValueError(f"{name} must be finite and positive, at most 1000000")
    return result


def microdollars(value) -> int:
    return int((decimal_value(value, "USD limit") * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))


def cost(plan: dict, input_tokens: int, output_tokens: int) -> int:
    pricing = plan["pricing"]
    # USD per million tokens numerically equals micro-USD per token.
    return int(
        (
            Decimal(pricing["input_usd_per_million"]) * input_tokens
            + Decimal(pricing["output_usd_per_million"]) * output_tokens
        ).to_integral_value(rounding=ROUND_CEILING)
    )


def make_plan(
    scenarios,
    provider,
    model,
    *,
    input_price,
    output_price,
    context_tokens,
    price_source,
    max_usd="10",
    max_requests=300,
    max_output_tokens=512,
    max_prompt_bytes=64000,
    trials=3,
    max_steps=12,
    seed=17,
) -> dict:
    plan = {
        "schema": "afl-evaluation-plan-v1",
        "created": now(),
        "version": __version__,
        "implementation_hash": implementation_hash(),
        "provider": provider,
        "model": model,
        "scenarios": [asdict(s) for s in scenarios],
        "modes": list(MODES),
        "trials": trials,
        "max_steps": max_steps,
        "seed": seed,
        "limits": {
            "max_requests": max_requests,
            "max_output_tokens": max_output_tokens,
            "max_prompt_bytes": max_prompt_bytes,
            "max_usd": str(max_usd),
        },
        "pricing": {
            "input_usd_per_million": str(input_price),
            "output_usd_per_million": str(output_price),
            "context_tokens": context_tokens,
            "source": price_source,
        },
        "primary_outcome": "task_success",
        "secondary_outcomes": ["duplicates", "false_success", "false_failure", "unknown", "missing"],
        "scope": "Selected synthetic tool failures. Live model decisions; simulated ticket service. "
        "No population-level or production reliability claim.",
    }
    plan["plan_hash"] = digest(plan)
    validate_plan(plan)
    return plan


def validate_plan(plan: dict, *, check_code=True) -> None:
    if not isinstance(plan, dict) or plan.get("schema") != "afl-evaluation-plan-v1":
        raise ValueError("Unsupported evaluation plan")
    if plan.get("plan_hash") != digest({k: v for k, v in plan.items() if k != "plan_hash"}):
        raise ValueError("Evaluation plan hash mismatch")
    if check_code and plan.get("implementation_hash") != implementation_hash():
        raise ValueError("Implementation changed since the plan was frozen; create a new plan")
    if plan.get("provider") not in {"openai", "anthropic"}:
        raise ValueError("Evaluation requires an explicit live provider")
    if not isinstance(plan.get("model"), str) or not 1 <= len(plan["model"]) <= 200:
        raise ValueError("Evaluation requires an explicit model id")
    if plan.get("modes") != list(MODES) or plan.get("primary_outcome") != "task_success":
        raise ValueError("Plan must compare plain, guided and guarded with task_success primary")
    scenarios = plan.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 100:
        raise ValueError("Select 1 to 100 scenarios")
    parsed = [Scenario.from_dict(s) for s in scenarios]
    if len({s.id for s in parsed}) != len(parsed):
        raise ValueError("Duplicate scenario ids")
    limits, pricing = plan["limits"], plan["pricing"]
    for value, maximum in (
        (plan["trials"], 100),
        (plan["max_steps"], 200),
        (limits["max_requests"], 100000),
        (limits["max_output_tokens"], 100000),
        (limits["max_prompt_bytes"], 1000000),
        (pricing["context_tokens"], 10000000),
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("Plan contains invalid integer bounds")
    if type(plan["seed"]) is not int or len(parsed) * plan["trials"] * 3 > 3000:
        raise ValueError("Invalid seed or more than 3000 scheduled episodes")
    for key in ("input_usd_per_million", "output_usd_per_million"):
        decimal_value(pricing[key], key)
    if not isinstance(pricing.get("source"), str) or not pricing["source"].startswith("https://"):
        raise ValueError("Record an HTTPS pricing/context source in the plan")
    if microdollars(limits["max_usd"]) < cost(plan, pricing["context_tokens"], limits["max_output_tokens"]):
        raise ValueError("USD limit cannot cover one conservative full-context request reservation")


def read_plan(path: Path) -> dict:
    if path.stat().st_size > 2_000_000:
        raise ValueError("Plan exceeds 2 MB")
    plan = json.loads(path.read_text())
    validate_plan(plan)
    return plan


def schedule(plan: dict) -> list[tuple[Scenario, str, int]]:
    rows = [
        (Scenario.from_dict(s), mode, trial)
        for s in plan["scenarios"]
        for mode in plan["modes"]
        for trial in range(plan["trials"])
    ]
    random.Random(plan["seed"]).shuffle(rows)
    return rows


def preflight(plan: dict) -> dict:
    validate_plan(plan)
    per_request = cost(plan, plan["pricing"]["context_tokens"], plan["limits"]["max_output_tokens"])
    return {
        "plan_hash": plan["plan_hash"],
        "scheduled_episodes": len(schedule(plan)),
        "max_decisions_if_all_steps_used": len(schedule(plan)) * plan["max_steps"],
        "limits": plan["limits"],
        "pricing_assumptions": plan["pricing"],
        "reservation_usd_per_request": per_request / 1_000_000,
        "network_calls": 0,
        "warning": "Costs are conditional on the supplied rates and full model context bound. "
        "Verify current standard-tier prices (including long-context premiums) before running. "
        "Missing usage retains the full reservation. No built-in paid tools are enabled.",
    }


@contextmanager
def exclusive_run(path: Path):
    """OS lock: released after a crash; never steal a possibly active runner's lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("An evaluation is already active for this database") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class LedgerTransport:
    """Reserve atomically BEFORE network dispatch; retain uncertain charges across restarts."""

    def __init__(self, db: Database, plan: dict, transport=None):
        self.db, self.plan = db, plan
        self.transport = transport or JSONTransport()
        self.episode_id = None
        with db.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS evaluation_budget (
                    id INTEGER PRIMARY KEY CHECK(id=1), plan_hash TEXT NOT NULL, halted INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_requests (
                    id INTEGER PRIMARY KEY, episode_id TEXT, created TEXT NOT NULL,
                    request_hash TEXT NOT NULL, status TEXT NOT NULL, charged_micro_usd INTEGER NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, elapsed_seconds REAL,
                    response_hash TEXT
                );
            """)
        with db.connect(immediate=True) as con:
            con.execute("INSERT OR IGNORE INTO evaluation_budget VALUES (1,?,0)", (plan["plan_hash"],))
            if con.execute("SELECT plan_hash FROM evaluation_budget").fetchone()[0] != plan["plan_hash"]:
                raise ValueError("This database belongs to a different evaluation plan")

    def request(self, method, url, payload=None, headers=None):
        reserved = cost(
            self.plan, self.plan["pricing"]["context_tokens"], self.plan["limits"]["max_output_tokens"]
        )
        with self.db.connect(immediate=True) as con:
            count, charged = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(charged_micro_usd),0) FROM evaluation_requests"
            ).fetchone()
            if con.execute("SELECT halted FROM evaluation_budget").fetchone()[0]:
                raise BudgetExhausted("Reported usage exceeded plan assumptions; evaluation halted")
            if count >= self.plan["limits"]["max_requests"]:
                raise BudgetExhausted("Persistent model-request limit reached")
            if charged + reserved > microdollars(self.plan["limits"]["max_usd"]):
                raise BudgetExhausted("Remaining budget cannot cover a full-context reservation")
            request_id = con.execute(
                "INSERT INTO evaluation_requests (episode_id,created,request_hash,status,charged_micro_usd) "
                "VALUES (?,?,?,'reserved',?)",
                (self.episode_id, now(), digest(payload), reserved),
            ).lastrowid
        started = time.monotonic()
        # Any error, cancellation or process death retains the committed reservation.
        response = self.transport.request(method, url, payload, headers)
        usage = response.get("usage") if isinstance(response, dict) else None
        valid = isinstance(usage, dict) and all(
            type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens")
        )
        input_tokens = usage["input_tokens"] if valid else None
        output_tokens = usage["output_tokens"] if valid else None
        # Anthropic cache counters are separate; no discount is assumed.
        if valid:
            for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                value = usage.get(key, 0)
                if type(value) is not int or value < 0:
                    valid = False
                    break
                input_tokens += value
        exceeds = valid and (
            input_tokens > self.plan["pricing"]["context_tokens"]
            or output_tokens > self.plan["limits"]["max_output_tokens"]
        )
        charged = cost(self.plan, input_tokens, output_tokens) if valid else reserved
        with self.db.connect(immediate=True) as con:
            con.execute(
                "UPDATE evaluation_requests SET status=?,charged_micro_usd=?,input_tokens=?, "
                "output_tokens=?,elapsed_seconds=?,response_hash=? WHERE id=?",
                (
                    "assumption_violation" if exceeds else "reported_usage" if valid else "missing_usage",
                    max(charged, reserved) if exceeds else charged,
                    input_tokens if valid else None,
                    output_tokens if valid else None,
                    time.monotonic() - started,
                    digest(response),
                    request_id,
                ),
            )
            if exceeds:
                con.execute("UPDATE evaluation_budget SET halted=1")
        if exceeds:
            raise BudgetExhausted("Reported usage exceeded plan assumptions; evaluation halted")
        return response

    def summary(self) -> dict:
        with self.db.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM evaluation_requests ORDER BY id")]
            halted = bool(con.execute("SELECT halted FROM evaluation_budget").fetchone()[0])
        return {
            "requests": len(rows),
            "charged_or_reserved_usd": sum(r["charged_micro_usd"] for r in rows) / 1e6,
            "unresolved_reservations": sum(r["status"] == "reserved" for r in rows),
            "missing_usage": sum(r["status"] == "missing_usage" for r in rows),
            "halted": halted,
            "note": "Conservative ledger under plan pricing assumptions; not a provider invoice.",
            "records": rows,
        }


def paired_outcomes(results: list[dict], plan: dict) -> dict:
    rows = {(r["scenario_id"], r["trial"], r["mode"]): r for r in results}
    pairs = {}
    for treatment in ("guided", "guarded"):
        counts = {
            "planned_pairs": len(plan["scenarios"]) * plan["trials"],
            "complete_pairs": 0,
            "both_succeeded": 0,
            "plain_only_succeeded": 0,
            "treatment_only_succeeded": 0,
            "neither_succeeded": 0,
        }
        for scenario in plan["scenarios"]:
            for trial in range(plan["trials"]):
                a, b = (rows.get((scenario["id"], trial, m)) for m in ("plain", treatment))
                if (
                    not a
                    or not b
                    or any(r["execution_status"] not in {"completed", "step_limit"} for r in (a, b))
                ):
                    continue
                counts["complete_pairs"] += 1
                av, bv = bool(a["metrics"]["task_success"]), bool(b["metrics"]["task_success"])
                key = (
                    "both_succeeded"
                    if av and bv
                    else "plain_only_succeeded"
                    if av
                    else "treatment_only_succeeded"
                    if bv
                    else "neither_succeeded"
                )
                counts[key] += 1
        counts["excluded_or_missing_pairs"] = counts["planned_pairs"] - counts["complete_pairs"]
        counts["success_difference_on_complete_pairs"] = (
            (counts["treatment_only_succeeded"] - counts["plain_only_succeeded"]) / counts["complete_pairs"]
            if counts["complete_pairs"]
            else None
        )
        pairs[treatment] = counts
    return {
        "comparisons_to_plain": pairs,
        "interpretation": "Descriptive paired counts on selected fixtures, not population inference. "
        "Missing/error/budget pairs are explicitly excluded; step limits count as outcomes. "
        "Trials share scenarios and must not be treated as independent tasks.",
    }


def evaluate(plan: dict, db_path: Path, output: Path, *, transport=None, api_key=None) -> dict:
    validate_plan(plan)
    db_path = db_path.resolve()
    with exclusive_run(db_path.with_suffix(".evaluation.lock")):
        db = Database(db_path)
        ledger = LedgerTransport(db, plan, transport)
        experiment_id = "eval-" + plan["plan_hash"][:20]
        try:
            db.experiment(experiment_id)
        except ValueError:
            db.create_experiment(
                experiment_id,
                {
                    "version": __version__,
                    "provider": plan["provider"],
                    "model": plan["model"],
                    "modes": plan["modes"],
                    "trials": plan["trials"],
                    "max_steps": plan["max_steps"],
                    "fixture_hash": digest(plan["scenarios"]),
                    "schedule_seed": plan["seed"],
                    "scheduled_episodes": len(schedule(plan)),
                    "environment": "simulated-ticket-service",
                    "created": now(),
                    "plan": plan,
                    "runtime": {"python": platform.python_version(), "system": platform.system()},
                    "validation_kind": "live-provider" if transport is None else "injected-transport-test",
                    "note": plan["scope"],
                },
            )
        validation_kind = db.experiment(experiment_id).get("validation_kind")
        if validation_kind != ("live-provider" if transport is None else "injected-transport-test"):
            raise ValueError("Cannot mix injected test responses and live responses in one evaluation")
        limits = plan["limits"]
        agent = ModelAgent(
            plan["provider"],
            plan["model"],
            Budget(limits["max_requests"], limits["max_output_tokens"], limits["max_prompt_bytes"]),
            ledger,
            api_key,
        )

        def export():
            results = db.results(experiment_id)
            statuses = {}
            for row in results:
                statuses[row["execution_status"]] = statuses.get(row["execution_status"], 0) + 1
            planned = len(schedule(plan))
            data = {
                "schema_version": 1,
                "experiment": db.experiment(experiment_id),
                "results": results,
                "summary": summarize(results),
                "request_ledger": ledger.summary(),
                "coverage": {
                    "planned": planned,
                    "recorded": len(results),
                    "not_started": planned - len(results),
                    "by_status": statuses,
                    "complete": len(results) == planned
                    and not any(statuses.get(s, 0) for s in ("interrupted", "budget", "error")),
                },
                "paired_outcomes": paired_outcomes(results, plan),
            }
            write_report(data, output)
            (output / "evaluation.json").write_text(
                json.dumps({k: data[k] for k in ("coverage", "paired_outcomes", "request_ledger")}, indent=2)
                + "\n"
            )
            return data

        try:
            for index, (scenario, mode, trial) in enumerate(schedule(plan)):
                episode_id = f"{experiment_id}-{index:05d}"
                try:
                    existing = db.episode(episode_id)
                except ValueError:
                    existing = None
                if existing and existing["result"] is not None:
                    if existing["result"]["execution_status"] == "budget":
                        break
                    continue  # Completed and errored episodes remain in the denominator; never cherry-pick retries.
                ledger.episode_id = episode_id
                result = run_episode(
                    db,
                    experiment_id,
                    scenario,
                    mode,
                    agent,
                    plan["max_steps"],
                    trial,
                    episode_id,
                    resume=existing is not None,
                )
                export()
                if result["execution_status"] in {"error", "budget"}:
                    break
        finally:
            data = export()
        return data
