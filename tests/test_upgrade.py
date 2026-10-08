from dataclasses import asdict
import gzip
import http.client
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import subprocess
import sys

from agent_failure_lab.explore import campaign, generated_scenarios, minimize, replay_case, reproduce
from agent_failure_lab.gateway import RecoveryGateway
from agent_failure_lab.policies import ReferenceAgent
from agent_failure_lab.providers import ScriptedAgent
from agent_failure_lab.runner import TITLE, run_experiment
from agent_failure_lab.schema import Contract, Observation, Scenario
from agent_failure_lab.simulator import SimulatedConnector
from agent_failure_lab.storage import Database
from agent_failure_lab.connectors import HTTPConnector, fixture_handler
from agent_failure_lab.transport import JSONTransport, TransportError
from agent_failure_lab.wire import FaultProxy, FaultRule, isolated_service, load_plan, running_proxy
from tests.test_core import LabCase
from tests import test_integrations


class RecoveryEvidenceTests(LabCase):
    def test_rejected_retry_does_not_establish_failure_of_prior_write(self):
        scenario = Scenario(
            "revoked-after-ack",
            "Lost acknowledgement then revocation",
            "Regression",
            fault="timeout_after",
            lookup=False,
            authorization_until=1,
        )
        experiment = run_experiment(self.db, [scenario], ScriptedAgent(), ["guarded"])
        row = self.db.results(experiment)[0]
        self.assertEqual(row["metrics"]["effects"], 1)
        self.assertFalse(row["metrics"]["false_failure"])
        self.assertEqual(row["decision"]["status"], "unknown")

    def test_rate_limited_retry_does_not_erase_prior_uncertainty(self):
        class Connector:
            contract = Contract(True, False)
            responses = iter([Observation("uncertain"), Observation("rate_limited", retry_after=3)])

            def clock(self):
                return 0

            def create(self, *args):
                return next(self.responses)

        gateway = RecoveryGateway(Connector(), self.db, "op", "Title")
        gateway.create("Title")
        self.assertEqual(gateway.create("Title").status, "uncertain")
        self.assertEqual(self.db.operation("op")["status"], "unknown")

    def test_reconciliation_with_multiple_effects_returns_conflict(self):
        connector = self.connector("no-keys")
        gateway = RecoveryGateway(connector, self.db, "op", "Title")
        gateway.create("Title")
        connector.create("op", "Title")
        result = gateway.create("Title")
        self.assertEqual(result.status, "conflict")
        self.assertEqual(len(result.ticket_ids), 2)
        self.assertNotEqual(self.db.operation("op")["status"], "confirmed")

    def test_reference_uses_stable_key_and_handles_ambiguous_unkeyed_write(self):
        for scenario_id in ("lost-ack", "late-commit", "stale-read", "no-keys", "late-no-keys"):
            with self.subTest(scenario=scenario_id):
                result = self.run_one(scenario_id, "reference")
                self.assertEqual(result["metrics"]["effects"], 1)
                self.assertFalse(result["metrics"]["false_success"])
                self.assertTrue(result["metrics"]["task_success"])
                if self.scenarios[scenario_id].idempotency:
                    keys = {
                        a["arguments"]["idempotency_key"]
                        for a in result["action_replay"]
                        if a["name"] == "create_ticket"
                    }
                    self.assertEqual(len(keys), 1)
                    self.assertNotIn(None, keys)

    def test_reference_abstains_when_contract_cannot_resolve_uncertainty(self):
        result = self.run_one("invisible-no-keys", "reference")
        self.assertEqual(result["decision"]["status"], "unknown")
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["metrics"]["create_calls"], 1)

    def test_reference_does_not_succeed_on_multiple_visible_ids(self):
        action = ReferenceAgent().decide(
            {
                "history": [
                    {"observation": {"status": "found", "ticket_id": "T-1", "ticket_ids": ["T-1", "T-2"]}}
                ],
                "contract": {},
                "remaining_steps": 10,
            },
            "reference",
        )
        self.assertEqual(action.arguments["status"], "unknown")

    def test_reference_replay_does_not_recompute_policy(self):
        row = self.run_one("lost-ack", "reference")
        result = replay_case(row["scenario"], row["action_replay"], "reference")
        self.assertEqual(result["metrics"], row["metrics"])


