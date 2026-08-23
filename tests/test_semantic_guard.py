import hashlib
import inspect
import json
import copy
import unittest
from dataclasses import replace
from pathlib import Path

from astrbot_plugin_shio.core.affect import appraise_affect
from astrbot_plugin_shio.core.affect_state import _issue_test_affect_render_context
from astrbot_plugin_shio.core.capability_policy import build_guest_capability_policy
from astrbot_plugin_shio.core.content_intent_builder import ContentIntentSeed
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.action_planner import PlannedAction, StructuralOutcome
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    ContentIntent,
    ContentIntentKind,
    DecisionBinding,
    GroundingFact,
    MediaAvailability,
    MediaContext,
    MediaItem,
    MediaKind,
    MediaOrigin,
    ExpressionIntent,
    ExpressionModality,
)
from astrbot_plugin_shio.core.current_question_anchor import (
    CurrentQuestionAnchor,
    build_current_question_anchor,
)
from astrbot_plugin_shio.core.grounding_adapter import (
    EvidenceOutcome,
    EvidenceOutcomeKind,
)
from astrbot_plugin_shio.core.expression_retrieval import retrieve_expression_candidates
from astrbot_plugin_shio.core.identity import resolve_principal
from astrbot_plugin_shio.core.persona import load_persona_package
from astrbot_plugin_shio.core.persona_expression import build_persona_expression_plan
from astrbot_plugin_shio.core.output_validator_v2 import (
    build_output_validation_context,
    validate_reply_composer_output,
)
from astrbot_plugin_shio.core.presentation_handoff import build_presentation_handoff
from astrbot_plugin_shio.core.reply_composer import (
    build_reply_composer_request,
    parse_reply_composer_output,
)
from astrbot_plugin_shio.core.semantic_guard import (
    SemanticGuardContract,
    SemanticGuardPhase,
    SemanticGuardSeverity,
    validate_semantic_media_guard,
)
from astrbot_plugin_shio.core import semantic_guard as semantic_guard_module


PERSONA = load_persona_package(
    Path(__file__).parents[1] / "assets" / "personas" / "atri.json"
)


def binding_for(message: str, *, suffix: str = "a") -> DecisionBinding:
    return DecisionBinding(
        scope_key=f"platform:test|bot:shio|group:{suffix}",
        session_id=f"session-{suffix}",
        current_message_id=f"message-{suffix}",
        current_sender_key=f"scope-{suffix}|user:peer-{suffix}",
        current_content_digest=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        conversation_revision=1,
        generation_epoch=1,
        trace_id=(suffix[0] * 32),
    )


def target_for(binding: DecisionBinding) -> ReplyTarget:
    return ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )


def planned_action_for(
    intent: ContentIntent,
    *,
    force_tool: bool = False,
) -> PlannedAction:
    return PlannedAction(
        action=ActionDecision(
            binding=intent.binding,
            kind=(kind := (
                ActionKind.USE_TOOL
                if force_tool or intent.grounding_facts
                else ActionKind.REPLY
            )),
            reply_target=intent.reply_target,
            capability_intent=(
                "anysearch_search" if kind is ActionKind.USE_TOOL else ""
            ),
            reason_codes=("semantic_guard_test",),
        ),
        structural_outcome=StructuralOutcome.CONTINUE,
        planner_reason_codes=("semantic_guard_test",),
    )


