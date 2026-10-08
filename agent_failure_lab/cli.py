"""Command-line interface. Network use is always explicit."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import uuid

from . import __version__
from .connectors import HTTPConnector, fixture_handler
from .providers import Budget, ModelAgent, ScriptedAgent
from .report import bundle, compare, read_bundle, write_report
from .runner import MODES, ReplayAgent, run_episode, run_experiment
from .schema import Scenario, digest, load_scenarios
from .simulator import SimulatedConnector
from .storage import Database
from .transport import TransportError


def positive(value):
    number = int(value)
    if not 1 <= number <= 100_000:
        raise argparse.ArgumentTypeError("Expected an integer from 1 to 100000")
    return number


def parser():
    root = argparse.ArgumentParser(
        prog="afl", description="Reproduce uncertain agent writes; grade the effects."
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("demo", "run"):
        command = commands.add_parser(
            name,
            help="Run offline baselines" if name == "demo" else "Run a live model against simulated tools",
        )
        command.add_argument("--db", type=Path, default=Path("runs/lab.sqlite3"))
        command.add_argument("--output", type=Path, default=Path("reports/latest"))
        command.add_argument("--fixtures", type=Path)
        command.add_argument("--scenario", action="append", help="Scenario id; repeat to select multiple")
        command.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
        command.add_argument("--trials", type=positive, default=1)
        command.add_argument("--max-steps", type=positive, default=16)
        command.add_argument("--seed", type=int, default=0)
        if name == "run":
            model_options(command)
    commands.add_parser("scenarios", help="List bundled fixtures")
    listing = commands.add_parser("list", help="List local experiments")
    listing.add_argument("--db", type=Path, default=Path("runs/lab.sqlite3"))
    report = commands.add_parser("report", help="Render a stored experiment")
    report.add_argument("experiment")
    report.add_argument("--db", type=Path, default=Path("runs/lab.sqlite3"))
    report.add_argument("--output", type=Path, default=Path("reports/latest"))
    replay = commands.add_parser("replay", help="Re-execute a recorded action sequence without model calls")
    replay.add_argument("report", type=Path)
    replay.add_argument("--episode", required=True)
    replay.add_argument("--db", type=Path, default=Path("runs/replay.sqlite3"))
    replay.add_argument("--output", type=Path, default=Path("reports/replay"))
    resume = commands.add_parser("resume", help="Resume one interrupted episode from its checkpoint")
    resume.add_argument("episode")
    resume.add_argument("--db", type=Path, default=Path("runs/lab.sqlite3"))
    resume.add_argument("--output", type=Path, default=Path("reports/resumed"))
    model_options(resume, scripted=True)
    comparison = commands.add_parser("compare", help="Return exit code 1 if paired fixtures regress")
    comparison.add_argument("before", type=Path)
    comparison.add_argument("after", type=Path)
    serving = commands.add_parser("serve", help="Serve a generated report on loopback")
    serving.add_argument("--directory", type=Path, default=Path("reports/latest"))
    serving.add_argument("--port", type=positive, default=8768)
    fixture = commands.add_parser("fixture-server", help="Expose one mock ticket service over loopback HTTP")
    fixture.add_argument("--scenario", default="lost-ack")
    fixture.add_argument("--db", type=Path, default=Path("runs/http.sqlite3"))
    fixture.add_argument("--namespace", default="http-demo")
    fixture.add_argument("--port", type=positive, default=8769)
    probe = commands.add_parser(
        "probe", help="Read an afl-ticket-v1 service contract without writing a ticket"
    )
    probe.add_argument("url")
    return root


def model_options(command, scripted=False):
    command.add_argument(
        "--provider",
        choices=(["scripted"] if scripted else []) + ["openai", "anthropic"],
        default="scripted" if scripted else None,
        required=not scripted,
    )
    command.add_argument("--model", help="Explicit provider model id; no model is silently selected")
    command.add_argument("--max-requests", type=positive, default=100)
    command.add_argument("--max-output-tokens", type=positive, default=1024)
    command.add_argument("--max-prompt-bytes", type=positive, default=64000)


def agent_for(args):
    if getattr(args, "provider", "scripted") == "scripted":
        return ScriptedAgent()
    if not args.model:
        raise ValueError("--model is required for a live provider")
    return ModelAgent(
        args.provider, args.model, Budget(args.max_requests, args.max_output_tokens, args.max_prompt_bytes)
    )


def announce(db, experiment_id, output):
    data = bundle(db, experiment_id)
    target = write_report(data, output)
    print(
        json.dumps(
            {"experiment_id": experiment_id, "report": str(target.resolve()), "summary": data["summary"]},
            indent=2,
        )
    )
    return data


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "scenarios":
            for scenario in load_scenarios():
                print(f"{scenario.id:24} {scenario.description}")
        elif args.command in {"demo", "run"}:
            scenarios = load_scenarios(args.fixtures)
            if args.scenario:
                unknown = set(args.scenario) - {s.id for s in scenarios}
                if unknown:
                    raise ValueError(f"Unknown scenarios: {', '.join(sorted(unknown))}")
                scenarios = [s for s in scenarios if s.id in args.scenario]
            if len(args.modes) != len(set(args.modes)):
                raise ValueError("Each mode may only be selected once")
            db = Database(args.db)
            experiment_id = run_experiment(
                db,
                scenarios,
                agent_for(args),
                args.modes,
                args.trials,
                args.max_steps,
                args.seed,
                progress=lambda r: print(
                    f"{r['scenario_id']} / {r['mode']}: {r['execution_status']}", file=sys.stderr
                ),
            )
            data = announce(db, experiment_id, args.output)
            if any(r["execution_status"] in {"budget", "error"} for r in data["results"]):
                return 2
        elif args.command == "list":
            print(json.dumps(Database(args.db).list_experiments(), indent=2))
        elif args.command == "report":
            announce(Database(args.db), args.experiment, args.output)
        elif args.command == "replay":
            source = read_bundle(args.report)
            row = next((r for r in source["results"] if r["episode_id"] == args.episode), None)
            if not row or "scenario" not in row or "action_replay" not in row:
                raise ValueError("Episode has no portable scenario and action record")
            scenario = Scenario.from_dict(row["scenario"])
            if scenario.fingerprint() != row["scenario_hash"]:
                raise ValueError("Scenario fingerprint does not match")
            db, experiment_id = Database(args.db), uuid.uuid4().hex[:12]
            db.create_experiment(
                experiment_id,
                {
                    "version": __version__,
                    "provider": "replay",
                    "model": None,
                    "fixture_hash": digest([asdict(scenario)]),
                    "modes": [row["mode"]],
                    "trials": 1,
                    "max_steps": row["max_steps"],
                    "scheduled_episodes": 1,
                    "source_episode": row["episode_id"],
                    "environment": "simulated-ticket-service",
                    "note": "Recorded actions, not another model evaluation.",
                },
            )
            result = run_episode(
                db,
                experiment_id,
                scenario,
                row["mode"],
                ReplayAgent(row["action_replay"]),
                row["max_steps"],
                row["trial"],
            )
            announce(db, experiment_id, args.output)
            matched = result["metrics"] == row["metrics"]
            print(json.dumps({"metrics_match": matched}))
            return 0 if matched else 1
        elif args.command == "resume":
            db = Database(args.db)
            row = db.episode(args.episode)
            if row["status"] != "running":
                raise ValueError(
                    "Only interrupted running episodes can resume; use replay for finished episodes"
                )
            metadata = db.experiment(row["experiment_id"])
            if (args.provider, args.model) != (metadata["provider"], metadata.get("model")):
                raise ValueError("Resume must use the original provider and model")
            spec = row["spec"]
            result = run_episode(
                db,
                row["experiment_id"],
                Scenario.from_dict(spec["scenario"]),
                spec["mode"],
                agent_for(args),
                spec["max_steps"],
                spec["trial"],
                args.episode,
                resume=True,
            )
            announce(db, row["experiment_id"], args.output)
            return 2 if result["execution_status"] in {"budget", "error"} else 0
        elif args.command == "compare":
            result = compare(read_bundle(args.before), read_bundle(args.after))
            print(json.dumps(result, indent=2))
            return 0 if result["passed"] else 1
        elif args.command == "serve":
            if not (args.directory / "index.html").is_file():
                raise ValueError("Generate a report before serving it")
            server = ThreadingHTTPServer(
                ("127.0.0.1", args.port),
                partial(SimpleHTTPRequestHandler, directory=str(args.directory.resolve())),
            )
            print(f"Report: http://127.0.0.1:{args.port}", flush=True)
            with server:
                server.serve_forever()
        elif args.command == "fixture-server":
            scenario = next((s for s in load_scenarios() if s.id == args.scenario), None)
            if scenario is None:
                raise ValueError("Unknown scenario")
            connector = SimulatedConnector(Database(args.db), args.namespace, scenario)
            server = ThreadingHTTPServer(("127.0.0.1", args.port), fixture_handler(connector))
            print(
                f"Mock service: http://127.0.0.1:{args.port} (persistent namespace {args.namespace})",
                flush=True,
            )
            with server:
                server.serve_forever()
        elif args.command == "probe":
            connector = HTTPConnector(args.url)
            print(json.dumps({"contract": asdict(connector.contract), "clock": connector.clock()}, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Saved checkpoints remain in the local database.", file=sys.stderr)
        return 130
    except (ValueError, OSError, KeyError, TransportError) as exc:
        print(f"afl: {exc}", file=sys.stderr)
        return 2
