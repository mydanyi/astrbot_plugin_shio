import unittest

from astrbot_plugin_shio.core.output_validator_v2 import (
    OutputIssueSeverity,
    OutputValidationIssue,
    OutputValidationReport,
    build_output_validation_context,
    validate_reply_composer_output,
)
from astrbot_plugin_shio.core.repair_controller import (
    RepairAction,
    build_single_repair_request,
    decide_output_repair,
)
from astrbot_plugin_shio.core.reply_composer import parse_reply_composer_output
from astrbot_plugin_shio.core.contracts import MediaAvailability
from astrbot_plugin_shio.core.semantic_guard import SemanticGuardPhase
from astrbot_plugin_shio.tests.test_semantic_guard import (
    canonical_contract,
    planned_action_for,
    semantic_turn,
)


def report(*codes, blocking=()):
    return OutputValidationReport(
        tuple(
            OutputValidationIssue(
                code=code,
                severity=(
                    OutputIssueSeverity.BLOCKING
                    if code in set(blocking)
                    else OutputIssueSeverity.REPAIRABLE
                ),
                detail=code,
            )
            for code in codes
        )
    )


def canonical_validation(contract, raw_output: str):
    request = contract.composer_request
    context = build_output_validation_context(
        composer_request=request,
        current_message=contract.current_message,
        expected_target_message_id=request.target_message_id,
        expected_target_sender_key=request.target_sender_key,
        is_owner=request.capability_policy.is_owner,
        semantic_contract=contract,
        current_question_anchor=contract.current_question_anchor,
    )
    result = parse_reply_composer_output(request, raw_output)
    validation = validate_reply_composer_output(
        request=request,
        result=result,
        raw_output=raw_output,
        context=context,
        semantic_phase=SemanticGuardPhase.INITIAL,
    )
    return request, result, validation


def typed_repair_inputs(visible_text: str, *issue_codes: str):
    message = "你好"
    binding, anchor, media, intent = semantic_turn(message)
    planned_action = planned_action_for(intent)
    contract = canonical_contract(
        message,
        planned_action=planned_action,
        intent=intent,
        anchor=anchor,
        media=media,
    )
    raw_output = (
        "<|channel|>thought hidden thought <|channel|>final " + visible_text
        if "tool_protocol_leak" in issue_codes
        else visible_text
    )
    original, result, validation_report = canonical_validation(contract, raw_output)
    for code in issue_codes:
        if code not in validation_report.issue_codes:
            raise AssertionError((code, validation_report.issues))
    return original, contract, result.visible_text, validation_report


