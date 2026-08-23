from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_shio.core.action_planner import PlannedAction, StructuralOutcome
from astrbot_plugin_shio.core.affect import appraise_affect
from astrbot_plugin_shio.core.affect_state import _issue_test_affect_render_context
from astrbot_plugin_shio.core.capability_policy import (
    CapabilityClass,
    ToolDescriptor,
    build_guest_capability_policy,
    build_owner_capability_policy,
    classify_descriptor,
    decide_tool,
)
from astrbot_plugin_shio.core.content_intent_builder import build_content_intent_seed
from astrbot_plugin_shio.core.context_assembler import ReplyTarget, build_reference_context
from astrbot_plugin_shio.core.conversation_ledger import ledger_content_digest
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    DecisionBinding,
    ExpressionIntent,
    ExpressionModality,
    MediaContext,
)
from astrbot_plugin_shio.core.current_question_anchor import build_current_question_anchor
from astrbot_plugin_shio.core.expression_retrieval import retrieve_expression_candidates
from astrbot_plugin_shio.core.identity import (
    TurnEnvelope,
    build_scope_key,
    build_sender_key,
    resolve_principal,
)
from astrbot_plugin_shio.core.knowledge_gap import decide_knowledge_gap
from astrbot_plugin_shio.core.output_validator_v2 import (
    build_output_validation_context,
    validate_reply_composer_output,
)
from astrbot_plugin_shio.core.persona import load_persona_package
from astrbot_plugin_shio.core.persona_expression import build_persona_expression_plan
from astrbot_plugin_shio.core.presentation_handoff import build_presentation_handoff
from astrbot_plugin_shio.core.reply_composer import (
    build_reply_composer_request,
    parse_reply_composer_output,
)
from astrbot_plugin_shio.core.runtime_invariant import typed_runtime_decision
from astrbot_plugin_shio.core.send_receipt import InternalSendReceiptLedger
from astrbot_plugin_shio.core.semantic_guard import SemanticGuardContract


ROOT = Path(__file__).parents[1]
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "p8_scenario_matrix.json"
PERSONA = load_persona_package(ROOT / "assets" / "personas" / "atri.json")


def fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def scope_for(chat_type, sender_id):
    return build_scope_key(
        platform_id="test",
        bot_id="shio",
        chat_type=chat_type,
        group_id="test-group" if chat_type == "group" else "",
        session_id="test-private" if chat_type == "private" else "test-group",
    )


def principal_and_policy(case):
    scope_key = scope_for(case["chat_type"], case["sender_id"])
    principal = resolve_principal(
        sender_id=case["sender_id"],
        sender_key=build_sender_key(scope_key, case["sender_id"]),
        chat_type=case["chat_type"],
        owner_ids=case["owner_ids"],
        verification_source="astrbot_event_sender_id",
        identity_verified=True,
    )
    policy = (
        build_owner_capability_policy(principal)
        if principal.is_owner
        else build_guest_capability_policy(
            principal,
            configured_tool_names=("anysearch_search", "search_memes"),
        )
    )
    return scope_key, principal, policy


def typed_bundle(case, *, referenced_message_id=""):
    scope_key, principal, policy = principal_and_policy(case)
    message = case["message"]
    binding = DecisionBinding(
        scope_key=scope_key,
        session_id="test-group" if case["chat_type"] == "group" else "test-private",
        current_message_id=case.get("current_message_id", f"message-{case['id']}"),
        current_sender_key=principal.sender_key,
        current_content_digest=ledger_content_digest(message),
        conversation_revision=1,
        generation_epoch=1,
        trace_id=hashlib.sha256(case["id"].encode()).hexdigest()[:32],
    )
    target = ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="current_inbound",
        referenced_message_id=referenced_message_id,
        degradation_reasons=(),
    )
    anchor = build_current_question_anchor(binding, message)
    media = MediaContext(binding=binding)
    content_seed = build_content_intent_seed(
        anchor=anchor,
        reply_target=target,
        media_context=media,
    )
    action = PlannedAction(
        action=ActionDecision(
            binding=binding,
            kind=ActionKind.REPLY,
            reply_target=target,
            reason_codes=("scenario_reply",),
        ),
        structural_outcome=StructuralOutcome.CONTINUE,
        planner_reason_codes=("scenario_reply",),
    )
    appraisal = appraise_affect(
        principal=principal,
        reply_target=target,
        current_message=message,
    )
    persona_expression = build_persona_expression_plan(
        PERSONA,
        appraisal,
        principal=principal,
    )
    retrieval = retrieve_expression_candidates(PERSONA, persona_expression)
    expression = ExpressionIntent(
        binding=binding,
        reply_target=target,
        modality=ExpressionModality.TEXT,
        social_act=persona_expression.topic_return,
        emotion_tags=(appraisal.trigger.value, appraisal.surface_emotion.value),
        max_bubbles=3,
        reason_codes=("scenario_text",),
    )
    request = build_reply_composer_request(
        planned_action=action,
        content_seed=content_seed,
        expression_intent=expression,
        affect_appraisal=appraisal,
        continuous_affect=_issue_test_affect_render_context(binding),
        persona_expression=persona_expression,
        expression_candidates=retrieval.candidates,
        persona_package=PERSONA,
        capability_policy=policy,
        current_message=message,
        sender_name="当前发言者",
        current_question_anchor=anchor,
        media_context=media,
    )
    return SimpleNamespace(
        scope_key=scope_key,
        principal=principal,
        policy=policy,
        binding=binding,
        target=target,
        anchor=anchor,
        content_seed=content_seed,
        action=action,
        appraisal=appraisal,
        persona_expression=persona_expression,
        expression=expression,
        request=request,
        media=media,
    )


