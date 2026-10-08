from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_failure_lab.gateway import RecoveryGateway
from agent_failure_lab.grading import grade
from agent_failure_lab.providers import ScriptedAgent
from agent_failure_lab.report import bundle, compare, write_report
from agent_failure_lab.runner import ReplayAgent, run_episode, run_experiment
from agent_failure_lab.schema import Action, Observation, Scenario, load_scenarios
from agent_failure_lab.simulator import SimulatedConnector
from agent_failure_lab.storage import Database


class LabCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.db = Database(self.path / "lab.sqlite3")
        self.scenarios = {s.id: s for s in load_scenarios()}

    def connector(self, scenario="lost-ack", namespace="world"):
        return SimulatedConnector(self.db, namespace, self.scenarios[scenario])

    def run_one(self, scenario="lost-ack", mode="guarded", agent=None):
        agent = agent or ScriptedAgent()
        eid = run_experiment(self.db, [self.scenarios[scenario]], agent, [mode])
        return self.db.results(eid)[0]


class SchemaTests(unittest.TestCase):
    def test_scenarios_are_unique_and_strict(self):
        self.assertEqual(len(load_scenarios()), 24)
        for bad in (
            {"fault": []},
            {"key_ttl": 0},
            {"idempotency": 1},
            {"visibility_delay": -1},
            {"surprise": 1},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Scenario.from_dict(dict(id="test", name="Test", description="Test", **bad))

    def test_invalid_actions_fail_closed(self):
        for name, args in [
            ("finish", {"status": [], "ticket_id": None, "reason": "x"}),
            ("create_ticket", {"title": "x"}),
            ("wait", {"extra": 1}),
            ("execute_code", {}),
            ([], {}),
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                Action.parse(name, args)

    def test_observation_shape_validation(self):
        for value in (
            {"status": "found", "ticket_ids": "T-1"},
            {"status": "found", "ticket_id": 2},
            {"status": "found", "retry_after": -1},
            {"status": "found", "message": []},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Observation.from_dict(value)


class SimulatorTests(LabCase):
    def test_lost_ack_does_not_mean_absent(self):
        c = self.connector()
        self.assertEqual(c.create("op", "Title").status, "uncertain")
        self.assertEqual(c.lookup("op").ticket_ids, ("T-1",))

    def test_lookup_absence_does_not_mean_no_pending_write(self):
        c = self.connector("late-no-keys")
        c.create("op", "Title")
        self.assertEqual(c.lookup("op").status, "not_found")
        c.settle()
        self.assertEqual(len(c.snapshot_for_grader()["effects"]), 1)

    def test_key_conflict_and_replay(self):
        c = self.connector("normal")
        first = c.create("op", "Title", "key")
        self.assertEqual(c.create("op", "Title", "key").ticket_id, first.ticket_id)
        self.assertEqual(c.create("op", "Changed", "key").status, "rejected")
        self.assertEqual(len(c.snapshot_for_grader()["effects"]), 1)

    def test_expired_key_no_longer_deduplicates(self):
        c = self.connector("key-expiry")
        c.create("op", "Title", "key")
        c.wait()
        c.create("op", "Title", "key")
        self.assertEqual(len(c.snapshot_for_grader()["effects"]), 2)

    def test_revocation_prevents_inflight_commit(self):
        c = self.connector("revoked-in-flight")
        c.create("op", "Title", "key")
        c.settle()
        self.assertEqual(c.snapshot_for_grader()["effects"], [])

    def test_world_cannot_be_reopened_with_different_fixture(self):
        self.connector()
        with self.assertRaises(ValueError):
            self.connector("normal")


class GatewayTests(LabCase):
    def test_no_keys_blocks_uncertain_retry(self):
        c = self.connector("invisible-no-keys")
        gateway = RecoveryGateway(c, self.db, "op", "Title")
        self.assertEqual(gateway.create("Title").status, "uncertain")
        for _ in range(5):
            self.assertEqual(gateway.create("Title").status, "uncertain")
        self.assertEqual(c.snapshot_for_grader()["calls"], 1)

    def test_known_rejection_and_payload_mismatch(self):
        c = self.connector("rejected")
        gateway = RecoveryGateway(c, self.db, "op", "Title")
        self.assertEqual(gateway.create("Other").status, "rejected")
        self.assertEqual(c.snapshot_for_grader()["calls"], 0)
        gateway.create("Title")
        gateway.create("Title")
        self.assertEqual(c.snapshot_for_grader()["calls"], 1)

    def test_concurrent_unkeyed_workers_only_dispatch_once(self):
        c = self.connector("invisible-no-keys")

        def work(_):
            return RecoveryGateway(c, Database(self.db.path), "op", "Title").create("Title")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(20)))
        self.assertEqual(c.snapshot_for_grader()["calls"], 1)
        self.assertEqual(len(c.snapshot_for_grader()["effects"]), 1)

    def test_concurrent_keyed_workers_commit_once(self):
        c = self.connector("lost-ack")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: RecoveryGateway(c, self.db, "op", "Title").create("Title"), range(20)))
        self.assertEqual(len(c.snapshot_for_grader()["effects"]), 1)

    def test_actual_process_death_after_dispatch(self):
        script = """
import os,sys
from agent_failure_lab.storage import Database
from agent_failure_lab.schema import load_scenarios
from agent_failure_lab.simulator import SimulatedConnector
from agent_failure_lab.gateway import RecoveryGateway
s=next(s for s in load_scenarios() if s.id=='invisible-no-keys')
db=Database(sys.argv[1])
c=SimulatedConnector(db,'world',s)
RecoveryGateway(c,db,'op','Title',lambda: os._exit(73)).create('Title')
"""
        completed = subprocess.run([sys.executable, "-c", script, str(self.db.path)], capture_output=True)
        self.assertEqual(completed.returncode, 73, completed.stderr.decode())
        db = Database(self.db.path)
        c = SimulatedConnector(db, "world", self.scenarios["invisible-no-keys"])
        self.assertEqual(db.operation("op")["status"], "pending")
        self.assertEqual(RecoveryGateway(c, db, "op", "Title").create("Title").status, "uncertain")
        self.assertEqual(c.snapshot_for_grader()["calls"], 1)

    def test_key_expiry_conservatively_blocks_retry(self):
        c = self.connector("key-expiry")
        gateway = RecoveryGateway(c, self.db, "op", "Title")
        gateway.create("Title")
        c.wait()
        self.assertEqual(gateway.create("Title").status, "uncertain")
        self.assertEqual(c.snapshot_for_grader()["calls"], 1)

    def test_confirmed_record_cannot_be_downgraded(self):
        self.db.claim("op", "hash", "key", 0)
        self.db.update_operation("op", "confirmed", {"status": "created"})
        self.db.update_operation("op", "unknown", {"status": "uncertain"})
        self.assertEqual(self.db.operation("op")["status"], "confirmed")
        with self.assertRaises(ValueError):
            self.db.claim("op", "different", "key", 0)


