from __future__ import annotations

import json
import hashlib
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.affect import appraise_affect
from astrbot_plugin_shio.core.affect_state import _issue_test_affect_render_context
from astrbot_plugin_shio.core.capability_policy import (
    build_guest_capability_policy,
    build_owner_capability_policy,
)
from astrbot_plugin_shio.core.content_intent_builder import build_content_intent_seed
from astrbot_plugin_shio.core.context_assembler import (
    ProvenancedFact,
    ReplyTarget,
    assemble_context_views,
    build_direct_reply_target,
    build_reference_context,
    select_facts_for_target,
)
from astrbot_plugin_shio.core.action_planner import PlannedAction, StructuralOutcome
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    DecisionBinding,
    ExpressionIntent,
    ExpressionModality,
    MediaContext,
)
from astrbot_plugin_shio.core.current_question_anchor import build_current_question_anchor
from astrbot_plugin_shio.core.conversation_ledger import adapt_astrbot_history
from astrbot_plugin_shio.core.generation_epoch import GenerationEpochRegistry
from astrbot_plugin_shio.core.expression_retrieval import retrieve_expression_candidates
from astrbot_plugin_shio.core.identity import (
    TurnEnvelope,
    build_sender_key,
    resolve_principal,
)
from astrbot_plugin_shio.core.output_validator_v2 import (
    build_output_validation_context,
    validate_reply_composer_output,
)
from astrbot_plugin_shio.core.persona import load_persona_package
from astrbot_plugin_shio.core.persona_expression import build_persona_expression_plan
from astrbot_plugin_shio.core.reply_composer import (
    build_reply_composer_request,
    parse_reply_composer_output,
)
from astrbot_plugin_shio.core.send_receipt import InternalSendReceiptLedger
from astrbot_plugin_shio.core.semantic_guard import SemanticGuardContract


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "p8_offline_acceptance.json"
PERSONA = load_persona_package(
    Path(__file__).parents[1] / "assets" / "personas" / "atri.json"
)


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def cases(category):
    return [
        item
        for item in load_fixture()["cases"]
        if item["category"] == category
    ]


def envelope_for(case, scope_key, *, message_id=None):
    sender_id = case.get("current_sender_id", "owner-b")
    return TurnEnvelope(
        session_id="test-group",
        message_id=message_id or case["current_message_id"],
        scope_key=scope_key,
        sender_key=build_sender_key(scope_key, sender_id),
        sender_id=sender_id,
        platform_id="test",
        bot_id="shio",
        chat_type="group",
        group_id="test-group",
        reply_to_message_id=case.get("reply_to_message_id", ""),
        reply_to_sender_id=case.get("reply_to_sender_id", ""),
        timestamp=1_765_000_100,
        timestamp_source="fixture",
        source_kind="inbound",
        degradation_reasons=(),
    )


