"""Deterministic HTTP faults against a fixed loopback service.

This is a local development reverse proxy, not a general forward proxy. It never
retries a request or infers whether an upstream response implies a committed effect.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import multiprocessing
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit
import uuid

from .connectors import HTTPConnector, fixture_handler
from .gateway import RecoveryGateway
from .grading import grade, summarize
from .providers import ScriptedAgent
from .policies import ReferenceAgent
from .runner import TITLE
from .schema import Action, Scenario, digest
from .simulator import SimulatedConnector
from .storage import Database, now
from . import __version__

FAULT_ACTIONS = {"disconnect_before", "drop_response", "corrupt_response", "http_503_before"}
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


@dataclass(frozen=True)
class FaultRule:
    action: str
    method: str = "POST"
    path: str = "/tickets"
    occurrence: int = 1

    def __post_init__(self):
        if not isinstance(self.action, str) or self.action not in FAULT_ACTIONS:
            raise ValueError("Unsupported wire fault")
        if not isinstance(self.method, str) or self.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Unsupported HTTP method")
        if (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or "?" in self.path
            or "#" in self.path
            or len(self.path) > 1000
        ):
            raise ValueError("Rules match an absolute path without query or fragment")
        if type(self.occurrence) is not int or not 1 <= self.occurrence <= 10000:
            raise ValueError("occurrence must be 1–10000")


def load_plan(path: Path) -> list[FaultRule]:
    if path.stat().st_size > 100000:
        raise ValueError("Fault plan exceeds 100 KB")
    values = json.loads(path.read_text())
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError("A plan contains 1–100 rules")
    try:
        rules = [FaultRule(**value) for value in values]
    except TypeError as exc:
        raise ValueError("Malformed fault rule") from exc
    identities = {(r.method, r.path, r.occurrence) for r in rules}
    if len(identities) != len(rules):
        raise ValueError("Only one fault may target a request occurrence")
    return rules


class FaultProxy:
    def __init__(self, upstream: str, rules: list[FaultRule], trace_path: Path | None = None):
        parsed = urlsplit(upstream)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            local = False
        if (
            parsed.scheme != "http"
            or not local
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not parsed.port
        ):
            raise ValueError(
                "Upstream must be a fixed HTTP loopback IP and explicit port, without credentials or path"
            )
        self.host, self.port = parsed.hostname, parsed.port
        self.rules = {(r.method, r.path, r.occurrence): r.action for r in rules}
        if len(self.rules) != len(rules):
            raise ValueError("Conflicting fault rules")
        self.counts = Counter()
        self.lock = threading.Lock()
        self.events = []
        self.trace_path = trace_path
        if trace_path:
            trace_path.parent.mkdir(parents=True, exist_ok=True)

    def select(self, method, path):
        with self.lock:
            self.counts[(method, path)] += 1
            occurrence = self.counts[(method, path)]
            return occurrence, self.rules.get((method, path, occurrence))

    def record(self, **event):
        # Intentionally no URL query, headers, or request/response payloads.
        with self.lock:
            event = {"sequence": len(self.events), **event}
            self.events.append(event)
            if self.trace_path:
                with self.trace_path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(event) + "\n")

    def handler(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def disconnect(self):
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()

            def handle_request(self):
                started = time.monotonic()
                route = urlsplit(self.path)
                # Absolute-form requests and tunneled destinations cannot change the fixed upstream.
                if route.scheme or route.netloc or not self.path.startswith("/") or route.fragment:
                    self.send_error(400, "Origin-form path required")
                    return
                if self.headers.get("Transfer-Encoding"):
                    self.send_error(400, "Chunked requests are not supported by this local test proxy")
                    return
                lengths = self.headers.get_all("Content-Length", [])
                try:
                    if len(lengths) > 1:
                        raise ValueError()
                    length = int(lengths[0]) if lengths else 0
                    if not 0 <= length <= 2_000_000:
                        raise ValueError()
                except ValueError:
                    self.send_error(413, "Invalid request length")
                    return
                try:
                    body = self.rfile.read(length) if length else None
                except (OSError, TimeoutError):
                    self.disconnect()
                    return
                if length and len(body) != length:
                    self.disconnect()
                    return
                occurrence, fault = proxy.select(self.command, route.path)
                event = {
                    "method": self.command,
                    "path": route.path,
                    "occurrence": occurrence,
                    "fault": fault,
                    "forward_attempted": False,
                    "upstream_responded": False,
                    "response_write_completed": False,
                    "upstream_status": None,
                }
                connection = None
                try:
                    if fault == "disconnect_before":
                        self.disconnect()
                        return
                    if fault == "http_503_before":
                        payload, status, headers = b'{"error":"injected local fault"}', 503, []
                    else:
                        connection = http.client.HTTPConnection(proxy.host, proxy.port, timeout=10)
                        # Strip standard hop headers and any connection-nominated fields.
                        blocked = HOP_HEADERS | {
                            x.strip().lower() for x in self.headers.get("Connection", "").split(",")
                        }
                        headers_out = {k: v for k, v in self.headers.items() if k.lower() not in blocked}
                        event["forward_attempted"] = True
                        connection.request(self.command, self.path, body=body, headers=headers_out)
                        response = connection.getresponse()
                        payload = response.read(2_000_001)
                        if len(payload) > 2_000_000:
                            raise ValueError("Upstream response exceeds the local proxy limit")
                        event["upstream_responded"], event["upstream_status"] = True, response.status
                        status, headers = response.status, response.getheaders()
                        if fault == "drop_response":
                            self.disconnect()
                            return
                        if fault == "corrupt_response":
                            payload = b'{"injected_truncated_json":'
                    self.send_response(status)
                    response_blocked = HOP_HEADERS | {"server", "date"}
                    for header, value in headers:
                        if header.lower() == "connection":
                            response_blocked |= {x.strip().lower() for x in value.split(",")}
                    if fault == "corrupt_response":
                        response_blocked |= {"content-encoding", "etag", "content-md5"}
                    for key, value in headers:
                        if key.lower() not in response_blocked:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()
                    event["response_write_completed"] = True
                    self.close_connection = True
                except (OSError, http.client.HTTPException, ValueError):
                    event["transport_error"] = True
                    self.disconnect()
                finally:
                    if connection:
                        connection.close()
                    event["duration_seconds"] = round(time.monotonic() - started, 6)
                    proxy.record(**event)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

        return Handler


@contextmanager
def running_proxy(proxy: FaultProxy):
    server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _service_worker(path, namespace, scenario, pipe):
    try:
        connector = SimulatedConnector(Database(path), namespace, Scenario.from_dict(scenario))
        with ThreadingHTTPServer(("127.0.0.1", 0), fixture_handler(connector)) as server:
            pipe.send({"port": server.server_port})
            pipe.close()
            server.serve_forever()
    except Exception as exc:
        pipe.send({"error": type(exc).__name__})
        pipe.close()


@contextmanager
def isolated_service(db_path: Path, namespace: str, scenario: Scenario):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_service_worker, args=(str(db_path), namespace, asdict(scenario), child))
    process.start()
    child.close()
    try:
        if not parent.poll(15):
            raise RuntimeError("Isolated ticket service did not start")
        ready = parent.recv()
        if "error" in ready:
            raise RuntimeError("Isolated ticket service failed: " + ready["error"])
        yield f"http://127.0.0.1:{ready['port']}"
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def run_wire_demo(output: Path, max_steps: int = 16) -> dict:
    """Run 24 real-transport cases; each service is a separate owned child process."""
    if not 2 <= max_steps <= 200:
        raise ValueError("max_steps must be 2–200")
    from .report import write_report

    experiment_id = uuid.uuid4().hex[:12]
    work = output / "private-runs" / experiment_id
    work.mkdir(parents=True, exist_ok=True)
    results = []
    for action in sorted(FAULT_ACTIONS):
        for keys in (True, False):
            for mode in ("plain", "reference", "guarded"):
                episode = uuid.uuid4().hex
                operation = "wire-" + episode
                scenario = Scenario(
                    "wire-service",
                    "Healthy ticket service",
                    "No service-level fault injection.",
                    idempotency=keys,
                )
                service_db, journal = (
                    work / (episode + "-service.sqlite3"),
                    Database(work / (episode + "-client.sqlite3")),
                )
                rule = FaultRule(action)
                visible, trace = [], []
                decision = {"status": "unknown", "ticket_id": None, "reason": "Step budget exhausted"}
                state = "step_limit"
                with isolated_service(service_db, episode, scenario) as service:
                    proxy = FaultProxy(service, [rule])
                    with running_proxy(proxy) as endpoint:
                        connector = HTTPConnector(endpoint)
                        gateway = RecoveryGateway(connector, journal, operation, TITLE)
                        agent = ReferenceAgent() if mode == "reference" else ScriptedAgent()
                        for step in range(max_steps):
                            context = {
                                "task": {"operation_id": operation, "title": TITLE},
                                "contract": asdict(connector.contract),
                                "clock": connector.clock(),
                                "remaining_steps": max_steps - step,
                                "history": visible,
                            }
                            chosen = agent.decide(context, mode)
                            chosen = Action.parse(chosen.name, chosen.arguments)
                            trace.append(
                                {
                                    "sequence": len(trace),
                                    "tick": context["clock"],
                                    "scope": "agent",
                                    "event": "action",
                                    "details": asdict(chosen),
                                }
                            )
                            if chosen.name == "finish":
                                decision, state = chosen.arguments, "completed"
                                break
                            if chosen.name == "create_ticket":
                                response = (
                                    gateway.create(chosen.arguments["title"])
                                    if mode == "guarded"
                                    else connector.create(
                                        operation,
                                        chosen.arguments["title"],
                                        chosen.arguments["idempotency_key"],
                                    )
                                )
                            elif chosen.name == "lookup_ticket":
                                response = connector.lookup(operation)
                            else:
                                response = connector.wait()
                            visible.append({"action": asdict(chosen), "observation": response.to_dict()})
                            trace.append(
                                {
                                    "sequence": len(trace),
                                    "tick": connector.clock(),
                                    "scope": "tool",
                                    "event": "observation",
                                    "details": response.to_dict(),
                                }
                            )
                    # Read authoritative state only after the caller's execution has finished.
                    world = SimulatedConnector(Database(service_db), episode, scenario).snapshot_for_grader()
                metrics = grade(world, decision, operation, TITLE)
                for event in world["events"]:
                    if event["scope"] == "world":
                        trace.append({**event, "sequence": len(trace)})
                trace.append(
                    {
                        "sequence": len(trace),
                        "tick": world["tick"],
                        "scope": "grader",
                        "event": "effects_checked",
                        "details": metrics,
                    }
                )
                trace.sort(
                    key=lambda e: (
                        e["tick"],
                        0 if e["scope"] == "world" else 2 if e["scope"] == "grader" else 1,
                    )
                )
                for sequence, event in enumerate(trace):
                    event["sequence"] = sequence
                case_id = action.replace("_", "-") + ("-keyed" if keys else "-unkeyed")
                results.append(
                    {
                        "episode_id": episode,
                        "experiment_id": experiment_id,
                        "scenario_id": case_id,
                        "scenario_name": action.replace("_", " ").title()
                        + (" · keys" if keys else " · no keys"),
                        "description": "A separate ticket-service process receives real HTTP traffic through a deterministic local fault proxy.",
                        "scenario_hash": digest({"rule": asdict(rule), "keys": keys}),
                        "mode": mode,
                        "trial": 0,
                        "provider": "scripted",
                        "model": None,
                        "execution_status": state,
                        "decision": decision,
                        "metrics": metrics,
                        "steps": step + 1,
                        "usage": {},
                        "trace": trace,
                        "visible_history": visible,
                        "effects": world["effects"],
                        "wire_events": proxy.events,
                        "fault_plan": [asdict(rule)],
                        "idempotency": keys,
                        "transport": "http-loopback-separate-process",
                    }
                )
    data = {
        "schema_version": 1,
        "experiment": {
            "id": experiment_id,
            "version": __version__,
            "provider": "scripted",
            "model": None,
            "created": now(),
            "environment": "http-loopback-separate-process",
            "modes": ["plain", "reference", "guarded"],
            "trials": 1,
            "max_steps": max_steps,
            "schedule_seed": 0,
            "scheduled_episodes": len(results),
            "fixture_hash": digest(sorted(FAULT_ACTIONS)),
            "note": "Actual HTTP disconnects and malformed responses; synthetic service, scripted agents, no model API calls.",
        },
        "results": results,
        "summary": summarize(results),
    }
    write_report(data, output)
    return data