class RunnerTests(LabCase):
    def test_every_fixture_runs_and_guards_have_known_limits(self):
        eid = run_experiment(self.db, list(self.scenarios.values()), ScriptedAgent())
        results = self.db.results(eid)
        self.assertEqual(len(results), 72)
        self.assertFalse(any(r["execution_status"] == "error" for r in results))
        guarded = {r["scenario_id"]: r for r in results if r["mode"] == "guarded"}
        self.assertEqual(
            [k for k, r in guarded.items() if r["metrics"]["duplicates"]], ["redelivery-no-keys"]
        )
        self.assertTrue(guarded["unfinished-no-keys"]["metrics"]["missing"])
        self.assertTrue(guarded["invisible-no-keys"]["metrics"]["unknown"])
        self.assertTrue(guarded["lost-ack"]["metrics"]["task_success"])
        self.assertTrue(guarded["restart-after-write"]["metrics"]["task_success"])

    def test_model_sees_no_hidden_scenario_or_world(self):
        test = self

        class Spy(ScriptedAgent):
            def decide(self, context, mode):
                test.assertEqual(set(context), {"task", "contract", "clock", "remaining_steps", "history"})
                encoded = json.dumps(context)
                for forbidden in [
                    "scenario_hash",
                    "fault_count",
                    "visible_at",
                    "ticket_committed",
                    "lost-ack",
                ]:
                    test.assertNotIn(forbidden, encoded)
                return super().decide(context, mode)

        self.run_one(agent=Spy())

    def test_action_replay_reproduces_all_fixture_metrics(self):
        eid = run_experiment(self.db, list(self.scenarios.values()), ScriptedAgent())
        for index, original in enumerate(self.db.results(eid)):
            with self.subTest(scenario=original["scenario_id"], mode=original["mode"]):
                replay_id = f"replay-{index}"
                self.db.create_experiment(replay_id, {})
                replay = run_episode(
                    self.db,
                    replay_id,
                    Scenario.from_dict(original["scenario"]),
                    original["mode"],
                    ReplayAgent(original["action_replay"]),
                    original["max_steps"],
                )
                self.assertEqual(original["metrics"], replay["metrics"])

    def test_grader_catches_wrong_identity_and_false_failure(self):
        world = {"effects": [{"operation_id": "op", "title": "x", "ticket_id": "T-1"}], "calls": 1, "tick": 1}
        self.assertTrue(grade(world, {"status": "succeeded", "ticket_id": "T-2"}, "op", "x")["false_success"])
        self.assertTrue(grade(world, {"status": "failed"}, "op", "x")["false_failure"])

    def test_invalid_actions_are_bounded(self):
        class Invalid:
            def decide(self, *args):
                return Action("arbitrary-tool", {})

        result = self.run_one(agent=Invalid())
        self.assertEqual(result["protocol_errors"], 16)
        self.assertEqual(result["metrics"]["create_calls"], 0)
        self.assertEqual(result["execution_status"], "step_limit")

    def test_resume_after_real_process_death(self):
        script = """
import os,sys
from agent_failure_lab.storage import Database
from agent_failure_lab.schema import load_scenarios
from agent_failure_lab.providers import ScriptedAgent
from agent_failure_lab.runner import run_episode
from agent_failure_lab.simulator import SimulatedConnector
s=next(s for s in load_scenarios() if s.id=='invisible-no-keys')
db=Database(sys.argv[1]);db.create_experiment('crash',{'provider':'scripted'})
old=SimulatedConnector.create
def crash(self,*a,**k):
    old(self,*a,**k)
    os._exit(74)
SimulatedConnector.create=crash
run_episode(db,'crash',s,'guarded',ScriptedAgent(),episode_id='dead')
"""
        completed = subprocess.run([sys.executable, "-c", script, str(self.db.path)], capture_output=True)
        self.assertEqual(completed.returncode, 74, completed.stderr.decode())
        self.assertEqual(self.db.episode("dead")["checkpoint"]["pending"]["name"], "create_ticket")
        row = run_episode(
            self.db,
            "crash",
            self.scenarios["invisible-no-keys"],
            "guarded",
            ScriptedAgent(),
            episode_id="dead",
            resume=True,
        )
        self.assertEqual(row["metrics"]["effects"], 1)
        self.assertEqual(row["metrics"]["create_calls"], 1)
        self.assertEqual(len(row["action_replay"]), 16)