class ExplorationTests(LabCase):
    def test_generator_is_reproducible_and_varies_with_seed(self):
        first = [asdict(s) for s in generated_scenarios(20, 7)]
        self.assertEqual(first, [asdict(s) for s in generated_scenarios(20, 7)])
        self.assertNotEqual(first, [asdict(s) for s in generated_scenarios(20, 8)])
        with self.assertRaises(ValueError):
            generated_scenarios(1001, 7)

    def test_campaign_records_complete_strong_baseline_comparison(self):
        data = campaign(self.path / "campaign", 8, 7, 12)
        self.assertEqual(len(data["results"]), 24)
        self.assertEqual(data["experiment"]["modes"], ["plain", "reference", "guarded"])
        self.assertEqual(data["experiment"]["campaign"]["seed"], 7)
        self.assertTrue((self.path / "campaign/scenarios.json").is_file())

    def test_reducer_removes_irrelevant_waits_and_proves_deletion_minimality(self):
        row = self.run_one("lost-ack", "plain")
        row["action_replay"] = (
            [{"name": "wait", "arguments": {}}] * 4
            + row["action_replay"]
            + [{"name": "wait", "arguments": {}}] * 3
        )
        artifact = minimize(row, "duplicates", self.path / "repro")
        self.assertEqual(artifact["reduced_actions"], 2)
        self.assertEqual(artifact["minimality"], "1-minimal by action deletion")
        self.assertTrue(reproduce(artifact)["failure_observed"])
        for index in range(2):
            candidate = artifact["actions"][:index] + artifact["actions"][index + 1 :]
            self.assertFalse(
                replay_case(artifact["scenario"], candidate, artifact["mode"])["metrics"]["duplicates"]
            )
        completed = subprocess.run(
            [sys.executable, str(self.path / "repro/test_regression.py")], capture_output=True, text=True
        )
        # The export deliberately asserts the desired invariant and fails on this case.
        self.assertEqual(completed.returncode, 1)
        self.assertIn("FAIL:", completed.stderr)

    def test_compressed_report_round_trip(self):
        import gzip
        from agent_failure_lab.report import bundle, read_bundle

        row = self.run_one("lost-ack", "plain")
        data = bundle(self.db, row["experiment_id"])
        path = self.path / "report.json.gz"
        path.write_bytes(gzip.compress(json.dumps(data).encode()))
        self.assertEqual(read_bundle(path), data)

    def test_reproducer_rejects_tampered_inputs(self):
        row = self.run_one("lost-ack", "plain")
        artifact = minimize(row, "duplicates", self.path / "repro")
        artifact["scenario"]["fault"] = "none"
        with self.assertRaisesRegex(ValueError, "hash"):
            reproduce(artifact)

    def test_reducer_rejects_nonfailure_and_marks_budget_limit(self):
        passing = self.run_one("normal", "plain")
        with self.assertRaises(ValueError):
            minimize(passing, "duplicates", self.path / "repro")
        row = self.run_one("lost-ack", "plain")
        row["action_replay"] = [{"name": "wait", "arguments": {}}] * 10 + row["action_replay"]
        artifact = minimize(row, "duplicates", self.path / "repro", max_evaluations=2)
        self.assertLessEqual(artifact["evaluations"], 2)
        self.assertIn("budget-limited", artifact["minimality"])

    def test_false_success_reduction_preserves_duplicate_mechanism(self):
        row = self.run_one("lost-ack", "plain")
        artifact = minimize(row, "false_success", self.path / "repro")
        self.assertTrue(artifact["metrics"]["duplicates"])
        self.assertTrue(artifact["metrics"]["false_success"])
        self.assertEqual(artifact["reduced_actions"], 3)

    def test_action_replay_preserves_old_claim_but_new_policy_run_uses_fixed_evidence(self):
        path = Path(__file__).resolve().parents[1] / "docs/evidence/revocation-before-fix.json"
        row = json.loads(path.read_text())["result"]
        self.assertTrue(row["metrics"]["false_failure"])
        # Replay fixes the old final claim too; it is not a counterfactual agent rerun.
        replay = replay_case(row["scenario"], row["action_replay"], row["mode"])
        self.assertTrue(replay["metrics"]["false_failure"])
        experiment = run_experiment(
            self.db, [Scenario.from_dict(row["scenario"])], ScriptedAgent(), ["guarded"]
        )
        self.assertFalse(self.db.results(experiment)[0]["metrics"]["false_failure"])


