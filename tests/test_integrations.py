import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from agent_failure_lab.connectors import GitHubIssueConnector, HTTPConnector, fixture_handler
from agent_failure_lab.gateway import RecoveryGateway
from agent_failure_lab.privacy import redact
from agent_failure_lab.providers import Budget, BudgetExhausted, ModelAgent, ProtocolError
from agent_failure_lab.schema import Contract
from agent_failure_lab.transport import JSONTransport, TransportError, validate_url
from tests.test_core import LabCase


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, payload=None, headers=None):
        self.calls.append((method, url, payload, headers))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def response(provider, name="wait", arguments=None):
    arguments = arguments or {}
    usage = {"input_tokens": 12, "output_tokens": 3}
    if provider == "openai":
        return {
            "output": [{"type": "function_call", "name": name, "arguments": json.dumps(arguments)}],
            "usage": usage,
        }
    return {"content": [{"type": "tool_use", "name": name, "input": arguments}], "usage": usage}


class ProviderTests(unittest.TestCase):
    def test_provider_contracts_and_accounting(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                transport = FakeTransport([response(provider)])
                budget = Budget(max_requests=1)
                model = ModelAgent(provider, "explicit-model", budget, transport, api_key="test-only")
                self.assertEqual(model.decide({"history": []}, "guided").name, "wait")
                self.assertEqual((budget.requests, budget.input_tokens, budget.output_tokens), (1, 12, 3))
                method, url, payload, headers = transport.calls[0]
                self.assertEqual(method, "POST")
                self.assertEqual(payload["model"], "explicit-model")
                self.assertEqual(len(payload["tools"]), 4)
                self.assertIn("Recovery guidance", payload.get("instructions", payload.get("system")))
                if provider == "openai":
                    self.assertFalse(payload["parallel_tool_calls"])
                    self.assertFalse(payload["store"])
                else:
                    self.assertEqual(headers["anthropic-version"], "2023-06-01")
                with self.assertRaises(BudgetExhausted):
                    model.decide({}, "plain")
                self.assertEqual(len(transport.calls), 1)

    def test_malformed_and_multi_tool_responses_fail_closed(self):
        for provider, field in [("openai", "output"), ("anthropic", "content")]:
            valid = response(provider)
            for invalid in ([], {}, {field: None}, {field: ["bad"]}, {field: []}, {field: valid[field] * 2}):
                with self.subTest(provider=provider, invalid=invalid), self.assertRaises(ProtocolError):
                    ModelAgent(provider, "model", Budget(), FakeTransport([invalid]), "test").decide(
                        {}, "plain"
                    )

    def test_prompt_byte_limit_prevents_network_request(self):
        transport = FakeTransport([])
        model = ModelAgent("openai", "model", Budget(max_prompt_bytes=5), transport, "test")
        with self.assertRaises(BudgetExhausted):
            model.decide({"history": []}, "plain")
        self.assertEqual(transport.calls, [])

    def test_usage_is_optional_and_untrusted(self):
        budget = Budget()
        for value in (
            None,
            {},
            {"input_tokens": None, "output_tokens": 2},
            {"input_tokens": -1, "output_tokens": 2},
        ):
            budget.usage(value)
        self.assertEqual(budget.usage_missing, 4)
        self.assertEqual(budget.input_tokens, 0)

    def test_guidance_is_only_added_in_guided_mode(self):
        transport = FakeTransport([response("openai")] * 3)
        agent = ModelAgent("openai", "model", Budget(), transport, "test")
        for mode in ("plain", "guided", "guarded"):
            agent.decide({}, mode)
        prompts = [call[2]["instructions"] for call in transport.calls]
        self.assertEqual(prompts[0], prompts[2])
        self.assertNotEqual(prompts[0], prompts[1])


class ConnectorTests(LabCase):
    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.addCleanup(close)
        return f"http://127.0.0.1:{server.server_port}"

    def test_real_http_roundtrip_with_lost_ack(self):
        service = self.connector()
        remote = HTTPConnector(self.start_server(fixture_handler(service)))
        gateway = RecoveryGateway(remote, self.db, "op", "Title")
        self.assertEqual(gateway.create("Title").status, "uncertain")
        self.assertEqual(gateway.create("Title").status, "found")
        self.assertEqual(len(service.snapshot_for_grader()["effects"]), 1)

    def test_http_missing_and_malformed_responses_are_uncertain(self):
        for failure in (TransportError("timeout"), ["invalid"], {"status": "created", "ticket_id": []}):
            transport = FakeTransport(
                [
                    {
                        "protocol": "afl-ticket-v1",
                        "contract": {"supports_idempotency": False, "supports_lookup": True},
                    },
                    failure,
                ]
            )
            remote = HTTPConnector("https://example.test", transport)
            self.assertEqual(remote.create("op", "x").status, "uncertain")

    def test_redirects_are_not_followed_and_errors_do_not_leak_secrets(self):
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "/secret-endpoint")
                self.end_headers()
                self.wfile.write(b"Bearer private-test-value")

        url = self.start_server(Redirect)
        with self.assertRaises(TransportError) as caught:
            JSONTransport().request("GET", url)
        self.assertEqual(caught.exception.status, 302)
        self.assertNotIn("private-test-value", str(caught.exception))

    def test_invalid_json_and_oversize_responses(self):
        class Invalid(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * (2_000_001 if self.path == "/large" else 1))

        url = self.start_server(Invalid)
        for suffix in ("/", "/large"):
            with self.subTest(suffix=suffix), self.assertRaises(TransportError):
                JSONTransport().request("GET", url + suffix)

    def test_github_writes_require_opt_in_and_uncertainty_blocks_retry(self):
        transport = FakeTransport([TransportError("timeout"), []])
        github = GitHubIssueConnector("owner/test", token="test", transport=transport)
        self.assertEqual(github.create("op", "x").status, "rejected")
        self.assertEqual(transport.calls, [])
        github.allow_writes = True
        gateway = RecoveryGateway(github, self.db, "op", "Title")
        self.assertEqual(gateway.create("Title").status, "uncertain")
        self.assertEqual(gateway.create("Title").status, "uncertain")
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])
        self.assertIn(github.marker("op"), transport.calls[0][2]["body"])

    def test_github_reconciliation_excludes_pull_requests_and_handles_null_body(self):
        marker = GitHubIssueConnector.marker("op")
        transport = FakeTransport(
            [
                [
                    {"number": 1, "body": marker, "pull_request": {}},
                    {"number": 2, "body": marker},
                    {"number": 3, "body": None},
                    {"number": 4, "body": 35},
                ]
            ]
        )
        github = GitHubIssueConnector("owner/test", token="test", transport=transport)
        self.assertEqual(github.lookup("op").ticket_ids, ("2",))

    def test_rate_limit_backoff_is_enforced_by_gateway(self):
        c = self.connector("rate-limit")
        gateway = RecoveryGateway(c, self.db, "op", "Title")
        gateway.create("Title")
        gateway.create("Title")
        self.assertEqual(c.snapshot_for_grader()["calls"], 1)
        for _ in range(3):
            c.wait()
        self.assertEqual(gateway.create("Title").status, "created")