class ReportTests(LabCase):
    def test_json_cannot_break_out_of_embedded_script(self):
        result = self.run_one()
        data = bundle(self.db, result["experiment_id"])
        data["results"][0]["description"] = "</script><script>alert(1)</script>"
        target = write_report(data, self.path / "report")
        self.assertNotIn("</script><script>alert(1)", target.read_text())
        self.assertIn("\\u003c/script\\u003e", target.read_text())
        self.assertTrue((target.parent / "results.json").is_file())

    def test_comparison_detects_regression(self):
        result = self.run_one()
        before = bundle(self.db, result["experiment_id"])
        after = json.loads(json.dumps(before))
        after["results"][0]["metrics"]["duplicates"] = True
        self.assertFalse(compare(before, after)["passed"])
        self.assertTrue(compare(before, before)["passed"])

    def test_comparison_rejects_different_or_incomplete_population(self):
        result = self.run_one()
        before = bundle(self.db, result["experiment_id"])
        for mutation in ("fixture_hash", "scheduled_episodes", "schedule_seed"):
            after = json.loads(json.dumps(before))
            after["experiment"][mutation] = "changed"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                compare(before, after)

    def test_unfinished_episode_preserves_identity(self):
        self.db.create_experiment("ex", {})
        self.db.create_episode(
            "ep", "ex", {"scenario": asdict(self.scenarios["normal"]), "mode": "plain", "trial": 2}
        )
        row = self.db.results("ex")[0]
        self.assertEqual((row["scenario_id"], row["mode"], row["trial"]), ("normal", "plain", 2))
        self.assertEqual(row["execution_status"], "interrupted")
