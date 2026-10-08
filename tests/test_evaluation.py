"""Adversarial evaluation accounting tests. No paid provider calls."""

import json
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from agent_failure_lab.evaluation import (
    LedgerTransport,
    evaluate,
    exclusive_run,
    make_plan,
    paired_outcomes,
    preflight,
    validate_plan,
)
from agent_failure_lab.providers import BudgetExhausted
from agent_failure_lab.schema import Scenario, digest
from agent_failure_lab.storage import Database
from agent_failure_lab.transport import TransportError


class Answers:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0

    def request(self, *args):
        self.calls += 1
        result = next(self.values)
        if isinstance(result, BaseException):
            raise result
        return result


def finish():
    return {
        "usage": {"input_tokens": 50, "output_tokens": 10},
        "output": [
            {
                "type": "function_call",
                "name": "finish",
                "arguments": json.dumps(
                    {"status": "unknown", "ticket_id": None, "reason": "Synthetic protocol test"}
                ),
            }
        ],
    }


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "run.sqlite3"
        self.out = self.root / "report"

    def plan(self, **kwargs):
        options = dict(
            input_price="1",
            output_price="2",
            context_tokens=1000,
            price_source="https://example.test/pricing",
            max_usd="1",
            trials=1,
            max_steps=2,
            max_output_tokens=100,
        )
        options.update(kwargs)
        return make_plan([Scenario("test", "Test", "Synthetic test")], "openai", "test-model", **options)

    def test_plan_mutation_and_code_drift_rejected(self):
        plan = self.plan()
        self.assertEqual(preflight(plan)["network_calls"], 0)
        plan["max_steps"] += 1
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_plan(plan)
        with patch("agent_failure_lab.evaluation.implementation_hash", return_value="different"):
            with self.assertRaisesRegex(ValueError, "Implementation changed"):
                validate_plan(self.plan_with_original_hash())

    def plan_with_original_hash(self):
        plan = self.plan()
        plan["implementation_hash"] = "original"
        plan["plan_hash"] = digest({k: v for k, v in plan.items() if k != "plan_hash"})
        return plan

    def test_bad_limits_and_prices_rejected(self):
        for kw in (
            {"input_price": "NaN"},
            {"max_usd": "-1"},
            {"output_price": "Infinity"},
            {"context_tokens": True},
            {"trials": 0},
            {"max_usd": "0.0000009"},
            {"price_source": "local"},
            {"max_requests": 0},
        ):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.plan(**kw)

    def test_reserve_before_dispatch_and_retain_after_timeout_and_restart(self):
        plan = self.plan(max_requests=1)
        db = Database(self.path)
        first = LedgerTransport(db, plan, Answers([TransportError("connection lost")]))
        with self.assertRaises(TransportError):
            first.request("POST", "unused", {})
        self.assertEqual(first.summary()["unresolved_reservations"], 1)
        second = LedgerTransport(db, plan, Answers([finish()]))
        with self.assertRaises(BudgetExhausted):
            second.request("POST", "unused", {})
        self.assertEqual(second.transport.calls, 0)
        self.assertAlmostEqual(second.summary()["charged_or_reserved_usd"], 0.0012)

    def test_usage_refunds_only_known_cost_and_missing_usage_retains_reserve(self):
        ledger = LedgerTransport(Database(self.path), self.plan(), Answers([finish(), {}, finish()]))
        ledger.request("POST", "unused", {"secret": "never-store-me"})
        ledger.request("POST", "unused", {})
        ledger.request("POST", "unused", {})
        summary = ledger.summary()
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["missing_usage"], 1)
        self.assertAlmostEqual(summary["charged_or_reserved_usd"], 0.00134)
        self.assertNotIn("never-store-me", json.dumps(summary))
        self.assertNotIn("Synthetic protocol test", json.dumps(summary))

    def test_dollar_cap_prevents_dispatch_even_after_new_transport(self):
        plan = self.plan(max_usd="0.0012")
        db = Database(self.path)
        a = LedgerTransport(db, plan, Answers([{}]))
        a.request("POST", "unused", {})
        b = LedgerTransport(db, plan, Answers([{}]))
        with self.assertRaisesRegex(BudgetExhausted, "Remaining budget"):
            b.request("POST", "unused", {})
        self.assertEqual(b.transport.calls, 0)

    def test_atomic_reservations_cannot_overspend_under_concurrency(self):
        plan = self.plan(max_usd="0.0012")
        db = Database(self.path)
        ledgers = [LedgerTransport(db, plan, Answers([{}])) for _ in range(8)]

        def call(ledger):
            try:
                ledger.request("POST", "unused", {})
                return True
            except BudgetExhausted:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(call, ledgers)), 1)
        self.assertEqual(ledgers[0].summary()["requests"], 1)

    def test_exceeded_assumption_halts_future_calls_and_keeps_actual_usage(self):
        value = finish()
        value["usage"]["input_tokens"] = 2000
        ledger = LedgerTransport(Database(self.path), self.plan(), Answers([value, finish()]))
        for _ in range(2):
            with self.assertRaisesRegex(BudgetExhausted, "exceeded"):
                ledger.request("POST", "unused", {})
        self.assertEqual(ledger.transport.calls, 1)
        self.assertTrue(ledger.summary()["halted"])
        self.assertAlmostEqual(ledger.summary()["charged_or_reserved_usd"], 0.00202)

    def test_anthropic_cache_tokens_counted_and_invalid_usage_not_refunded(self):
        values = [
            dict(input_tokens=2, output_tokens=1, cache_read_input_tokens=10, cache_creation_input_tokens=5),
            dict(input_tokens=1, output_tokens=True),
            dict(input_tokens=1, output_tokens=2, cache_read_input_tokens=-1),
        ]
        ledger = LedgerTransport(Database(self.path), self.plan(), Answers([{"usage": u} for u in values]))
        for _ in values:
            ledger.request("POST", "unused", {})
        summary = ledger.summary()
        self.assertEqual(summary["records"][0]["input_tokens"], 17)
        self.assertEqual(summary["missing_usage"], 2)

    def test_different_plan_cannot_reset_same_database_budget(self):
        db = Database(self.path)
        LedgerTransport(db, self.plan())
        with self.assertRaisesRegex(ValueError, "different evaluation plan"):
            LedgerTransport(db, self.plan(max_usd="2"))

    def test_runner_lock_blocks_competing_runner_and_releases(self):
        lock = self.root / "run.lock"
        with exclusive_run(lock):
            with self.assertRaisesRegex(ValueError, "already active"):
                with exclusive_run(lock):
                    self.fail("second lock acquired")
        with exclusive_run(lock):
            pass

    def test_complete_schedule_is_idempotent_and_marked_injected(self):
        plan = self.plan()
        answers = Answers([finish()] * 3)
        first = evaluate(plan, self.path, self.out, transport=answers, api_key="test-only")
        second = evaluate(plan, self.path, self.out, transport=answers, api_key="test-only")
        self.assertTrue(first["coverage"]["complete"])
        self.assertEqual(second["request_ledger"]["requests"], 3)
        self.assertEqual(answers.calls, 3)
        self.assertEqual(first["experiment"]["validation_kind"], "injected-transport-test")
        self.assertEqual(first["paired_outcomes"]["comparisons_to_plain"]["guarded"]["complete_pairs"], 1)
        self.assertTrue((self.out / "evaluation.json").exists())
        with self.assertRaisesRegex(ValueError, "Cannot mix"):
            evaluate(plan, self.path, self.out, api_key="test-only")

    def test_provider_error_kept_visible_and_resume_continues_remaining_schedule(self):
        plan = self.plan()
        a = Answers([TransportError("synthetic timeout")])
        first = evaluate(plan, self.path, self.out, transport=a, api_key="test-only")
        self.assertEqual(first["coverage"]["by_status"]["error"], 1)
        self.assertEqual(first["coverage"]["not_started"], 2)
        self.assertFalse(first["coverage"]["complete"])
        b = Answers([finish()] * 2)
        second = evaluate(plan, self.path, self.out, transport=b, api_key="test-only")
        self.assertEqual(second["coverage"]["recorded"], 3)
        self.assertEqual(second["coverage"]["by_status"]["error"], 1)
        self.assertFalse(second["coverage"]["complete"])
        self.assertEqual(second["request_ledger"]["requests"], 3)
        self.assertEqual(second["request_ledger"]["unresolved_reservations"], 1)

    def test_budget_exit_cannot_be_restarted_to_get_more_calls(self):
        plan = self.plan(max_requests=1)
        answers = Answers([finish()] * 3)
        data = evaluate(plan, self.path, self.out, transport=answers, api_key="test-only")
        self.assertEqual(data["coverage"]["by_status"]["budget"], 1)
        evaluate(plan, self.path, self.out, transport=answers, api_key="test-only")
        self.assertEqual(answers.calls, 1)

    def test_interrupted_episode_resumes_and_old_reservation_is_not_forgotten(self):
        plan = self.plan()
        with self.assertRaises(KeyboardInterrupt):
            evaluate(plan, self.path, self.out, transport=Answers([KeyboardInterrupt()]), api_key="test-only")
        saved = json.loads((self.out / "results.json").read_text())
        self.assertEqual(saved["coverage"]["by_status"]["interrupted"], 1)
        data = evaluate(plan, self.path, self.out, transport=Answers([finish()] * 3), api_key="test-only")
        self.assertTrue(data["coverage"]["complete"])
        self.assertEqual(data["request_ledger"]["requests"], 4)
        self.assertEqual(data["request_ledger"]["unresolved_reservations"], 1)

    def test_pair_analysis_exposes_missing_and_discordant_outcomes(self):
        plan = self.plan(trials=2)
        rows = [
            dict(
                scenario_id="test",
                trial=0,
                mode=m,
                execution_status="completed",
                metrics={"task_success": m == "guarded"},
            )
            for m in ("plain", "guided", "guarded")
        ]
        rows += [dict(scenario_id="test", trial=1, mode="plain", execution_status="error", metrics={})]
        counts = paired_outcomes(rows, plan)["comparisons_to_plain"]["guarded"]
        self.assertEqual(counts["planned_pairs"], 2)
        self.assertEqual(counts["complete_pairs"], 1)
        self.assertEqual(counts["excluded_or_missing_pairs"], 1)
        self.assertEqual(counts["success_difference_on_complete_pairs"], 1)


if __name__ == "__main__":
    unittest.main()
