import inspect
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path

from astrbot_plugin_shio.core.contracts import ExpressionModality, MediaContext
from astrbot_plugin_shio.core.output_validator_v2 import (
    build_output_validation_context,
    validate_reply_composer_output,
)
from astrbot_plugin_shio.core.persona import load_persona_package
from astrbot_plugin_shio.core.presentation_handoff import build_presentation_handoff
from astrbot_plugin_shio.core.reply_composer import parse_reply_composer_output
from astrbot_plugin_shio.core.semantic_guard import (
    SemanticGuardContract,
    SemanticGuardPhase,
)
from astrbot_plugin_shio.tests.test_reply_composer import build_request


ROOT = Path(__file__).parents[1]
PACKAGE = load_persona_package(ROOT / "assets" / "personas" / "atri.json")


def setup_turn(
    message="你今天真的很厉害",
    raw_output="哼哼，这次确实做得不错吧。",
    reply_shape=None,
):
    turn, request = build_request(PACKAGE, message)
    media = MediaContext(binding=turn.binding)
    overrides = {"media_context": media}
    if reply_shape is not None:
        overrides["reply_shape"] = reply_shape
    turn, request = build_request(PACKAGE, message, **overrides)
    contract = SemanticGuardContract(
        composer_request=request,
        planned_action=request.planned_action,
        content_intent=request.content_seed.intent,
        current_question_anchor=request.current_question_anchor,
        media_context=request.media_context,
        current_message=message,
        evidence_outcome=None,
        action_outcome=None,
        action_outcome_authority=None,
    )
    context = build_output_validation_context(
        composer_request=request,
        current_message=message,
        expected_target_message_id=request.target_message_id,
        expected_target_sender_key=request.target_sender_key,
        is_owner=request.capability_policy.is_owner,
        semantic_contract=contract,
        current_question_anchor=request.current_question_anchor,
    )
    result = parse_reply_composer_output(request, raw_output)
    validation = validate_reply_composer_output(
        request=request,
        result=result,
        raw_output=raw_output,
        context=context,
        semantic_phase=SemanticGuardPhase.INITIAL,
    )
    return turn, request, contract, result, validation


def present(turn, request, contract, result, validation, **overrides):
    values = {
        "composer_request": request,
        "semantic_contract": contract,
        "action_outcome": None,
        "action_outcome_authority": None,
        "planned_action": turn.action,
        "expression_intent": turn.expression_intent,
        "affect_appraisal": turn.appraisal,
        "result": result,
        "validation": validation,
        "capability_policy": turn.policy,
    }
    values.update(overrides)
    return build_presentation_handoff(**values)


class PresentationHandoffTests(unittest.TestCase):
    def test_validated_text_semantics_have_no_plugin_call_budget_in_p3(self):
        turn, request, contract, result, validation = setup_turn()
        handoff = present(turn, request, contract, result, validation)

        self.assertTrue(handoff.eligible)
        self.assertEqual(handoff.final_visible_text, result.visible_text)
        self.assertEqual(handoff.candidate_call_budget, 0)
        self.assertIn("praise", handoff.emotion_tags)
        self.assertIn("react_then_answer", handoff.semantic_tags)
        self.assertEqual(len(handoff.final_text_digest), 64)

    def test_invalid_or_empty_final_output_returns_empty_handoff(self):
        invalid_turn = setup_turn(
            raw_output=(
                "<|channel|>thought internal <|channel|>final "
                "哼哼，这次确实做得不错吧。"
            )
        )
        rejected = present(*invalid_turn)
        empty_turn = setup_turn(raw_output="")
        empty = present(*empty_turn)

        self.assertFalse(rejected.eligible)
        self.assertFalse(empty.eligible)
        self.assertEqual(rejected.final_visible_text, "")
        self.assertEqual(empty.candidate_call_budget, 0)

    def test_raw_replaced_meme_expression_cannot_bypass_composer_binding(self):
        turn, request, contract, result, validation = setup_turn()
        meme_expression = replace(
            turn.expression_intent,
            modality=ExpressionModality.TEXT_AND_MEME,
            meme_executor="meme_manager",
            max_meme_calls=1,
        )
        handoff = present(
            turn,
            request,
            contract,
            result,
            validation,
            expression_intent=meme_expression,
        )

        self.assertFalse(handoff.eligible)
        self.assertIn("presentation_expression_binding_mismatch", handoff.reason_codes)
        self.assertEqual(handoff.candidate_call_budget, 0)

    def test_capability_or_target_mismatch_cannot_reach_handoff(self):
        turn, request, contract, result, validation = setup_turn()
        wrong_policy = replace(turn.policy, principal_key="another-principal")
        denied = present(
            turn,
            request,
            contract,
            result,
            validation,
            capability_policy=wrong_policy,
        )
        with self.assertRaises(ValueError):
            present(
                turn,
                request,
                contract,
                replace(result, target_message_id="message-b"),
                validation,
            )

        self.assertIn("presentation_principal_mismatch", denied.reason_codes)
        self.assertFalse(denied.eligible)

    def test_trace_metadata_contains_no_text_or_identity(self):
        turn, request, contract, result, validation = setup_turn()
        handoff = present(turn, request, contract, result, validation)
        serialized = str(handoff.trace_metadata())

        self.assertNotIn(result.visible_text, serialized)
        self.assertNotIn("peer-secret-id", serialized)
        self.assertNotIn("message-secret-id", serialized)

    def test_handoff_text_and_digest_cover_the_same_exact_long_payload(self):
        long_visible = "这是需要完整保留的动作结果。" * 120
        turn, request, contract, result, validation = setup_turn(
            raw_output=long_visible,
            reply_shape="long_form",
        )

        handoff = present(turn, request, contract, result, validation)

        self.assertTrue(handoff.eligible)
        self.assertEqual(handoff.final_visible_text, long_visible)
        self.assertEqual(
            handoff.final_text_digest,
            hashlib.sha256(long_visible.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn(long_visible, repr(handoff))

    def test_handoff_api_has_no_legacy_plan_or_tool_payload(self):
        parameters = inspect.signature(build_presentation_handoff).parameters
        source = (ROOT / "core" / "presentation_handoff.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("local_plan", parameters)
        self.assertNotIn("LocalChatPlan", source)
        self.assertNotIn("tool_arguments", source)
        self.assertNotIn("planner_draft", source)
        self.assertNotIn("livingmemory", source.lower())
        self.assertNotIn("astrbot_plugin_meme_manager", source)


if __name__ == "__main__":
    unittest.main()