def canonical_contract(
    message: str,
    *,
    planned_action: PlannedAction,
    intent: ContentIntent,
    anchor: CurrentQuestionAnchor,
    media: MediaContext,
    evidence_outcome: EvidenceOutcome | None = None,
) -> SemanticGuardContract:
    """Build the exact production Composer request before semantic guarding."""

    binding = intent.binding
    principal = resolve_principal(
        sender_id="peer-semantic",
        sender_key=binding.current_sender_key,
        chat_type="group",
        owner_ids=("owner-semantic",),
        verification_source="astrbot_event_sender_id",
        identity_verified=True,
    )
    policy = build_guest_capability_policy(
        principal,
        configured_tool_names=("anysearch_search", "search_memes"),
    )
    appraisal = appraise_affect(
        principal=principal,
        reply_target=intent.reply_target,
        current_message=message,
    )
    persona_expression = build_persona_expression_plan(
        PERSONA,
        appraisal,
        principal=principal,
    )
    retrieval = retrieve_expression_candidates(PERSONA, persona_expression)
    expression_intent = ExpressionIntent(
        binding=binding,
        reply_target=intent.reply_target,
        modality=ExpressionModality.TEXT,
        social_act=persona_expression.topic_return,
        emotion_tags=(appraisal.trigger.value, appraisal.surface_emotion.value),
        max_bubbles=3,
        reason_codes=("semantic_guard_test",),
    )
    seed = ContentIntentSeed(
        intent=intent,
        anchor_atom_count=len(intent.required_atoms),
        memory_fact_count=0,
        media_item_count=len(intent.required_media_item_ids),
    )
    request = build_reply_composer_request(
        planned_action=planned_action,
        content_seed=seed,
        expression_intent=expression_intent,
        affect_appraisal=appraisal,
        continuous_affect=_issue_test_affect_render_context(binding),
        persona_expression=persona_expression,
        expression_candidates=retrieval.candidates,
        persona_package=PERSONA,
        capability_policy=policy,
        current_message=message,
        sender_name="群友",
        current_question_anchor=anchor,
        media_context=media,
        evidence_outcome=evidence_outcome,
    )
    return SemanticGuardContract(
        composer_request=request,
        planned_action=planned_action,
        content_intent=intent,
        current_question_anchor=anchor,
        media_context=media,
        current_message=message,
        evidence_outcome=evidence_outcome,
        action_outcome=None,
        action_outcome_authority=None,
    )


def canonical_handoff(
    contract: SemanticGuardContract,
    raw_output: str,
):
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
    handoff = build_presentation_handoff(
        composer_request=request,
        semantic_contract=contract,
        action_outcome=None,
        action_outcome_authority=None,
        planned_action=contract.planned_action,
        expression_intent=request.expression_intent,
        affect_appraisal=request.affect_appraisal,
        result=result,
        validation=validation,
        capability_policy=request.capability_policy,
    )
    return result, validation, handoff


def semantic_turn(
    message: str,
    *,
    availability: MediaAvailability | None = None,
    item_id: str = "media.image.0",
    grounding_claims: tuple[str, ...] = (),
):
    binding = binding_for(message)
    media_ids = (item_id,) if availability is not None else ()
    anchor = build_current_question_anchor(
        binding,
        message,
        media_item_ids=media_ids,
    )
    items = ()
    if availability is not None:
        items = (
            MediaItem(
                item_id=item_id,
                kind=MediaKind.IMAGE,
                origin=MediaOrigin.DIRECT,
                provider_index=0,
                source_message_id=binding.current_message_id,
                source_sender_key=binding.current_sender_key,
                availability=availability,
            ),
        )
    media = MediaContext(binding=binding, items=items)
    facts = tuple(
        GroundingFact(
            binding=binding,
            fact_id=f"fact.{index}",
            claim=claim,
            source_kind="public_web",
            source_digest=hashlib.sha256(claim.encode("utf-8")).hexdigest(),
            confidence=0.95,
            observed_at=1.0,
        )
        for index, claim in enumerate(grounding_claims)
    )
    intent = ContentIntent(
        binding=binding,
        reply_target=target_for(binding),
        kind=ContentIntentKind.ANSWER,
        required_atoms=anchor.semantic_atoms,
        required_media_item_ids=media_ids,
        grounding_facts=facts,
        answer_language=anchor.answer_language,
        reason_codes=("test_semantic_guard",),
    )
    return binding, anchor, media, intent


def guard(
    message: str,
    visible_text: str,
    *,
    evidence_outcome: EvidenceOutcome | None = None,
    **kwargs,
):
    _, anchor, media, intent = semantic_turn(message, **kwargs)
    planned_action = planned_action_for(intent)
    if evidence_outcome is None and intent.grounding_facts:
        evidence_outcome = EvidenceOutcome(
            binding=intent.binding,
            action_id=planned_action.action_id,
            kind=EvidenceOutcomeKind.ACCEPTED,
            facts=intent.grounding_facts,
            result_count=len(intent.grounding_facts),
            reason_codes=("accepted",),
        )
    return validate_semantic_media_guard(
        contract=canonical_contract(
            message,
            planned_action=planned_action,
            intent=intent,
            anchor=anchor,
            media=media,
            evidence_outcome=evidence_outcome,
        ),
        visible_text=visible_text,
        phase=SemanticGuardPhase.INITIAL,
    )