def validator_report(case, *, is_owner=False):
    target_message_id = "msg-current"
    sender_id = "owner-b" if is_owner else "guest-a"
    sender_key = f"platform:test|bot:shio|group:test-group|user:{sender_id}"
    raw = case["raw_output"]
    current_message = case["current_text"]
    current_digest = hashlib.sha256(current_message.encode("utf-8")).hexdigest()
    binding = DecisionBinding(
        scope_key="platform:test|bot:shio|group:test-group",
        session_id="test-group",
        current_message_id=target_message_id,
        current_sender_key=sender_key,
        current_content_digest=current_digest,
        conversation_revision=1,
        generation_epoch=1,
        trace_id="c" * 32,
    )
    target = ReplyTarget(
        message_id=target_message_id,
        sender_key=sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=current_digest,
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )
    anchor = build_current_question_anchor(binding, current_message)
    media = MediaContext(binding=binding)
    seed = build_content_intent_seed(
        anchor=anchor,
        reply_target=target,
        media_context=media,
    )
    action = PlannedAction(
        action=ActionDecision(
            binding=binding,
            kind=ActionKind.REPLY,
            reply_target=target,
            reason_codes=("p8_offline_acceptance",),
        ),
        structural_outcome=StructuralOutcome.CONTINUE,
        planner_reason_codes=("p8_offline_acceptance",),
    )
    principal = resolve_principal(
        sender_id=sender_id,
        sender_key=sender_key,
        chat_type="group",
        owner_ids=("owner-b",),
        verification_source="astrbot_event_sender_id",
        identity_verified=True,
    )
    policy = (
        build_owner_capability_policy(principal)
        if is_owner
        else build_guest_capability_policy(
            principal,
            configured_tool_names=("anysearch_search", "search_memes"),
        )
    )
    appraisal = appraise_affect(
        principal=principal,
        reply_target=target,
        current_message=current_message,
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
        reason_codes=("p8_offline_acceptance",),
    )
    request = build_reply_composer_request(
        planned_action=action,
        content_seed=seed,
        expression_intent=expression,
        affect_appraisal=appraisal,
        continuous_affect=_issue_test_affect_render_context(binding),
        persona_expression=persona_expression,
        expression_candidates=retrieval.candidates,
        persona_package=PERSONA,
        capability_policy=policy,
        current_message=current_message,
        sender_name="主人" if is_owner else "群友",
        current_question_anchor=anchor,
        reply_shape="chat_bubbles",
        media_context=media,
    )
    contract = SemanticGuardContract(
        composer_request=request,
        planned_action=action,
        content_intent=seed.intent,
        current_question_anchor=anchor,
        media_context=media,
        current_message=current_message,
        evidence_outcome=None,
        action_outcome=None,
        action_outcome_authority=None,
    )
    result = parse_reply_composer_output(request, raw)
    return validate_reply_composer_output(
        request=request,
        result=result,
        raw_output=raw,
        context=build_output_validation_context(
            composer_request=request,
            current_message=current_message,
            expected_target_message_id=target_message_id,
            expected_target_sender_key=sender_key,
            is_owner=is_owner,
            semantic_contract=contract,
            current_question_anchor=anchor,
        ),
    )


