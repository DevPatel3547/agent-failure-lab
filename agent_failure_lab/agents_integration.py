"""Optional integration with the real OpenAI Agents SDK; the demo model is scripted.

The SDK executes actual function tools against an isolated service through the fault
proxy. No OpenAI API calls, API keys, or remote tracing are used by this demo.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from importlib.metadata import version
import json
from pathlib import Path
import uuid

from . import __version__
from .connectors import HTTPConnector
from .gateway import RecoveryGateway
from .grading import grade, summarize
from .report import write_report
from .runner import TITLE
from .schema import Scenario, digest
from .simulator import SimulatedConnector
from .storage import Database, now
from .wire import FAULT_ACTIONS, FaultProxy, FaultRule, isolated_service, running_proxy


def ticket_tools(connector, journal, operation_id, title, *, guarded=True):
    """Bind one application-owned operation identity across distinct SDK tool call IDs.

    A new logical task needs a new operation_id. Preserve that ID and the journal when
    resuming the same task. Neither a model-generated call ID nor a process-local cache
    is a durable business operation identity.
    """
    from agents import function_tool

    gateway = RecoveryGateway(connector, journal, operation_id, title)

    @function_tool(failure_error_function=None)
    async def create_ticket(title: str) -> str:
        """Create the requested ticket; an uncertain response may mean it already exists."""
        result = await asyncio.to_thread(
            gateway.create if guarded else lambda value: connector.create(operation_id, value, None), title
        )
        return json.dumps(result.to_dict())

    @function_tool(failure_error_function=None)
    async def lookup_ticket() -> str:
        """Look up this logical operation; empty results may reflect delayed visibility."""
        result = await asyncio.to_thread(connector.lookup, operation_id)
        return json.dumps(result.to_dict())

    @function_tool(failure_error_function=None)
    async def wait() -> str:
        """Advance the test service clock by one tick."""
        result = await asyncio.to_thread(connector.wait)
        return json.dumps(result.to_dict())

    return [create_ticket, lookup_ticket, wait]


def scripted_sdk_model(*, parallel_first=False):
    """A test double only for model inference. Runner and function execution stay real."""
    from agents import Model, ModelResponse, Usage
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

    class RetryModel(Model):
        def __init__(self):
            self.turn = 0
            self.inputs = []

        async def get_response(self, system_instructions, input, *args, **kwargs):
            self.inputs.append({"system_instructions": system_instructions, "input": deepcopy(input)})
            self.turn += 1
            observations = [
                json.loads(item["output"])
                for item in input
                if isinstance(item, dict) and item.get("type") == "function_call_output"
            ]
            latest = observations[-1] if observations else {}
            if latest.get("status") in {"created", "found"}:
                decision = {
                    "status": "succeeded",
                    "ticket_id": latest.get("ticket_id"),
                    "reason": "Scripted SDK policy saw a ticket acknowledgement.",
                }
                output = [
                    ResponseOutputMessage(
                        id=f"message-{self.turn}",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(type="output_text", annotations=[], text=json.dumps(decision))
                        ],
                    )
                ]
            else:
                name = "wait" if latest.get("status") in {"rate_limited", "pending"} else "create_ticket"
                arguments = {"title": TITLE} if name == "create_ticket" else {}
                output = [
                    ResponseFunctionToolCall(
                        id=f"item-{self.turn}-{i}",
                        call_id=f"call-{self.turn}-{i}",
                        type="function_call",
                        name=name,
                        arguments=json.dumps(arguments),
                    )
                    for i in range(2 if parallel_first and self.turn == 1 else 1)
                ]
            return ModelResponse(output=output, usage=Usage(), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError("The deterministic SDK demo is non-streaming")
            yield  # Make the implementation an async generator, as required by the SDK interface.

    return RetryModel()


def run_sdk_case(work: Path, action: str, keys: bool, mode: str, *, parallel_first=False) -> dict:
    from agents import Agent, Runner, RunConfig, MaxTurnsExceeded

    episode = uuid.uuid4().hex
    operation = "sdk-" + episode
    scenario = Scenario(
        "sdk-service", "Healthy local service", "Only the HTTP proxy injects faults.", idempotency=keys
    )
    service_db = work / (episode + "-service.sqlite3")
    journal = Database(work / (episode + "-journal.sqlite3"))
    model = scripted_sdk_model(parallel_first=parallel_first)
    decision = {"status": "unknown", "ticket_id": None, "reason": "SDK turn budget exhausted"}
    state = "step_limit"
    rule = FaultRule(action)
    with isolated_service(service_db, episode, scenario) as upstream:
        proxy = FaultProxy(upstream, [rule])
        with running_proxy(proxy) as endpoint:
            connector = HTTPConnector(endpoint)
            agent = Agent(
                name="Ticket recovery integration",
                instructions="Create exactly one ticket. Public contract: "
                + json.dumps(asdict(connector.contract)),
                model=model,
                tools=ticket_tools(connector, journal, operation, TITLE, guarded=mode == "guarded"),
            )
            try:
                result = Runner.run_sync(
                    agent, TITLE, max_turns=12, run_config=RunConfig(tracing_disabled=True)
                )
                decision = json.loads(result.final_output)
                state = "completed"
            except MaxTurnsExceeded:
                pass
        world = SimulatedConnector(Database(service_db), episode, scenario).snapshot_for_grader()
    metrics = grade(world, decision, operation, TITLE)
    case_id = action.replace("_", "-") + ("-keyed" if keys else "-unkeyed")
    trace = list(world["events"])
    trace.append(
        {
            "sequence": len(trace),
            "tick": world["tick"],
            "scope": "grader",
            "event": "effects_checked",
            "details": metrics,
        }
    )
    return {
        "episode_id": episode,
        "scenario_id": case_id,
        "scenario_name": case_id,
        "description": "Actual OpenAI Agents SDK Runner and function tools; scripted model; "
        "separate service process and real HTTP fault injection.",
        "scenario_hash": digest({"fault": asdict(rule), "keys": keys, "parallel_first": parallel_first}),
        "mode": mode,
        "trial": 0,
        "provider": "scripted",
        "model": None,
        "execution_status": state,
        "decision": decision,
        "metrics": metrics,
        "steps": model.turn,
        "trace": trace,
        "effects": world["effects"],
        "wire_events": proxy.events,
        "sdk_model_inputs": model.inputs,
        "parallel_first": parallel_first,
        "usage": {},
        "idempotency": keys,
    }


def run_sdk_demo(output: Path) -> dict:
    experiment_id = uuid.uuid4().hex[:12]
    work = output / "private-runs" / experiment_id
    work.mkdir(parents=True, exist_ok=True)
    results = [
        run_sdk_case(work, action, keys, mode)
        for action in sorted(FAULT_ACTIONS)
        for keys in (True, False)
        for mode in ("plain", "guarded")
    ]
    data = {
        "schema_version": 1,
        "experiment": {
            "id": experiment_id,
            "version": __version__,
            "provider": "scripted",
            "model": None,
            "created": now(),
            "environment": "http-loopback-separate-process",
            "integration": "openai-agents",
            "integration_version": version("openai-agents"),
            "openai_client_version": version("openai"),
            "modes": ["plain", "guarded"],
            "scheduled_episodes": len(results),
            "trials": 1,
            "max_steps": 12,
            "schedule_seed": 0,
            "fixture_hash": digest(sorted(FAULT_ACTIONS)),
            "note": "Real SDK execution, scripted inference. This is integration evidence, "
            "not a live-model result or a claim of a bug in the SDK.",
        },
        "results": results,
        "summary": summarize(results),
    }
    write_report(data, output)
    return data