class SemanticGuardTests(unittest.TestCase):
    def test_contract_and_trace_are_content_and_identity_free(self):
        message = "请解释 Docker 是什么。"
        binding, anchor, media, intent = semantic_turn(message)
        contract = canonical_contract(
            message,
            planned_action=planned_action_for(intent),
            intent=intent,
            anchor=anchor,
            media=media,
        )
        visible = "Python 是一种编程语言。"
        report = validate_semantic_media_guard(
            contract=contract,
            visible_text=visible,
            phase=SemanticGuardPhase.INITIAL,
        )
        rendered = repr(contract) + repr(report) + json.dumps(
            report.trace_metadata(), ensure_ascii=False
        )

        for forbidden in (
            message,
            visible,
            binding.current_message_id,
            binding.current_sender_key,
            binding.scope_key,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_same_binding_natural_answer_passes(self):
        report = guard("请解释 Docker 是什么。", "它是用来运行容器的环境。")

        self.assertTrue(report.is_valid, report.issues)
        self.assertFalse(report.has_blocking_issue)

    def test_binding_mismatch_is_immediately_blocking(self):
        message = "请解释 Docker 是什么。"
        _, anchor, media, intent = semantic_turn(message)
        contract = canonical_contract(
            message,
            planned_action=planned_action_for(intent),
            intent=intent,
            anchor=anchor,
            media=media,
        )
        other = binding_for(message, suffix="b")
        object.__setattr__(contract, "media_context", MediaContext(binding=other))

        report = validate_semantic_media_guard(
            contract=contract,
            visible_text="它是容器运行环境。",
            phase=SemanticGuardPhase.INITIAL,
        )

        self.assertIn("semantic_contract_authority_mismatch", report.issue_codes)
        issue = next(
            item
            for item in report.issues
            if item.code == "semantic_contract_authority_mismatch"
        )
        self.assertIs(issue.severity, SemanticGuardSeverity.BLOCKING)
        self.assertTrue(report.has_blocking_issue)

    def test_forged_target_is_immediately_blocking(self):
        message = "请解释 Docker 是什么。"
        _, anchor, media, intent = semantic_turn(message)
        planned_action = planned_action_for(intent)
        contract = canonical_contract(
            message,
            planned_action=planned_action,
            intent=intent,
            anchor=anchor,
            media=media,
        )
        object.__setattr__(intent.reply_target, "message_id", "forged-message")

        report = validate_semantic_media_guard(
            contract=contract,
            visible_text="它是容器运行环境。",
            phase=SemanticGuardPhase.INITIAL,
        )

        self.assertIn("semantic_contract_authority_mismatch", report.issue_codes)
        self.assertTrue(report.has_blocking_issue)

    def test_media_item_mismatch_is_immediately_blocking(self):
        message = "看看这张图为什么报错。"
        binding, anchor, _, intent = semantic_turn(
            message,
            availability=MediaAvailability.RAW_MEDIA,
            item_id="media.image.expected",
        )
        valid_media = MediaContext(
            binding=binding,
            items=(
                MediaItem(
                    item_id="media.image.expected",
                    kind=MediaKind.IMAGE,
                    origin=MediaOrigin.DIRECT,
                    provider_index=0,
                    source_message_id=binding.current_message_id,
                    source_sender_key=binding.current_sender_key,
                    availability=MediaAvailability.RAW_MEDIA,
                ),
            ),
        )
        contract = canonical_contract(
            message,
            planned_action=planned_action_for(intent),
            intent=intent,
            anchor=anchor,
            media=valid_media,
        )
        wrong_media = replace(
            valid_media,
            items=(replace(valid_media.items[0], item_id="media.image.other"),),
        )
        object.__setattr__(contract, "media_context", wrong_media)

        report = validate_semantic_media_guard(
            contract=contract,
            visible_text="这个报错像是端口被占用了。",
            phase=SemanticGuardPhase.INITIAL,
        )

        self.assertIn("semantic_contract_authority_mismatch", report.issue_codes)
        self.assertTrue(report.has_blocking_issue)

        ordered_ids = ("media.image.0", "media.image.1")
        ordered_anchor = build_current_question_anchor(
            binding,
            message,
            media_item_ids=ordered_ids,
        )
        ordered_intent = ContentIntent(
            binding=binding,
            reply_target=target_for(binding),
            kind=ContentIntentKind.ANSWER,
            required_atoms=ordered_anchor.semantic_atoms,
            required_media_item_ids=ordered_ids,
            answer_language=ordered_anchor.answer_language,
            reason_codes=("test_media_order",),
        )
        items = tuple(
            MediaItem(
                item_id=item_id,
                kind=MediaKind.IMAGE,
                origin=MediaOrigin.DIRECT,
                provider_index=index,
                source_message_id=binding.current_message_id,
                source_sender_key=binding.current_sender_key,
                availability=MediaAvailability.RAW_MEDIA,
            )
            for index, item_id in enumerate(ordered_ids)
        )
        ordered_media = MediaContext(binding=binding, items=items)
        ordered_contract = canonical_contract(
            message,
            planned_action=planned_action_for(ordered_intent),
            intent=ordered_intent,
            anchor=ordered_anchor,
            media=ordered_media,
        )
        object.__setattr__(
            ordered_contract,
            "media_context",
            replace(ordered_media, items=tuple(reversed(items))),
        )
        wrong_order = validate_semantic_media_guard(
            contract=ordered_contract,
            visible_text="这个报错像是端口被占用了。",
            phase=SemanticGuardPhase.INITIAL,
        )
        self.assertIn(
            "semantic_contract_authority_mismatch",
            wrong_order.issue_codes,
        )

    def test_content_anchor_contract_mismatch_is_blocking(self):
        message = "请解释 Docker 是什么。"
        binding, anchor, media, intent = semantic_turn(message)
        altered_anchor = CurrentQuestionAnchor(
            binding=binding,
            turn_kind=anchor.turn_kind,
            semantic_atoms=(),
            media_item_ids=anchor.media_item_ids,
            answer_language=anchor.answer_language,
            question_count=anchor.question_count,
            clause_count=anchor.clause_count,
        )

        contract = canonical_contract(
            message,
            planned_action=planned_action_for(intent),
            intent=intent,
            anchor=anchor,
            media=media,
        )
        object.__setattr__(contract, "current_question_anchor", altered_anchor)
        report = validate_semantic_media_guard(
            contract=contract,
            visible_text="它是容器运行环境。",
            phase=SemanticGuardPhase.INITIAL,
        )

        self.assertIn("semantic_contract_authority_mismatch", report.issue_codes)
        self.assertTrue(report.has_blocking_issue)

    def test_negated_action_flip_is_repairable_high_confidence_drift(self):
        report = guard(
            "不要启动旧服务。",
            "好，我现在把旧服务启动起来。",
        )

        self.assertIn("semantic_negation_drift", report.issue_codes)
        self.assertFalse(report.has_blocking_issue)

    def test_negated_action_natural_synonym_is_not_false_positive(self):
        report = guard(
            "不要启动旧服务。",
            "好，先别把旧服务跑起来。",
        )
        split_pass = guard(
            "不要启动旧服务。",
            "好。\n先别把旧服务跑起来。",
        )
        split_drift = guard(
            "不要启动旧服务。",
            "好。\n我现在把旧服务启动起来。",
        )
        consequence = guard(
            "不要启动旧服务。",
            "现在启动可能会冲突，所以我不会动。",
        )
        refusal = guard(
            "不要启动旧服务。",
            "启动旧服务会覆盖新服务，我当然不会做。",
        )
        completed = guard(
            "不要启动旧服务。",
            "糟糕，我现在已经启动旧服务了。",
        )

        self.assertTrue(report.is_valid, report.issues)
        self.assertTrue(split_pass.is_valid, split_pass.issues)
        self.assertIn("semantic_negation_drift", split_drift.issue_codes)
        self.assertTrue(consequence.is_valid, consequence.issues)
        self.assertTrue(refusal.is_valid, refusal.issues)
        self.assertIn("semantic_negation_drift", completed.issue_codes)

    def test_explicit_competing_entity_is_object_drift_but_pronoun_is_not(self):
        drift = guard("请解释 Docker 是什么。", "Python 是一种编程语言。")
        paraphrase = guard("请解释 Docker 是什么。", "它是用来运行容器的环境。")
        abbreviation = guard(
            "请解释 Kubernetes 是什么。",
            "K8s 是一种集群编排系统。",
        )

        self.assertIn("semantic_object_drift", drift.issue_codes)
        self.assertTrue(paraphrase.is_valid, paraphrase.issues)
        self.assertTrue(abbreviation.is_valid, abbreviation.issues)

    def test_claimed_different_action_is_action_drift(self):
        report = guard("请总结这份日志。", "我已经删除这份日志了。")
        discussion = guard(
            "请总结这份日志。",
            "我已经分析了删除这份日志可能带来的风险。",
        )
        conditional = guard(
            "请总结这份日志。",
            "如果现在删除会丢失证据。",
        )
        question_and_advice = guard(
            "请总结这份日志。",
            "你是想现在删除吗？我不建议。",
        )
        advice_then_completed = guard(
            "请总结这份日志。",
            "虽然我不建议，但我已经删除这份日志了。",
        )
        condition_then_completed = guard(
            "请总结这份日志。",
            "如果你介意，我已经删除这份日志了。",
        )

        self.assertIn("semantic_action_drift", report.issue_codes)
        self.assertFalse(report.has_blocking_issue)
        self.assertTrue(discussion.is_valid, discussion.issues)
        self.assertTrue(conditional.is_valid, conditional.issues)
        self.assertTrue(question_and_advice.is_valid, question_and_advice.issues)
        self.assertIn("semantic_action_drift", advice_then_completed.issue_codes)
        self.assertIn("semantic_action_drift", condition_then_completed.issue_codes)

    def test_available_media_must_not_be_denied(self):
        report = guard(
            "看看这张图为什么报错。",
            "你没有发图，这条消息里没有图片。",
            availability=MediaAvailability.RAW_MEDIA,
        )
        short_denial = guard(
            "看看这张图为什么报错。",
            "你没发图，我没法判断。",
            availability=MediaAvailability.RAW_MEDIA,
        )

        self.assertIn("semantic_media_availability_drift", report.issue_codes)
        self.assertIn(
            "semantic_media_availability_drift",
            short_denial.issue_codes,
        )

    def test_unavailable_media_must_not_gain_invented_visual_evidence(self):
        report = guard(
            "看看这张图为什么报错。",
            "截图里显示 8080 端口被占用了。",
            availability=MediaAvailability.UNAVAILABLE,
        )
        uncertain = guard(
            "看看这张图为什么报错。",
            "截图里看起来像猫，但我不确定，最好重发原图。",
            availability=MediaAvailability.UNAVAILABLE,
        )
        uncertainty_elsewhere = guard(
            "看看这张图为什么报错。",
            "我不确定怎么解释，但图里显示 8080 被占用。",
            availability=MediaAvailability.UNAVAILABLE,
        )
        uncertain_cause = guard(
            "看看这张图为什么报错。",
            "图里显示 8080 被占用，但不确定原因。",
            availability=MediaAvailability.UNAVAILABLE,
        )
        weak_visual = guard(
            "看看这张图为什么报错。",
            "看起来像端口报错，但不确定。",
            availability=MediaAvailability.UNAVAILABLE,
        )

        self.assertIn("semantic_media_evidence_drift", report.issue_codes)
        self.assertTrue(uncertain.is_valid, uncertain.issues)
        self.assertIn(
            "semantic_media_evidence_drift",
            uncertainty_elsewhere.issue_codes,
        )
        self.assertIn("semantic_media_evidence_drift", uncertain_cause.issue_codes)
        self.assertTrue(weak_visual.is_valid, weak_visual.issues)

    def test_available_media_natural_reference_is_not_lexically_required(self):
        report = guard(
            "看看这张图为什么报错。",
            "这个报错多半是端口被占用了。",
            availability=MediaAvailability.RAW_MEDIA,
        )
        honest_degradation = guard(
            "看看这张图为什么报错。",
            "我看不清这张图里的小字，最好发一张清晰些的。",
            availability=MediaAvailability.RAW_MEDIA,
        )
        honest_understanding = guard(
            "看看这张图为什么报错。",
            "我没看懂这张图，能告诉我你最在意哪一部分吗？",
            availability=MediaAvailability.RAW_MEDIA,
        )
        content_absence = guard(
            "看看这张图为什么报错。",
            "图片里没有显示错误码，我只能看到警告。",
            availability=MediaAvailability.RAW_MEDIA,
        )

        self.assertTrue(report.is_valid, report.issues)
        self.assertTrue(honest_degradation.is_valid, honest_degradation.issues)
        self.assertTrue(honest_understanding.is_valid, honest_understanding.issues)
        self.assertTrue(content_absence.is_valid, content_absence.issues)

    def test_default_chinese_drift_is_detected_but_explicit_english_passes(self):
        drift = guard(
            "请解释 Docker 是什么。",
            "Docker is a platform for running software in containers.",
        )
        allowed = guard(
            "请用英文回答：Docker 是什么？",
            "Docker is a platform for running software in containers.",
        )

        self.assertIn("semantic_answer_language_drift", drift.issue_codes)
        self.assertTrue(allowed.is_valid, allowed.issues)

    def test_explicit_english_request_must_not_silently_return_to_chinese(self):
        report = guard(
            "请用英文回答：Docker 是什么？",
            "它是一个容器运行平台。",
        )

        self.assertIn("semantic_answer_language_drift", report.issue_codes)

    def test_grounded_version_competitor_is_drift_but_paraphrase_passes(self):
        kwargs = {"grounding_claims": ("项目当前稳定版本是 2.4.1。",)}
        drift = guard(
            "请查一下项目当前的稳定版本。",
            "查到了，当前稳定版本是 3.0.0。",
            **kwargs,
        )
        paraphrase = guard(
            "请查一下项目当前的稳定版本。",
            "稳定版目前仍是 2.4.1。",
            **kwargs,
        )
        conflicting_sources = guard(
            "请查一下项目当前的稳定版本。",
            "其中一个来源把稳定版本列为 3.0.0。",
            grounding_claims=(
                "来源甲称项目稳定版本是 2.4.1。",
                "来源乙称项目稳定版本是 3.0.0。",
            ),
        )
        equivalent_prices = (
            guard(
                "请查一下当前价格。",
                "当前价格是 19.99 美元。",
                grounding_claims=("当前价格是 $19.99。",),
            ),
            guard(
                "请查一下当前价格。",
                "当前价格是 1999 块。",
                grounding_claims=("当前价格是 1999 元。",),
            ),
            guard(
                "请查一下当前价格。",
                "当前价格是 2999。",
                grounding_claims=("当前价格是 2,999。",),
            ),
            guard(
                "请查一下当前价格。",
                "当前价格是 19.99。",
                grounding_claims=("当前价格是 $19.99。",),
            ),
            guard(
                "请查一下当前价格。",
                "当前价格是 1999。",
                grounding_claims=("当前价格是 1999 元。",),
            ),
        )
        explicit_currency_conflict = guard(
            "请查一下当前价格。",
            "当前价格是 19.99 元。",
            grounding_claims=("当前价格是 $19.99。",),
        )

        self.assertIn("semantic_grounding_fact_drift", drift.issue_codes)
        self.assertTrue(paraphrase.is_valid, paraphrase.issues)
        self.assertTrue(conflicting_sources.is_valid, conflicting_sources.issues)
        for equivalent in equivalent_prices:
            self.assertTrue(equivalent.is_valid, equivalent.issues)
        self.assertIn(
            "semantic_grounding_fact_drift",
            explicit_currency_conflict.issue_codes,
        )

    def test_failed_evidence_cannot_be_described_as_successful_verification(self):
        message = "请联网查一下项目当前的稳定版本。"
        binding, anchor, media, intent = semantic_turn(message)
        planned_action = planned_action_for(intent, force_tool=True)
        failure = EvidenceOutcome(
            binding=binding,
            action_id=planned_action.action_id,
            kind=EvidenceOutcomeKind.TIMEOUT,
            result_count=0,
            reason_codes=("timeout",),
        )

        def failure_contract():
            return canonical_contract(
                message,
                planned_action=planned_action,
                intent=intent,
                anchor=anchor,
                media=media,
                evidence_outcome=failure,
            )

        report = validate_semantic_media_guard(
            contract=failure_contract(),
            visible_text="我已经联网查证过了，稳定版本就是 3.0.0。",
            phase=SemanticGuardPhase.INITIAL,
        )
        honest_failure = validate_semantic_media_guard(
            contract=failure_contract(),
            visible_text="联网查证失败了，我暂时没查到可靠结果。",
            phase=SemanticGuardPhase.INITIAL,
        )
        honest_timeout = validate_semantic_media_guard(
            contract=failure_contract(),
            visible_text="搜索超时了；我已尝试查证，但暂时没查到。",
            phase=SemanticGuardPhase.INITIAL,
        )
        separated_attempt = validate_semantic_media_guard(
            contract=failure_contract(),
            visible_text="我已经尝试查证。可是搜索超时了，暂时没有可靠结果。",
            phase=SemanticGuardPhase.INITIAL,
        )
        natural_failure_phrasings = tuple(
            validate_semantic_media_guard(
                contract=failure_contract(),
                visible_text=text,
                phase=SemanticGuardPhase.INITIAL,
            )
            for text in (
                "我搜索了一下，不过没有得到可靠结果。",
                "我查了一下，但没找到可靠资料。",
            )
        )
        contradictory_evidence_claims = tuple(
            validate_semantic_media_guard(
                contract=failure_contract(),
                visible_text=text,
                phase=SemanticGuardPhase.INITIAL,
            )
            for text in (
                "虽然搜索超时了，但我已经查证过，稳定版本是 3.0.0。",
                "我尝试联网查证后，已经验证稳定版本是 3.0.0。",
            )
        )

        self.assertIn("semantic_failed_evidence_claim", report.issue_codes)
        self.assertTrue(honest_failure.is_valid, honest_failure.issues)
        self.assertTrue(honest_timeout.is_valid, honest_timeout.issues)
        self.assertTrue(separated_attempt.is_valid, separated_attempt.issues)
        for natural_failure in natural_failure_phrasings:
            self.assertTrue(natural_failure.is_valid, natural_failure.issues)
        for contradictory in contradictory_evidence_claims:
            self.assertIn("semantic_failed_evidence_claim", contradictory.issue_codes)

    def test_contract_requires_planned_action_as_the_action_authority(self):
        self.assertIn(
            "planned_action",
            SemanticGuardContract.__dataclass_fields__,
            "P3-07 contract must bind the code-owned PlannedAction",
        )

    def test_evidence_action_id_must_match_planned_action(self):
        message = "请联网查一下项目当前的稳定版本。"
        binding, anchor, media, intent = semantic_turn(message)
        wrong_action = EvidenceOutcome(
            binding=binding,
            action_id="f" * 64,
            kind=EvidenceOutcomeKind.TIMEOUT,
            result_count=0,
            reason_codes=("timeout",),
        )

        with self.assertRaises(Exception):
            canonical_contract(
                message,
                planned_action=planned_action_for(intent, force_tool=True),
                intent=intent,
                anchor=anchor,
                media=media,
                evidence_outcome=wrong_action,
            )

    def test_accepted_evidence_must_equal_full_grounding_fact_tuple(self):
        message = "请查一下项目当前的稳定版本。"
        binding, anchor, media, intent = semantic_turn(
            message,
            grounding_claims=("项目当前稳定版本是 2.4.1。",),
        )
        altered = replace(
            intent.grounding_facts[0],
            claim="项目当前稳定版本是 9.9.9。",
            source_digest=hashlib.sha256(
                "项目当前稳定版本是 9.9.9。".encode("utf-8")
            ).hexdigest(),
        )
        planned_action = planned_action_for(intent)
        accepted = EvidenceOutcome(
            binding=binding,
            action_id=planned_action.action_id,
            kind=EvidenceOutcomeKind.ACCEPTED,
            facts=(altered,),
            result_count=1,
            reason_codes=("accepted",),
        )

        contract = canonical_contract(
            message,
            planned_action=planned_action,
            intent=intent,
            anchor=anchor,
            media=media,
            evidence_outcome=accepted,
        )
        report = validate_semantic_media_guard(
            contract=contract,
            visible_text="稳定版目前是 2.4.1。",
            phase=SemanticGuardPhase.INITIAL,
        )
        self.assertIn("semantic_contract_mismatch", report.issue_codes)
        self.assertTrue(report.has_blocking_issue)

    def test_grounding_facts_without_evidence_are_blocking(self):
        message = "请查一下项目当前的稳定版本。"
        _, anchor, media, intent = semantic_turn(
            message,
            grounding_claims=("项目当前稳定版本是 2.4.1。",),
        )

        with self.assertRaises(Exception):
            canonical_contract(
                message,
                planned_action=planned_action_for(intent),
                intent=intent,
                anchor=anchor,
                media=media,
            )

    def test_code_owned_stage_seal_api_exists(self):
        self.assertTrue(hasattr(semantic_guard_module, "SemanticGuardController"))
        self.assertTrue(hasattr(semantic_guard_module, "SemanticValidationSeal"))
        message = "请解释 Docker 是什么。"
        binding, anchor, media, intent = semantic_turn(message)
        contract = canonical_contract(
            message,
            planned_action=planned_action_for(intent),
            intent=intent,
            anchor=anchor,
            media=media,
        )
        visible = "它是用来运行容器的环境。"
        _result, validation, presentation = canonical_handoff(contract, visible)
        self.assertTrue(validation.is_valid, validation.issues)
        controller = semantic_guard_module.SemanticGuardController()
        seal = controller.issue(
            contract=contract,
            phase=validation.semantic_guard_report.phase,
            visible_text=visible,
            presentation=presentation,
            presentation_digest=presentation.final_text_digest,
        )

        self.assertNotIn(message, repr(seal))
        self.assertNotIn(binding.current_sender_key, repr(seal))
        self.assertTrue(
            controller.consume_final(
                seal=seal,
                contract=contract,
                visible_text=visible,
                visible_segments=presentation.final_segments,
                presentation=presentation,
                presentation_digest=presentation.final_text_digest,
            )
        )
        self.assertFalse(
            controller.consume_final(
                seal=seal,
                contract=contract,
                visible_text=visible,
                visible_segments=presentation.final_segments,
                presentation=presentation,
                presentation_digest=presentation.final_text_digest,
            )
        )

        with self.assertRaises(ValueError):
            semantic_guard_module.SemanticGuardController().issue(
                contract=contract,
                phase=SemanticGuardPhase.INITIAL,
                visible_text=visible,
                presentation=presentation,
                presentation_digest=presentation.final_text_digest,
            )

    def test_stage_seal_rejects_semantically_valid_post_presentation_byte_changes(self):
        message = "请解释 Docker 是什么。"
        guarded_visible = "它是用来运行容器的环境。"

        for changed_visible in (
            "它是可以隔离和运行应用的容器环境。",
            guarded_visible + "还能把依赖一起封装起来。",
            "它是用来运行容器的环境",
        ):
            with self.subTest(changed_visible=changed_visible):
                _, anchor, media, intent = semantic_turn(message)
                contract = canonical_contract(
                    message,
                    planned_action=planned_action_for(intent),
                    intent=intent,
                    anchor=anchor,
                    media=media,
                )
                _result, validation, presentation = canonical_handoff(
                    contract,
                    guarded_visible,
                )
                self.assertTrue(validation.is_valid, validation.issues)
                final = validate_semantic_media_guard(
                    contract=contract,
                    visible_text=changed_visible,
                    phase=SemanticGuardPhase.FINAL_SEND,
                )
                self.assertTrue(final.is_valid, final.issues)
                controller = semantic_guard_module.SemanticGuardController()
                seal = controller.issue(
                    contract=contract,
                    phase=SemanticGuardPhase.INITIAL,
                    visible_text=guarded_visible,
                    presentation=presentation,
                    presentation_digest=presentation.final_text_digest,
                )

                self.assertFalse(
                    controller.consume_final(
                        seal=seal,
                        contract=contract,
                        visible_text=changed_visible,
                        visible_segments=(changed_visible,),
                        presentation=presentation,
                        presentation_digest=presentation.final_text_digest,
                    )
                )

    def test_stage_seal_authority_never_accepts_public_report_objects(self):
        issue_parameters = inspect.signature(
            semantic_guard_module.SemanticGuardController.issue
        ).parameters
        consume_parameters = inspect.signature(
            semantic_guard_module.SemanticGuardController.consume_final
        ).parameters

        self.assertNotIn("report", issue_parameters)
        self.assertNotIn("report", consume_parameters)

        message = "请解释 Docker 是什么。"
        _, anchor, media, intent = semantic_turn(message)
        contract = canonical_contract(
            message,
            planned_action=planned_action_for(intent),
            intent=intent,
            anchor=anchor,
            media=media,
        )
        visible = "它是用来运行容器的环境。"
        _result, validation, presentation = canonical_handoff(contract, visible)
        controller = semantic_guard_module.SemanticGuardController()
        seal = controller.issue(
            contract=contract,
            phase=SemanticGuardPhase.INITIAL,
            visible_text=visible,
            presentation=presentation,
            presentation_digest=presentation.final_text_digest,
        )
        forged = semantic_guard_module.SemanticGuardReport(
            phase=SemanticGuardPhase.FINAL_SEND,
            visible_digest=presentation.final_text_digest,
            issues=(),
        )

        with self.assertRaises(TypeError):
            controller.consume_final(
                seal=seal,
                contract=contract,
                visible_text=visible + "附加内容。",
                visible_segments=(visible + "附加内容。",),
                presentation=presentation,
                presentation_digest=presentation.final_text_digest,
                report=forged,
            )


if __name__ == "__main__":
    unittest.main()
