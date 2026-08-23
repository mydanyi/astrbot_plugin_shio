from __future__ import annotations

import socket
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_shio.tests.harness.assertions import (
    compare_semantic_observation,
)
from astrbot_plugin_shio.tests.harness.loader import load_fixture_suite
from astrbot_plugin_shio.tests.harness.model_adapter import (
    ExternalModelAdapter,
    ExternalModelConfig,
    ExternalModelError,
)
from astrbot_plugin_shio.tests.harness.reporting import render_report
from astrbot_plugin_shio.tests.harness.scenario_runner import (
    EvaluationTier,
    run_deterministic_suite,
    run_external_model_suite,
    run_stub_integration_suite,
)
from astrbot_plugin_shio.tests.harness.stubs import (
    EffectBudgetExceeded,
    FixedClock,
    ScriptViolation,
    ScriptedProvider,
    ScriptedStep,
    SideEffectLedger,
)


ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "p1"


class RecordingTransport:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def evaluate(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("unexpected transport call")
        return self._responses.pop(0)


class P1EvalHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = load_fixture_suite(FIXTURE_ROOT)

    def test_deterministic_runner_consumes_same_fixture_without_network(self):
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network forbidden"),
        ):
            results = run_deterministic_suite(self.suite)

        self.assertEqual(len(results), len(self.suite.cases))
        self.assertTrue(all(result.passed for result in results))
        self.assertEqual({result.tier for result in results}, {EvaluationTier.DETERMINISTIC})
        self.assertEqual(sum(result.model_call_count for result in results), 0)
        self.assertEqual(sum(result.stub_call_count for result in results), 0)

    def test_semantic_assertions_ignore_extra_visible_wording_and_hide_values(self):
        outcome = compare_semantic_observation(
            {"action": "reply", "send_count": 1},
            {
                "action": "reply",
                "send_count": 1,
                "visible_wording": "synthetic reply wording",
            },
        )
        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.assertion_count, 2)

        mismatch = compare_semantic_observation(
            {"action": "reply"},
            {"action": "no_action"},
        )
        self.assertFalse(mismatch.passed)
        self.assertEqual(mismatch.reason_codes, ("value_mismatch:action",))
        self.assertNotIn("reply", str(mismatch.reason_codes))

    def test_fixed_clock_and_side_effect_ledger_are_budgeted(self):
        clock = FixedClock(start_ms=1000)
        ledger = SideEffectLedger(clock=clock, budgets={"model_call": 1, "send": 0})

        self.assertEqual(clock.now_ms(), 1000)
        clock.advance_ms(25)
        ledger.record("model_call", reason_code="scripted_step")
        self.assertEqual(ledger.counts(), {"model_call": 1, "send": 0})
        self.assertEqual(ledger.records[0].at_ms, 1025)
        with self.assertRaises(EffectBudgetExceeded):
            ledger.record("model_call", reason_code="over_budget")
        with self.assertRaises(EffectBudgetExceeded):
            ledger.record("send", reason_code="forbidden")
        with self.assertRaises(ValueError):
            clock.advance_ms(-1)

    def test_scripted_provider_enforces_order_budget_and_exhaustion(self):
        ledger = SideEffectLedger(
            clock=FixedClock(start_ms=2000),
            budgets={"model_call": 2},
        )
        provider = ScriptedProvider(
            steps=(
                ScriptedStep("compose", {"language": "unexpected"}),
                ScriptedStep("repair", {"language": "chinese"}),
            ),
            call_budget=2,
            ledger=ledger,
        )
        first = provider.invoke(case_id="case.synthetic", operation="compose")
        second = provider.invoke(case_id="case.synthetic", operation="repair")
        provider.assert_exhausted()

        self.assertEqual(first["language"], "unexpected")
        self.assertEqual(second["language"], "chinese")
        self.assertEqual(provider.call_count, 2)
        with self.assertRaises(ScriptViolation):
            provider.invoke(case_id="case.synthetic", operation="repair")

        wrong_order = ScriptedProvider(
            steps=(ScriptedStep("compose", {"ok": True}),),
            call_budget=1,
        )
        with self.assertRaises(ScriptViolation):
            wrong_order.invoke(case_id="case.synthetic", operation="repair")

    def test_stub_integration_runner_uses_strict_offline_substitutes(self):
        with patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("network forbidden"),
        ):
            results = run_stub_integration_suite(self.suite)

        self.assertEqual(len(results), len(self.suite.cases))
        self.assertTrue(all(result.passed for result in results))
        self.assertEqual({result.tier for result in results}, {EvaluationTier.STUB_INTEGRATION})
        self.assertGreater(sum(result.stub_call_count for result in results), 0)
        self.assertGreater(sum(result.model_call_count for result in results), 0)

    def test_external_model_requires_explicit_opt_in_and_configuration(self):
        case = self.suite.cases[0]
        with self.assertRaisesRegex(ExternalModelError, "external_model_opt_in_required"):
            ExternalModelAdapter(
                allow_external_model=False,
                config=None,
                transport=None,
            ).evaluate(case)

        with self.assertRaisesRegex(ExternalModelError, "external_model_config_required"):
            ExternalModelAdapter(
                allow_external_model=True,
                config=None,
                transport=None,
            ).evaluate(case)

    def test_external_model_transport_is_injected_and_call_budget_is_strict(self):
        case = self.suite.cases[0]
        transport = RecordingTransport((dict(case.expectation),))
        adapter = ExternalModelAdapter(
            allow_external_model=True,
            config=ExternalModelConfig(
                endpoint="https://model.invalid/evaluate",
                model_alias="configured_model",
                api_key_env="SHIO_EVAL_TEST_KEY",
                call_budget=1,
            ),
            transport=transport,
        )
        results = run_external_model_suite(
            replace(self.suite, cases=(case,)),
            adapter=adapter,
        )

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)
        self.assertEqual(results[0].model_call_count, 1)
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("expect", transport.requests[0])
        with self.assertRaisesRegex(ExternalModelError, "external_model_call_budget_exceeded"):
            adapter.evaluate(case)

    def test_report_schema_never_contains_fixture_body_aliases_or_endpoint(self):
        result = run_deterministic_suite(
            replace(self.suite, cases=(self.suite.cases[0],))
        )[0]
        failed = replace(
            result,
            passed=False,
            reason_codes=("value_mismatch:authority",),
        )
        payload = render_report(
            tier=EvaluationTier.DETERMINISTIC,
            results=(failed,),
            generated_at_ms=3000,
        )

        self.assertIn(failed.case_id, payload)
        self.assertIn("value_mismatch:authority", payload)
        for forbidden in (
            "scope_alias",
            "turn_aliases",
            "owner_turn_a",
            "configured_model",
            "https://",
            "tool_parameters",
            "visible_wording",
        ):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