class WireTests(LabCase):
    start_server = test_integrations.ConnectorTests.start_server

    def test_proxy_requires_fixed_loopback_origin(self):
        for url in (
            "http://example.com:80",
            "http://localhost:8000",
            "https://127.0.0.1:8000",
            "http://127.0.0.1:80/private",
            "http://u:p@127.0.0.1:80",
            "http://127.0.0.1:80?target=other",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                FaultProxy(url, [])

    def test_rules_are_one_shot_and_route_specific(self):
        proxy = FaultProxy("http://127.0.0.1:1", [FaultRule("drop_response", occurrence=2)])
        self.assertEqual(proxy.select("GET", "/tickets"), (1, None))
        self.assertEqual(proxy.select("POST", "/tickets"), (1, None))
        self.assertEqual(proxy.select("POST", "/tickets"), (2, "drop_response"))
        self.assertEqual(proxy.select("POST", "/tickets"), (3, None))

    def test_fault_plan_rejects_conflicts_and_unknown_fields(self):
        path = self.path / "plan.json"
        for data in ([{"action": "drop_response", "extra": True}], [{"action": "drop_response"}] * 2):
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                load_plan(path)

    def test_dropped_response_reconciles_across_real_service_process(self):
        scenario = self.scenarios["normal"]
        service_path = self.path / "service.sqlite3"
        with isolated_service(service_path, "service", scenario) as upstream:
            proxy = FaultProxy(upstream, [FaultRule("drop_response")])
            with running_proxy(proxy) as endpoint:
                connector = HTTPConnector(endpoint)
                gateway = RecoveryGateway(connector, self.db, "op", TITLE)
                self.assertEqual(gateway.create(TITLE).status, "uncertain")
                self.assertEqual(gateway.create(TITLE).status, "found")
            state = SimulatedConnector(Database(service_path), "service", scenario).snapshot_for_grader()
        self.assertEqual(len(state["effects"]), 1)
        fault = next(e for e in proxy.events if e["fault"])
        self.assertTrue(fault["upstream_responded"])
        self.assertFalse(fault["response_write_completed"])

    def test_disconnect_before_does_not_reach_upstream(self):
        connector = self.connector("normal")
        upstream = self.start_server(fixture_handler(connector))
        proxy = FaultProxy(upstream, [FaultRule("disconnect_before")])
        with running_proxy(proxy) as endpoint:
            remote = HTTPConnector(endpoint)
            self.assertEqual(remote.create("op", TITLE).status, "uncertain")
        self.assertEqual(connector.snapshot_for_grader()["effects"], [])
        fault = next(e for e in proxy.events if e["fault"])
        self.assertFalse(fault["forward_attempted"])

    def test_corrupt_response_has_committed_effect_and_logs_no_credentials(self):
        connector = self.connector("normal")
        proxy = FaultProxy(
            self.start_server(fixture_handler(connector)),
            [FaultRule("corrupt_response")],
            self.path / "trace.jsonl",
        )
        with running_proxy(proxy) as endpoint:
            remote = HTTPConnector(endpoint)
            self.assertEqual(remote.create("op", TITLE).status, "uncertain")
            JSONTransport().request(
                "GET",
                endpoint + "/tickets?operation_id=op&secret=test-secret-query",
                headers={"Authorization": "Bearer test-secret-header"},
            )
        self.assertEqual(len(connector.snapshot_for_grader()["effects"]), 1)
        log = (self.path / "trace.jsonl").read_text()
        for value in ("test-secret-query", "test-secret-header", TITLE):
            self.assertNotIn(value, log)

    def test_proxy_preserves_content_encoding_when_not_corrupting(self):
        payload = gzip.compress(b'{"ok":true}')

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        proxy = FaultProxy(self.start_server(Handler), [])
        with running_proxy(proxy) as endpoint:
            connection = http.client.HTTPConnection(endpoint.removeprefix("http://"))
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual(response.getheader("Content-Encoding"), "gzip")
                self.assertEqual(gzip.decompress(response.read()), b'{"ok":true}')
            finally:
                connection.close()

    def test_truncated_http_body_is_uncertain_transport_failure(self):
        class Truncated(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"abc")
                self.close_connection = True

        url = self.start_server(Truncated)
        with self.assertRaises(TransportError):
            JSONTransport().request("GET", url)
