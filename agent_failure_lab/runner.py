"""Bounded experiments, durable checkpoints, and reproducible action replay."""

from __future__ import annotations

from dataclasses import asdict
import random
import time
import uuid

from . import __version__
from .gateway import RecoveryGateway, WorkerRestart
from .grading import grade
from .privacy import redact
from .providers import BudgetExhausted, ProtocolError
from .policies import ReferenceAgent
from .schema import Action, Scenario, digest
from .simulator import SimulatedConnector
from .storage import Database, now
from .transport import TransportError

TITLE = "Investigate dropped checkout events"
MODES = ("plain", "guided", "guarded")
ALL_MODES = (*MODES, "reference")


def run_episode(
    db: Database,
    experiment_id: str,
    scenario: Scenario,
    mode: str,
    agent,
    max_steps: int = 16,
    trial: int = 0,
    episode_id: str | None = None,
    resume: bool = False,
) -> dict:
    if mode not in ALL_MODES or not 1 <= max_steps <= 200:
        raise ValueError("Invalid mode or step limit")
    if mode == "reference" and not isinstance(agent, ReplayAgent):
        agent = ReferenceAgent()
    episode_id = episode_id or uuid.uuid4().hex
    spec = {"scenario": asdict(scenario), "mode": mode, "max_steps": max_steps, "trial": trial}
    if not resume:
        db.create_episode(episode_id, experiment_id, spec)
    checkpoint = db.episode(episode_id)["checkpoint"]
    history = checkpoint.get("history", [])
    step = checkpoint.get("next_step", 0)
    restart_injected = checkpoint.get("restart_injected", False)
    connector = SimulatedConnector(db, episode_id, scenario)
    operation_id = f"operation-{episode_id}"
    decision = {"status": "unknown", "ticket_id": None, "reason": "Step limit reached"}
    status = "step_limit"
    started = time.monotonic()
    protocol_errors = checkpoint.get("protocol_errors", 0)
    budget = getattr(agent, "budget", None)
    usage_before = asdict(budget) if budget else {}

    def save(pending=None):
        db.checkpoint(
            episode_id,
            {
                "history": history,
                "next_step": step,
                "pending": pending,
                "restart_injected": restart_injected,
                "protocol_errors": protocol_errors,
            },
        )

    if resume and checkpoint.get("pending"):
        history.append(
            {
                "action": checkpoint["pending"],
                "observation": {
                    "status": "worker_restarted",
                    "message": "The worker restarted without a saved result. An earlier write may have committed.",
                },
            }
        )
        connector.record("control", "worker_resumed", next_step=step)
        save()

    def after_dispatch():
        nonlocal restart_injected
        if scenario.crash_after_write and not restart_injected:
            restart_injected = True
            save()
            raise WorkerRestart()

    gateway = RecoveryGateway(connector, db, operation_id, TITLE, after_dispatch)
    while step < max_steps:
        context = {
            "task": {"operation_id": operation_id, "title": TITLE},
            "contract": connector.public_contract(),
            "clock": connector.clock(),
            "remaining_steps": max_steps - step,
            "history": history,
        }
        step += 1
        try:
            action = agent.decide(context, mode)
            action = Action.parse(action.name, action.arguments)
        except (ProtocolError, ValueError) as exc:
            protocol_errors += 1
            message = redact(str(exc))[:1000]
            history.append({"observation": {"status": "invalid_action", "message": message}})
            connector.record("control", "invalid_model_action", message=message)
            save()
            continue
        except BudgetExhausted as exc:
            decision["reason"], status = str(exc), "budget"
            save()
            break
        except TransportError as exc:
            decision["reason"], status = str(exc), "error"
            connector.record("control", "model_transport_error", message=str(exc))
            save()
            break
        serialized = {"name": action.name, "arguments": action.arguments}
        connector.record("agent", "action", **redact(serialized))
        save(pending=serialized)
        if action.name == "finish":
            decision, status = action.arguments, "completed"
            save()
            break
        try:
            if action.name == "create_ticket":
                if mode == "guarded":
                    observation = gateway.create(action.arguments["title"])
                else:
                    observation = connector.create(
                        operation_id, action.arguments["title"], action.arguments["idempotency_key"]
                    )
                    after_dispatch()
            elif action.name == "lookup_ticket":
                observation = connector.lookup(operation_id)
            else:
                observation = connector.wait()
            result = observation.to_dict()
        except WorkerRestart:
            result = {
                "status": "worker_restarted",
                "message": "Worker restarted before its result was saved. An earlier write may have committed.",
            }
            connector.record("control", "injected_worker_restart")
            # Reopen both views; recovery must use SQLite rather than an in-memory object.
            connector = SimulatedConnector(Database(db.path), episode_id, scenario)
            gateway = RecoveryGateway(connector, Database(db.path), operation_id, TITLE, after_dispatch)
        history.append({"action": serialized, "observation": result})
        save()
    connector.record("agent", "final_report", **redact(decision))
    connector.settle()
    world = connector.snapshot_for_grader()
    metrics = grade(world, decision, operation_id, TITLE)
    connector.record("grader", "effects_checked", **metrics)
    world = connector.snapshot_for_grader()
    usage = (
        {
            key: getattr(budget, key) - usage_before[key]
            for key in ("requests", "input_tokens", "output_tokens", "usage_missing")
        }
        if budget
        else {}
    )
    result = redact(
        {
            "episode_id": episode_id,
            "experiment_id": experiment_id,
            "scenario_id": scenario.id,
            "scenario_name": scenario.name,
            "description": scenario.description,
            "scenario_hash": scenario.fingerprint(),
            "scenario": asdict(scenario),
            "max_steps": max_steps,
            "mode": mode,
            "trial": trial,
            "provider": getattr(agent, "provider", "scripted"),
            "model": getattr(agent, "model", None),
            "execution_status": status,
            "decision": decision,
            "metrics": metrics,
            "usage": usage,
            "protocol_errors": protocol_errors,
            "steps": step,
            "duration_seconds": round(time.monotonic() - started, 4),
            "trace": world["events"],
            "effects": world["effects"],
            "visible_history": history,
            "action_replay": [item["action"] for item in history if "action" in item]
            + ([{"name": "finish", "arguments": decision}] if status == "completed" else []),
        }
    )
    db.finish_episode(episode_id, result)
    return result