class RepairControllerTests(unittest.TestCase):
    def test_any_typed_structural_mismatch_is_immediately_blocked(self):
        for code in (
            "semantic_binding_mismatch",
            "semantic_target_mismatch",
            "semantic_media_item_mismatch",
            "semantic_contract_mismatch",
        ):
            with self.subTest(code=code):
                decision = decide_output_repair(
                    report(code, blocking=(code,)),
                    repair_attempts_used=0,
                )
                self.assertEqual(decision.action, RepairAction.BLOCK)
                self.assertEqual(decision.repair_generation_budget, 0)

    def test_semantic_repair_request_reuses_exact_contract_and_media_ids(self):
        message = "看看这张图为什么报错。"
        binding, anchor, media, intent = semantic_turn(
            message,
            availability=MediaAvailability.RAW_MEDIA,
        )
        contract = canonical_contract(
            message,
            planned_action=planned_action_for(intent),
            intent=intent,
            anchor=anchor,
            media=media,
        )
        original, result, validation_report = canonical_validation(
            contract,
            "你没有发图。",
        )
        self.assertIn(
            "semantic_media_availability_drift",
            validation_report.issue_codes,
        )

        repair_request = build_single_repair_request(
            original_request=original,
            rejected_visible_text=result.visible_text,
            report=validation_report,
            repair_attempts_used=0,
            semantic_contract=contract,
        )

        self.assertIs(repair_request.semantic_contract, contract)
        self.assertEqual(
            repair_request.media_item_ids,
            tuple(item.item_id for item in media.items),
        )
        self.assertNotIn(media.items[0].item_id, repair_request.user_prompt)

    def test_valid_output_sends_without_repair(self):
        decision = decide_output_repair(
            report(),
            repair_attempts_used=0,
        )

        self.assertEqual(decision.action, RepairAction.SEND)
        self.assertEqual(decision.repair_generation_budget, 0)

    def test_first_severe_failure_gets_exactly_one_repair_generation(self):
        decision = decide_output_repair(
            report("tool_protocol_leak", "unexpected_language_switch"),
            repair_attempts_used=0,
        )

        self.assertEqual(decision.action, RepairAction.GENERATE_ONCE)
        self.assertEqual(decision.repair_generation_budget, 1)

    def test_every_blocking_issue_gets_zero_repair_budget(self):
        blocking_decision = decide_output_repair(
            report("empty_visible_reply", blocking=("empty_visible_reply",)),
            repair_attempts_used=0,
        )
        repairable_decision = decide_output_repair(
            report("empty_visible_reply"),
            repair_attempts_used=0,
        )

        self.assertEqual(blocking_decision.action, RepairAction.BLOCK)
        self.assertEqual(blocking_decision.repair_generation_budget, 0)
        self.assertEqual(repairable_decision.action, RepairAction.GENERATE_ONCE)
        self.assertEqual(repairable_decision.repair_generation_budget, 1)

    def test_target_binding_error_is_immediately_blocked(self):
        decision = decide_output_repair(
            report("target_binding_mismatch", blocking=("target_binding_mismatch",)),
            repair_attempts_used=0,
        )

        self.assertEqual(decision.action, RepairAction.BLOCK)
        self.assertEqual(decision.repair_generation_budget, 0)

    def test_failed_repair_without_fallback_is_blocked(self):
        decision = decide_output_repair(
            report("nonowner_relationship_escalation"),
            repair_attempts_used=1,
        )

        self.assertEqual(decision.action, RepairAction.BLOCK)
        self.assertEqual(decision.repair_generation_budget, 0)

    def test_attempt_counts_above_one_can_never_allocate_another_generation(self):
        for attempts in (1, 2, 9):
            with self.subTest(attempts=attempts):
                decision = decide_output_repair(
                    report("tool_protocol_leak"),
                    repair_attempts_used=attempts,
                )
                self.assertNotEqual(decision.action, RepairAction.GENERATE_ONCE)
                self.assertEqual(decision.repair_generation_budget, 0)

    def test_repair_request_contains_only_visible_rejection_and_issue_codes(self):
        original, contract, rejected_visible, validation_report = typed_repair_inputs(
            "可以呀。",
            "tool_protocol_leak",
        )
        request = build_single_repair_request(
            original_request=original,
            rejected_visible_text=rejected_visible,
            report=validation_report,
            repair_attempts_used=0,
            semantic_contract=contract,
        )

        self.assertEqual(request.generation_call_budget, 1)
        self.assertEqual(request.routine_style_rewrite_budget, 0)
        self.assertIn("tool_protocol_leak", request.issue_codes)
        self.assertNotIn(rejected_visible, request.user_prompt)
        self.assertNotIn("hidden thought", request.user_prompt)
        self.assertIn("受限重新生成", request.system_prompt)
        rendered = repr(request)
        for forbidden in (
            request.target_message_id,
            request.system_prompt,
            request.user_prompt,
            "你好",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_repair_request_cannot_be_built_after_attempt_is_used(self):
        original, contract, rejected_visible, validation_report = typed_repair_inputs(
            "可以呀。",
            "tool_protocol_leak",
        )
        with self.assertRaises(ValueError):
            build_single_repair_request(
                original_request=original,
                rejected_visible_text=rejected_visible,
                report=validation_report,
                repair_attempts_used=1,
                semantic_contract=contract,
            )


if __name__ == "__main__":
    unittest.main()