def tool_descriptor(name):
    values = {
        "anysearch_search": ToolDescriptor(
            name="anysearch_search",
            description="Search public web",
            origin="plugin",
            module_path="astrbot_plugin_anysearch.main",
            parameter_names=("query",),
            active=True,
            declared_capability="",
            declared_side_effect="",
        ),
        "search_memes": ToolDescriptor(
            name="search_memes",
            description="Search local meme candidates",
            origin="plugin",
            module_path="astrbot_plugin_meme_manager.main",
            parameter_names=("query",),
            active=True,
            declared_capability="",
            declared_side_effect="",
        ),
        "shell_exec": ToolDescriptor(
            name="shell_exec",
            description="Execute a shell command",
            origin="core",
            module_path="astrbot.core.tools.shell",
            parameter_names=("command",),
            active=True,
            declared_capability="",
            declared_side_effect="",
        ),
    }
    return values[name]


class P8ScenarioMatrixTests(unittest.TestCase):
    def test_fixture_is_synthetic_and_runtime_budget_follows_action(self):
        data = fixture()
        self.assertEqual(data["privacy"], "synthetic_redacted_only")
        case = data["simple_scenarios"][0]
        turn = typed_bundle(case)
        runtime = typed_runtime_decision(
            activation_ready=True,
            planned_action=turn.action,
        )
        self.assertTrue(runtime.activate_typed_pipeline)
        self.assertEqual(runtime.acquisition_call_budget, 0)
        self.assertEqual(runtime.final_generation_budget, 1)
        self.assertEqual(runtime.final_send_budget, 1)

    def test_simple_owner_guest_group_private_scenarios_run_typed_end_to_end(self):
        for case in fixture()["simple_scenarios"]:
            with self.subTest(case=case["id"]):
                turn = typed_bundle(case)
                self.assertEqual(turn.policy.policy_kind, case["expected_policy"])
                self.assertEqual(
                    turn.appraisal.relationship_distance.value,
                    case["expected_relationship"],
                )
                result = parse_reply_composer_output(
                    turn.request,
                    case["visible_output"],
                )
                semantic_contract = SemanticGuardContract(
                    composer_request=turn.request,
                    planned_action=turn.action,
                    content_intent=turn.content_seed.intent,
                    current_question_anchor=turn.anchor,
                    media_context=turn.media,
                    current_message=case["message"],
                    evidence_outcome=None,
                    action_outcome=None,
                    action_outcome_authority=None,
                )
                validation = validate_reply_composer_output(
                    request=turn.request,
                    result=result,
                    raw_output=case["visible_output"],
                    context=build_output_validation_context(
                        composer_request=turn.request,
                        current_message=case["message"],
                        expected_target_message_id=turn.target.message_id,
                        expected_target_sender_key=turn.principal.sender_key,
                        is_owner=turn.principal.is_owner,
                        semantic_contract=semantic_contract,
                        current_question_anchor=turn.anchor,
                    ),
                )
                self.assertTrue(validation.is_valid, validation.issue_codes)
                self.assertEqual(len(result.bubbles), case["expected_bubbles"])

                handoff = build_presentation_handoff(
                    planned_action=turn.action,
                    expression_intent=turn.expression,
                    affect_appraisal=turn.appraisal,
                    result=result,
                    validation=validation,
                    capability_policy=turn.policy,
                    composer_request=turn.request,
                    semantic_contract=semantic_contract,
                    action_outcome=None,
                    action_outcome_authority=None,
                )
                self.assertEqual(handoff.eligible, case["expected_presentation"])
                self.assertEqual(handoff.candidate_call_budget, 0)

                send_ledger = InternalSendReceiptLedger(
                    id_factory=lambda: f"send-{case['id']}",
                    now_fn=lambda: 100.0,
                )
                reply = send_ledger.begin_reply(
                    target=turn.target,
                    visible_segments=result.bubbles,
                )
                for segment in reply.segments:
                    send_ledger.mark_attempted(segment.segment_id)
                    send_ledger.mark_succeeded(segment.segment_id)
                sent = send_ledger.sent_reply_record(reply.internal_reply_id)
                self.assertTrue(sent.is_terminal)
                self.assertEqual(len(sent.successful_segments), case["expected_bubbles"])

    def test_guest_web_read_is_allowed_but_shell_is_denied(self):
        case = fixture()["tool_scenarios"][0]
        turn = typed_bundle(case)
        web = decide_tool(
            turn.policy,
            classify_descriptor(tool_descriptor("anysearch_search")),
        )
        shell = decide_tool(
            turn.policy,
            classify_descriptor(tool_descriptor("shell_exec")),
        )
        gap = decide_knowledge_gap(
            content_seed=turn.content_seed,
            current_message=case["message"],
        )

        self.assertEqual(web.allowed, case["expected_web_allowed"])
        self.assertEqual(shell.allowed, case["expected_shell_allowed"])
        self.assertTrue(gap.requires_evidence)
        self.assertEqual(gap.requested_capability, CapabilityClass.PUBLIC_WEB_READ)
        self.assertLessEqual(turn.policy.max_external_tool_calls, 2)

    def test_verified_owner_private_policy_keeps_full_capability(self):
        case = fixture()["tool_scenarios"][1]
        turn = typed_bundle(case)
        web = decide_tool(
            turn.policy,
            classify_descriptor(tool_descriptor("anysearch_search")),
        )
        shell = decide_tool(
            turn.policy,
            classify_descriptor(tool_descriptor("shell_exec")),
        )

        self.assertTrue(turn.principal.is_owner)
        self.assertEqual(turn.policy.policy_kind, case["expected_policy"])
        self.assertEqual(web.allowed, case["expected_web_allowed"])
        self.assertEqual(shell.allowed, case["expected_shell_allowed"])
        self.assertTrue(turn.policy.agent_full)

    def test_meme_is_a_p6_presentation_capability_not_a_renderer_tool(self):
        case = fixture()["simple_scenarios"][1]
        turn = typed_bundle(case)
        meme = decide_tool(
            turn.policy,
            classify_descriptor(tool_descriptor("search_memes")),
        )

        self.assertTrue(meme.allowed)
        self.assertEqual(meme.classification.capability, CapabilityClass.LOCAL_PRESENTATION)
        self.assertEqual(turn.policy.max_local_presentation_calls, 1)
        self.assertNotIn("search_memes", turn.request.system_prompt)
        self.assertNotIn("search_memes", turn.request.user_prompt)

    def test_quote_keeps_owner_principal_and_reference_separate(self):
        case = fixture()["quoted_scenario"]
        turn = typed_bundle(case, referenced_message_id=case["quoted_message_id"])
        envelope = TurnEnvelope(
            session_id="test-group",
            message_id=case["current_message_id"],
            scope_key=turn.scope_key,
            sender_key=turn.principal.sender_key,
            sender_id=case["sender_id"],
            platform_id="test",
            bot_id="shio",
            chat_type="group",
            group_id="test-group",
            reply_to_message_id=case["quoted_message_id"],
            reply_to_sender_id=case["quoted_sender_id"],
            timestamp=1_765_000_100,
            timestamp_source="fixture",
            source_kind="inbound",
            degradation_reasons=(),
        )
        reference = build_reference_context(envelope)

        self.assertTrue(turn.principal.is_owner)
        self.assertEqual(turn.target.sender_key, turn.principal.sender_key)
        self.assertNotEqual(reference.sender_key, turn.principal.sender_key)
        self.assertEqual(turn.action.action.reply_target, turn.target)


if __name__ == "__main__":
    unittest.main()