def run_experiment(
    db: Database,
    scenarios: list[Scenario],
    agent,
    modes=MODES,
    trials: int = 1,
    max_steps: int = 16,
    seed: int = 0,
    progress=None,
) -> str:
    if (
        not scenarios
        or not 1 <= trials <= 100
        or not 1 <= max_steps <= 200
        or not modes
        or len(set(modes)) != len(modes)
        or any(mode not in ALL_MODES for mode in modes)
    ):
        raise ValueError("Invalid experiment selection")
    if "reference" in modes and getattr(agent, "provider", "scripted") != "scripted":
        raise ValueError("The reference baseline is scripted; run it separately from live model treatments")
    experiment_id = uuid.uuid4().hex[:12]
    schedule = [
        (scenario, mode, trial) for scenario in scenarios for mode in modes for trial in range(trials)
    ]
    random.Random(seed).shuffle(schedule)
    metadata = {
        "version": __version__,
        "provider": getattr(agent, "provider", "scripted"),
        "model": getattr(agent, "model", None),
        "fixture_hash": digest([asdict(s) for s in scenarios]),
        "modes": list(modes),
        "trials": trials,
        "max_steps": max_steps,
        "schedule_seed": seed,
        "scheduled_episodes": len(schedule),
        "scenario_ids": [s.id for s in scenarios],
        "limits": asdict(agent.budget) if hasattr(agent, "budget") else {},
        "environment": "simulated-ticket-service",
        "created": now(),
        "note": "Scripted baselines are not model evaluations. Fixtures are not a population sample.",
    }
    db.create_experiment(experiment_id, metadata)
    for scenario, mode, trial in schedule:
        result = run_episode(db, experiment_id, scenario, mode, agent, max_steps, trial)
        if progress:
            progress(result)
        if result["execution_status"] in {"budget", "error"}:
            break
    return experiment_id


class ReplayAgent:
    """Replay recorded public actions against a fresh world; no API calls."""

    provider = "replay"
    model = None

    def __init__(self, actions: list[dict]):
        self.actions = iter(actions)

    def decide(self, context: dict, mode: str) -> Action:
        value = next(
            self.actions,
            {
                "name": "finish",
                "arguments": {
                    "status": "unknown",
                    "ticket_id": None,
                    "reason": "Recorded action sequence exhausted",
                },
            },
        )
        return Action.parse(value["name"], value["arguments"])