class BoundaryTests(unittest.TestCase):
    def test_unsafe_endpoint_urls_are_rejected(self):
        for url in (
            "http://example.com",
            "file:///etc/passwd",
            "https://user:secret@example.com",
            "https://example.com/#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        validate_url("http://127.0.0.1:1234")
        validate_url("https://example.com")

    def test_connector_capabilities_are_validated(self):
        with self.assertRaises(ValueError):
            Contract("yes", True)
        with self.assertRaises(ValueError):
            Contract(True, True, key_ttl=-1)

    def test_redaction_removes_common_credentials_recursively(self):
        value = {
            "api_key": "test-value",
            "nested": [{"message": "Bearer testcredential sk-examplefake123456789"}],
        }
        encoded = json.dumps(redact(value))
        self.assertNotIn("test-value", encoded)
        self.assertNotIn("testcredential", encoded)
        self.assertNotIn("sk-examplefake123456789", encoded)

    def test_cli_demo_replay_and_compare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = [sys.executable, "-m", "agent_failure_lab"]
            process = subprocess.run(
                common
                + [
                    "demo",
                    "--scenario",
                    "lost-ack",
                    "--db",
                    str(root / "lab.db"),
                    "--output",
                    str(root / "report"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            report = root / "report/results.json"
            data = json.loads(report.read_text())
            episode = data["results"][0]["episode_id"]
            replay = subprocess.run(
                common
                + [
                    "replay",
                    str(report),
                    "--episode",
                    episode,
                    "--db",
                    str(root / "replay.db"),
                    "--output",
                    str(root / "replay"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertIn('"metrics_match": true', replay.stdout)
            comparison = subprocess.run(
                common + ["compare", str(report), str(report)], capture_output=True, text=True
            )
            self.assertEqual(comparison.returncode, 0, comparison.stderr)

    def test_cli_live_run_requires_explicit_model(self):
        result = subprocess.run(
            [sys.executable, "-m", "agent_failure_lab", "run", "--provider", "openai"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--model is required", result.stderr)


class ExperimentFailureTests(LabCase):
    def test_request_budget_stops_schedule_and_marks_incomplete(self):
        from agent_failure_lab.runner import run_experiment
        from agent_failure_lab.report import bundle

        transport = FakeTransport([response("openai")])
        agent = ModelAgent("openai", "test-model", Budget(max_requests=1), transport, "test")
        experiment = run_experiment(self.db, [self.scenarios["normal"]], agent)
        report = bundle(self.db, experiment)
        self.assertEqual(report["experiment"]["scheduled_episodes"], 3)
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["results"][0]["execution_status"], "budget")
        self.assertEqual(report["results"][0]["usage"]["requests"], 1)
        self.assertEqual(report["results"][0]["metrics"]["effects"], 0)

    def test_transport_error_stops_without_retry(self):
        from agent_failure_lab.runner import run_experiment

        transport = FakeTransport([TransportError("HTTP 503", 503)])
        agent = ModelAgent("openai", "test-model", Budget(), transport, "test")
        experiment = run_experiment(self.db, [self.scenarios["normal"]], agent)
        rows = self.db.results(experiment)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["execution_status"], "error")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(rows[0]["decision"]["status"], "unknown")

    def test_unacknowledged_committed_effect_is_not_success(self):
        from agent_failure_lab.runner import run_experiment

        transport = FakeTransport(
            [
                response(
                    "openai",
                    "create_ticket",
                    {"title": "Investigate dropped checkout events", "idempotency_key": None},
                ),
                TransportError("timeout"),
            ]
        )
        agent = ModelAgent("openai", "test-model", Budget(), transport, "test")
        experiment = run_experiment(self.db, [self.scenarios["lost-ack"]], agent, ["plain"])
        row = self.db.results(experiment)[0]
        self.assertEqual(row["metrics"]["effects"], 1)
        self.assertFalse(row["metrics"]["task_success"])
        self.assertTrue(row["metrics"]["unknown"])

    def test_concurrent_backoff_does_not_dispatch_early(self):
        from concurrent.futures import ThreadPoolExecutor

        connector = self.connector("rate-limit")
        gateway = RecoveryGateway(connector, self.db, "op", "Title")
        gateway.create("Title")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: gateway.create("Title"), range(20)))
        self.assertTrue(all(row.status == "rate_limited" for row in results))
        self.assertEqual(connector.snapshot_for_grader()["calls"], 1)
        self.assertEqual(self.db.operation("op")["status"], "retryable")

    def test_github_lookup_scans_more_than_one_page(self):
        first = [{"number": i, "body": "unrelated"} for i in range(100)]
        second = [{"number": 101, "body": GitHubIssueConnector.marker("op")}]
        transport = FakeTransport([first, second])
        connector = GitHubIssueConnector("owner/test", token="test", transport=transport)
        self.assertEqual(connector.lookup("op").ticket_id, "101")
        self.assertEqual(len(transport.calls), 2)
