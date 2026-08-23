from __future__ import annotations

import json
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.capability_policy import (
    CapabilityClass,
    SideEffectClass,
    ToolDescriptor,
    build_guest_capability_policy,
    build_owner_capability_policy,
    classify_descriptor,
    decide_tool,
)
from astrbot_plugin_shio.core.identity import PrincipalContext
from astrbot_plugin_shio.core.product_trace import (
    ProductOutcome,
    ProductStage,
    ProductTrace,
    ProductTracePayload,
    ProductTraceStatus,
)
from astrbot_plugin_shio.tests.harness.privacy import scan_fixture_tree


ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "p10"
FIXTURE_PATH = FIXTURE_ROOT / "acceptance_matrix.json"
PRODUCTION_DISPOSITIONS = frozenset(
    {
        "sealed_current_web_read",
        "trusted_memory_projection",
        "presentation_only",
        "not_exposed_to_renderer",
        "owner_adapter_all_off",
        "unsupported_operation",
        "hard_disabled",
        "denied_unknown",
    }
)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("p10_fixture_duplicate_or_invalid_key")
        result[key] = value
    return result


def _load_fixture():
    return json.loads(
        FIXTURE_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("p10_fixture_nonfinite")
        ),
    )


def _guest() -> PrincipalContext:
    return PrincipalContext(
        sender_key="scope-synthetic|user-peer",
        sender_id="peer-synthetic",
        is_owner=False,
        relationship_role="group_peer",
        verification_source="astrbot_event_sender_id:owner_allowlist_miss",
    )


def _owner() -> PrincipalContext:
    return PrincipalContext(
        sender_key="scope-synthetic|user-owner",
        sender_id="owner-synthetic",
        is_owner=True,
        relationship_role="owner",
        verification_source="astrbot_event_sender_id:configured_owner_id",
    )


class P10FixedMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = _load_fixture()

    def test_fixture_is_strict_complete_and_privacy_safe(self):
        self.assertEqual(
            set(self.fixture),
            {
                "schema_version",
                "privacy",
                "capabilities",
                "runtime_replays",
                "production_observation",
            },
        )
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(
            self.fixture["privacy"],
            "production_shaped_redacted_only",
        )
        privacy = scan_fixture_tree(FIXTURE_ROOT)
        self.assertTrue(privacy.is_safe, privacy.issue_codes)
        self.assertGreaterEqual(privacy.file_count, 1)

        capability_ids = [
            case["expected"]["capability_id"]
            for case in self.fixture["capabilities"]
        ]
        self.assertEqual(
            set(capability_ids),
            {capability.value for capability in CapabilityClass},
        )
        self.assertEqual(len(capability_ids), len(set(capability_ids)))
        replay_outcomes = [
            case["terminal"]["outcome"]
            for case in self.fixture["runtime_replays"]
        ]
        self.assertEqual(
            set(replay_outcomes),
            {outcome.value for outcome in ProductOutcome},
        )

    def test_every_capability_runs_through_real_classifier_and_policy(self):
        configured_names = tuple(
            case["descriptor"]["name"]
            for case in self.fixture["capabilities"]
        )
        guest_policy = build_guest_capability_policy(
            _guest(),
            configured_tool_names=configured_names,
        )
        owner_policy = build_owner_capability_policy(_owner())

        for case in self.fixture["capabilities"]:
            descriptor_data = case["descriptor"]
            expected = case["expected"]
            with self.subTest(case=case["case_id"]):
                descriptor = ToolDescriptor(
                    name=descriptor_data["name"],
                    description=descriptor_data["description"],
                    origin=descriptor_data["origin"],
                    module_path=descriptor_data["module_path"],
                    parameter_names=tuple(descriptor_data["parameter_names"]),
                    active=descriptor_data["active"],
                    declared_capability="",
                    declared_side_effect="",
                )
                classification = classify_descriptor(descriptor)
                self.assertIs(
                    classification.capability,
                    CapabilityClass(expected["capability_id"]),
                )
                self.assertIs(
                    classification.side_effect,
                    SideEffectClass(expected["side_effect"]),
                )
                self.assertIs(
                    decide_tool(guest_policy, classification).allowed,
                    expected["guest_allowed"],
                )
                self.assertIs(
                    decide_tool(owner_policy, classification).allowed,
                    expected["owner_policy_allowed"],
                )
                self.assertIn(
                    expected["production_disposition"],
                    PRODUCTION_DISPOSITIONS,
                )

    def test_every_matrix_row_points_to_one_executable_regression(self):
        all_cases = (
            *self.fixture["capabilities"],
            *self.fixture["runtime_replays"],
        )
        for case in all_cases:
            test_id = case["evidence_test_id"]
            with self.subTest(case=case["case_id"]):
                loader = unittest.TestLoader()
                suite = loader.loadTestsFromName(test_id)
                self.assertEqual(suite.countTestCases(), 1)
                errors = tuple(getattr(loader, "errors", ()))
                self.assertEqual(errors, ())

    def test_redacted_runtime_records_replay_through_product_trace(self):
        for index, case in enumerate(self.fixture["runtime_replays"], start=1):
            with self.subTest(case=case["case_id"]):
                product_trace = ProductTrace(
                    trace_id=f"{index:032x}",
                    conversation_revision=index,
                    generation_epoch=index,
                )
                for sequence, stage in enumerate(case["stages"], start=1):
                    product_trace.append(
                        ProductStage(stage["stage"]),
                        elapsed_ms=float(sequence),
                        payload=ProductTracePayload(
                            status=ProductTraceStatus(stage["status"]),
                            reason_code=stage["reason_code"],
                        ),
                    )
                terminal = case["terminal"]
                product_trace.terminate(
                    ProductOutcome(terminal["outcome"]),
                    elapsed_ms=float(len(case["stages"]) + 1),
                    reason_code=terminal["reason_code"],
                )
                snapshot = product_trace.snapshot()
                self.assertEqual(
                    snapshot["events"][-1]["outcome"],
                    terminal["outcome"],
                )
                rendered = json.dumps(snapshot, ensure_ascii=False)
                for forbidden in (
                    "private-content",
                    "sender-synthetic",
                    "scope-synthetic",
                    "authorization",
                    "cookie",
                    "prompt",
                ):
                    self.assertNotIn(forbidden, rendered.casefold())

    def test_production_observation_contains_only_bounded_safe_shapes(self):
        observation = self.fixture["production_observation"]
        self.assertEqual(observation["window_hours"], 168)
        self.assertGreater(observation["aggregate_counts"]["pipeline_metrics"], 0)
        self.assertGreater(observation["aggregate_counts"]["send_succeeded"], 0)
        self.assertEqual(
            {record["component"] for record in observation["redacted_shapes"]},
            {
                "pipeline.metrics",
                "send.succeeded",
                "owner_action.denial_prepare_failed",
                "owner_action.delivery_finalize_failed",
            },
        )
        rendered = json.dumps(observation, ensure_ascii=False).casefold()
        for forbidden in (
            "trace_id",
            "subject_digest",
            "target_digest",
            "message_id",
            "sender_id",
            "group_id",
            "content",
            "path",
            "token",
            "secret",
        ):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