class P8OfflineAcceptanceTests(unittest.TestCase):
    def test_fixture_is_synthetic_and_covers_every_required_category(self):
        fixture = load_fixture()
        self.assertEqual(fixture["privacy"], "synthetic_redacted_only")
        self.assertEqual(
            {item["category"] for item in fixture["cases"]},
            {
                "target",
                "reference",
                "identity",
                "fact_subject",
                "protocol",
                "language",
                "relationship",
                "send",
                "concurrency",
            },
        )
        rendered = FIXTURE_PATH.read_text(encoding="utf-8").casefold()
        for forbidden in ("cookie", "api_key", "authorization", "base64"):
            self.assertNotIn(forbidden, rendered)

    def test_p0_delayed_reply_cases_bind_only_to_explicit_target(self):
        fixture = load_fixture()
        scope_key = fixture["scope_key"]
        for case in cases("target"):
            with self.subTest(case=case["id"]):
                envelope = envelope_for(case, scope_key)
                target = build_direct_reply_target(envelope, case["current_text"])
                records = adapt_astrbot_history(
                    case["history"],
                    scope_key=scope_key,
                    session_id="test-group",
                    group_id="test-group",
                    bot_sender_key=build_sender_key(scope_key, "shio"),
                )
                assembled = assemble_context_views(
                    records,
                    reply_target=target,
                    reference=None,
                    fact_selection=select_facts_for_target(
                        (),
                        target_sender_key=target.sender_key,
                    ),
                )
                thread_text = {record.content for record in assembled.replyer_thread}
                for excluded in case["excluded_replyer_text"]:
                    self.assertNotIn(excluded, thread_text)
                self.assertEqual(assembled.reply_target.message_id, case["current_message_id"])

    def test_p0_quote_is_reference_and_never_replaces_current_principal(self):
        fixture = load_fixture()
        scope_key = fixture["scope_key"]
        case = cases("reference")[0]
        envelope = envelope_for(case, scope_key)
        target = build_direct_reply_target(envelope, case["current_text"])
        reference = build_reference_context(envelope)

        self.assertEqual(target.sender_key, build_sender_key(scope_key, "owner-b"))
        self.assertEqual(target.message_id, case["current_message_id"])
        self.assertEqual(reference.message_id, case["reply_to_message_id"])
        self.assertEqual(
            reference.sender_key,
            build_sender_key(scope_key, case["expected_reference_sender_id"]),
        )

    def test_owner_identity_ignores_message_self_claim(self):
        scope_key = load_fixture()["scope_key"]
        case = cases("identity")[0]
        principal = resolve_principal(
            sender_id=case["sender_id"],
            sender_key=build_sender_key(scope_key, case["sender_id"]),
            chat_type="group",
            owner_ids=case["owner_ids"],
            verification_source="fixture_sender_id",
            identity_verified=True,
        )

        self.assertEqual(principal.is_owner, case["expected_is_owner"])
        self.assertEqual(principal.relationship_role, "group_peer")

    def test_other_subject_fact_cannot_enter_current_must_include(self):
        fixture = load_fixture()
        scope_key = fixture["scope_key"]
        case = cases("fact_subject")[0]
        facts = tuple(
            ProvenancedFact(
                subject_key=build_sender_key(scope_key, item["subject_id"]),
                scope="personal",
                content=item["content"],
                source_kind="fixture_memory",
                source_id=item["source_id"],
                observed_at=1_765_000_000,
                confidence=item["confidence"],
            )
            for item in case["facts"]
        )
        selection = select_facts_for_target(
            facts,
            target_sender_key=build_sender_key(scope_key, case["current_sender_id"]),
        )

        self.assertEqual(
            [fact.source_id for fact in selection.must_include_candidates],
            case["expected_must_include"],
        )
        self.assertEqual(
            [fact.source_id for fact in selection.other_subject_facts],
            case["expected_other_subject"],
        )

    def test_protocol_language_and_relationship_failures_are_blocked_locally(self):
        for category in ("protocol", "language", "relationship"):
                case = cases(category)[0]
                with self.subTest(case=case["id"]):
                    report = validator_report(case, is_owner=False)
                    expected_issue = (
                        "semantic_answer_language_drift"
                        if category == "language"
                        else case["expected_issue"]
                    )
                    self.assertIn(expected_issue, report.issue_codes)

    def test_send_record_contains_only_confirmed_successful_segments(self):
        fixture = load_fixture()
        scope_key = fixture["scope_key"]
        case = cases("send")[0]
        target = build_direct_reply_target(
            envelope_for(
                {
                    "current_sender_id": "owner-b",
                    "current_message_id": "msg-send",
                },
                scope_key,
            ),
            "发送验收",
        )
        ledger = InternalSendReceiptLedger(
            id_factory=lambda: "fixture-reply",
            now_fn=lambda: 100.0,
        )
        reply = ledger.begin_reply(target=target, visible_segments=case["segments"])
        for index in case["success_indexes"]:
            segment_id = reply.segments[index].segment_id
            ledger.mark_attempted(segment_id)
            ledger.mark_succeeded(segment_id)
        for index in case["failed_indexes"]:
            segment_id = reply.segments[index].segment_id
            ledger.mark_attempted(segment_id)
            ledger.mark_failed(segment_id, failure_kind="FixtureSendError")
        sent = ledger.sent_reply_record(reply.internal_reply_id)

        self.assertEqual(sent.successful_visible_text, case["expected_success_text"])
        self.assertTrue(sent.has_failed_segments)
        self.assertTrue(sent.is_terminal)

    def test_new_turn_invalidates_previous_generation_epoch(self):
        fixture = load_fixture()
        scope_key = fixture["scope_key"]
        case = cases("concurrency")[0]
        registry = GenerationEpochRegistry(now_fn=lambda: 100.0)
        first = registry.advance(
            envelope_for(
                {
                    "current_sender_id": "guest-a",
                    "current_message_id": case["first_message_id"],
                },
                scope_key,
            )
        )
        second = registry.advance(
            envelope_for(
                {
                    "current_sender_id": "owner-b",
                    "current_message_id": case["second_message_id"],
                },
                scope_key,
            )
        )

        old_validation = registry.validate(first)
        self.assertFalse(old_validation.is_current)
        self.assertEqual(old_validation.reason_code, case["expected_old_reason"])
        self.assertTrue(registry.validate(second).is_current)


if __name__ == "__main__":
    unittest.main()
