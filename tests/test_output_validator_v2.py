import unittest
import hashlib
import json
from pathlib import Path

from astrbot_plugin_shio.core.affect import appraise_affect
from astrbot_plugin_shio.core.affect_state import _issue_test_affect_render_context
from astrbot_plugin_shio.core.capability_policy import (
    build_guest_capability_policy,
    build_owner_capability_policy,
)
from astrbot_plugin_shio.core.content_intent_builder import build_content_intent_seed
from astrbot_plugin_shio.core.expression_retrieval import retrieve_expression_candidates
from astrbot_plugin_shio.core.identity import resolve_principal
from astrbot_plugin_shio.core.output_validator_v2 import (
    OutputDisposition,
    OutputValidationContext,
    build_output_validation_context,
    validate_reply_composer_output,
)
from astrbot_plugin_shio.core.context_assembler import (
    AssembledContext,
    FactSelection,
    ProvenancedFact,
    ReplyTarget,
)
from astrbot_plugin_shio.core.conversation_ledger import (
    LedgerRecord,
    LedgerRole,
    LedgerSourceKind,
    ledger_content_digest,
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
from astrbot_plugin_shio.core.current_question_anchor import (
    build_current_question_anchor,
)
from astrbot_plugin_shio.core.persona import load_persona_package
from astrbot_plugin_shio.core.persona_expression import build_persona_expression_plan
from astrbot_plugin_shio.core.reply_composer import (
    build_reply_composer_request,
    parse_reply_composer_output,
)
from astrbot_plugin_shio.core.repair_controller import (
    RepairAction,
    build_single_repair_request,
    decide_output_repair,
)
from astrbot_plugin_shio.core.semantic_guard import (
    SemanticGuardContract,
    SemanticGuardPhase,
)


ROOT = Path(__file__).parents[1]
PERSONA = load_persona_package(ROOT / "assets" / "personas" / "atri.json")


def validate(text, *, owner=False, context_kwargs=None, req=None):
    kwargs = dict(context_kwargs or {})
    message = kwargs.pop("current_message", "你觉得可以吗？")
    grounding_facts = tuple(kwargs.pop("grounding_facts", ()))
    forbidden_facts = tuple(kwargs.pop("forbidden_fact_fragments", ()))
    context_references = tuple(kwargs.pop("context_reference_fragments", ()))
    if req is None:
        req, anchor = anchored_request(
            message,
            owner=owner,
            grounding_facts=grounding_facts,
            forbidden_facts=forbidden_facts,
            context_references=context_references,
        )
    else:
        anchor = req.current_question_anchor
        if anchor is None:
            raise AssertionError("typed validator test requests require an anchor")
    result = parse_reply_composer_output(req, text)
    context = semantic_context(message, req, anchor, **kwargs)
    return validate_reply_composer_output(
        request=req,
        result=result,
        raw_output=text,
        context=context,
    )


def anchored_request(
    message,
    *,
    owner=False,
    grounding_facts=(),
    forbidden_facts=(),
    context_references=(),
    assistant_replies=(),
):
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    binding = DecisionBinding(
        scope_key="platform:test|bot:shio|group:synthetic",
        session_id="session-anchor-secret",
        current_message_id="message-anchor-secret",
        current_sender_key=(
            "scope-anchor-secret|user:owner-anchor-secret"
            if owner
            else "scope-anchor-secret|user:peer-anchor-secret"
        ),
        current_content_digest=digest,
        conversation_revision=1,
        generation_epoch=1,
        trace_id="c" * 32,
    )
    anchor = build_current_question_anchor(binding, message)
    principal = resolve_principal(
        sender_id=("owner-anchor-secret" if owner else "peer-anchor-secret"),
        sender_key=binding.current_sender_key,
        chat_type="group",
        owner_ids=("owner-anchor-secret",),
        verification_source="astrbot_event_sender_id",
        identity_verified=True,
    )
    target = ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )
    public_facts = tuple(
        ProvenancedFact(
            subject_key="",
            scope="public",
            content=str(content),
            source_kind="validator_test",
            source_id=hashlib.sha256(str(content).encode()).hexdigest()[:16],
            observed_at=1.0,
            confidence=0.9,
        )
        for content in grounding_facts
    )
    other_facts = tuple(
        ProvenancedFact(
            subject_key="scope-anchor-secret|user:other-anchor-secret",
            scope="personal",
            content=str(content),
            source_kind="validator_test",
            source_id=hashlib.sha256(str(content).encode()).hexdigest()[:16],
            observed_at=1.0,
            confidence=0.9,
        )
        for content in forbidden_facts
    )
    reference_records = tuple(
        LedgerRecord(
            sequence=index,
            source_kind=LedgerSourceKind.INBOUND,
            role=LedgerRole.USER,
            scope_key=binding.scope_key,
            session_id=binding.session_id,
            message_id=f"context-reference-{index}",
            sender_key=binding.current_sender_key,
            reply_to_message_id="",
            referenced_sender_key="",
            target_sender_key=binding.current_sender_key,
            timestamp=float(index),
            content=str(content),
            content_digest=ledger_content_digest(str(content)),
            source_id=f"validator-context-{index}",
            attribution_status="verified_sender",
        )
        for index, content in enumerate(context_references, start=1)
    )
    assistant_records = tuple(
        LedgerRecord(
            sequence=100 + index,
            source_kind=LedgerSourceKind.OUTBOUND,
            role=LedgerRole.ASSISTANT,
            scope_key=binding.scope_key,
            session_id=binding.session_id,
            message_id=f"assistant-recent-{index}",
            sender_key="scope-anchor-secret|assistant:shio",
            reply_to_message_id="",
            referenced_sender_key="",
            target_sender_key=binding.current_sender_key,
            timestamp=float(100 + index),
            content=str(content),
            content_digest=ledger_content_digest(str(content)),
            source_id=f"validator-assistant-{index}",
            attribution_status="verified_send_receipt",
        )
        for index, content in enumerate(assistant_replies, start=1)
    )
    assembled = None
    if public_facts or other_facts or reference_records or assistant_records:
        assembled = AssembledContext(
            reply_target=target,
            reference=None,
            planner_records=reference_records,
            replyer_thread=(*reference_records, *assistant_records),
            public_background=(),
            fact_selection=FactSelection(
                must_include_candidates=(),
                uncertain_target_facts=(),
                public_background=public_facts,
                other_subject_facts=other_facts,
            ),
        )
    planned_action = PlannedAction(
        action=ActionDecision(
            binding=binding,
            kind=ActionKind.REPLY,
            reply_target=target,
            reason_codes=("validator_test",),
        ),
        structural_outcome=StructuralOutcome.CONTINUE,
        planner_reason_codes=("validator_test",),
    )
    media = MediaContext(binding=binding)
    seed = build_content_intent_seed(
        anchor=anchor,
        reply_target=target,
        media_context=media,
    )
    policy = (
        build_owner_capability_policy(principal)
        if owner
        else build_guest_capability_policy(
            principal,
            configured_tool_names=("anysearch_search", "search_memes"),
        )
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
    expression_intent = ExpressionIntent(
        binding=binding,
        reply_target=target,
        modality=ExpressionModality.TEXT,
        social_act=persona_expression.topic_return,
        emotion_tags=(appraisal.trigger.value, appraisal.surface_emotion.value),
        max_bubbles=3,
        reason_codes=("validator_test",),
    )
    req = build_reply_composer_request(
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
        sender_name="主人" if owner else "群友",
        current_question_anchor=anchor,
        media_context=media,
        assembled_context=assembled,
    )
    return req, anchor


def semantic_context(message, req, anchor, **kwargs):
    binding = anchor.binding
    contract = SemanticGuardContract(
        composer_request=req,
        planned_action=req.planned_action,
        content_intent=req.content_seed.intent,
        current_question_anchor=anchor,
        media_context=req.media_context,
        current_message=message,
        evidence_outcome=None,
        action_outcome=None,
        action_outcome_authority=None,
    )
    return build_output_validation_context(
        composer_request=req,
        current_message=message,
        expected_target_message_id=kwargs.pop(
            "expected_target_message_id", req.target_message_id
        ),
        expected_target_sender_key=kwargs.pop(
            "expected_target_sender_key", req.target_sender_key
        ),
        is_owner=kwargs.pop("is_owner", req.capability_policy.is_owner),
        current_question_anchor=anchor,
        semantic_contract=contract,
        **kwargs,
    )


class OutputValidatorV2Tests(unittest.TestCase):
    def test_validation_context_requires_typed_contract_and_repr_is_private(self):
        message = "请解释 Docker 是什么。"
        req, anchor = anchored_request(message)
        with self.assertRaises(TypeError):
            OutputValidationContext(
                current_message="private current message",
                expected_target_message_id="private-message-id",
                expected_target_sender_key="private-sender-id",
                is_owner=False,
                semantic_contract=None,
                current_question_anchor=anchor,
            )

        context = semantic_context(message, req, anchor)
        rendered = repr(context)
        for forbidden in (
            message,
            req.target_message_id,
            req.target_sender_key,
            context.semantic_contract.current_message,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_typed_semantic_guard_is_part_of_validator_and_respects_phase(self):
        message = "不要启动旧服务。"
        req, anchor = anchored_request(message)
        wrong = "好，我现在把旧服务启动起来。"
        context = semantic_context(message, req, anchor)
        initial = validate_reply_composer_output(
            request=req,
            result=parse_reply_composer_output(req, wrong),
            raw_output=wrong,
            context=context,
            semantic_phase=SemanticGuardPhase.INITIAL,
        )
        permit = build_single_repair_request(
            original_request=req,
            rejected_visible_text=initial.validated_result.visible_text,
            report=initial,
            repair_attempts_used=0,
            semantic_contract=context.semantic_contract,
        )
        repaired_wrong = "好，我现在把旧服务启动起来。"
        report = validate_reply_composer_output(
            request=req,
            result=parse_reply_composer_output(req, repaired_wrong),
            raw_output=repaired_wrong,
            context=context,
            semantic_phase=SemanticGuardPhase.REPAIR,
            repair_request=permit,
        )

        self.assertIn("semantic_negation_drift", report.issue_codes)
        self.assertEqual(report.disposition, OutputDisposition.REPAIR_ONCE)
        self.assertIsNotNone(report.semantic_guard_report)
        self.assertIs(report.semantic_guard_report.phase, SemanticGuardPhase.REPAIR)
        self.assertEqual(
            report.trace_metadata()["semantic_guard_status"],
            "repair",
        )

    def test_plain_natural_reply_passes_without_style_requirements(self):
        report = validate("可以呀，我觉得这样挺合适的。")

        self.assertTrue(report.is_valid, report.issues)
        self.assertEqual(report.disposition, OutputDisposition.PASS)

    def test_mechanical_tone_missing_catchphrase_and_mild_repetition_are_not_severe(self):
        samples = (
            "结论是：可以。",
            "可以，我同意。可以，就这样吧。",
            "我知道了。",
        )

        for sample in samples:
            with self.subTest(sample=sample):
                report = validate(sample)
                self.assertTrue(report.is_valid, report.issues)

    def test_recent_fixed_opening_is_repairable_and_bound_to_canonical_history(self):
        message = "继续说吧"
        req, anchor = anchored_request(
            message,
            assistant_replies=(
                "才没有，我只是顺手看了一眼配置。",
                "才没有，我只是刚好发现了这个问题。",
            ),
        )
        text = "才没有，我只是想帮你把最后一步做完。"
        result = parse_reply_composer_output(req, text)
        report = validate_reply_composer_output(
            request=req,
            result=result,
            raw_output=text,
            context=semantic_context(message, req, anchor),
        )

        self.assertIn("dialogue_repetition", report.issue_codes)
        self.assertEqual(report.disposition, OutputDisposition.REPAIR_ONCE)
        permit = build_single_repair_request(
            original_request=req,
            rejected_visible_text=text,
            report=report,
            repair_attempts_used=0,
            semantic_contract=report.validation_context.semantic_contract,
        )
        self.assertIn("dialogue_repetition", permit.user_prompt)
        self.assertIn("must_change_visible_form", permit.user_prompt)

    def test_asset_phrase_repeat_is_repairable_but_explicit_quote_is_allowed(self):
        repeated_message = "这次呢？"
        repeated_req, repeated_anchor = anchored_request(
            repeated_message,
            assistant_replies=("我是高性能的嘛！上次已经验证好了。",),
        )
        repeated_text = "我是高性能的嘛！不过这次先看当前问题。"
        repeated = validate_reply_composer_output(
            request=repeated_req,
            result=parse_reply_composer_output(repeated_req, repeated_text),
            raw_output=repeated_text,
            context=semantic_context(
                repeated_message,
                repeated_req,
                repeated_anchor,
            ),
        )

        quote_message = "把刚才那句原样再说一遍"
        quote_req, quote_anchor = anchored_request(
            quote_message,
            assistant_replies=("我是高性能的嘛！",),
        )
        quoted_text = "我是高性能的嘛！"
        quoted = validate_reply_composer_output(
            request=quote_req,
            result=parse_reply_composer_output(quote_req, quoted_text),
            raw_output=quoted_text,
            context=semantic_context(quote_message, quote_req, quote_anchor),
        )

        self.assertIn("dialogue_repetition", repeated.issue_codes)
        self.assertNotIn("dialogue_repetition", quoted.issue_codes)

    def test_dynamic_role_phrase_repeat_needs_no_core_blacklist(self):
        message = "再来一次"
        req, anchor = anchored_request(
            message,
            assistant_replies=("哼哼，我可是高性能机器人！",),
        )
        text = "这次当然也难不倒高性能机器人啦。"
        report = validate_reply_composer_output(
            request=req,
            result=parse_reply_composer_output(req, text),
            raw_output=text,
            context=semantic_context(message, req, anchor),
        )

        self.assertIn("dialogue_repetition", report.issue_codes)

    def test_raw_protocol_and_reasoning_leak_are_repairable(self):
        raw = "<|channel|>thought 我先分析一下 <|channel|>final 可以呀。"
        report = validate(raw)

        self.assertIn("tool_protocol_leak", report.issue_codes)
        self.assertIn("internal_reasoning_leak", report.issue_codes)
        self.assertEqual(report.disposition, OutputDisposition.REPAIR_ONCE)

    def test_nonowner_identity_and_intimacy_are_rejected_but_owner_is_allowed(self):
        identity = validate("既然主人这么说了，那我就收下啦。")
        intimacy = validate("不过……mua回去一下也不是不行啦。")
        owner = validate("不过……mua回去一下也不是不行啦。", owner=True)

        self.assertIn("nonowner_identity_confusion", identity.issue_codes)
        self.assertIn("nonowner_relationship_escalation", intimacy.issue_codes)
        self.assertNotIn("nonowner_relationship_escalation", owner.issue_codes)

    def test_unexpected_language_switch_is_severe_but_requested_translation_is_allowed(self):
        unexpected = validate(
            "I will answer only in English because that is easier for me.",
            context_kwargs={"current_message": "你觉得可以吗？"},
        )
        requested = validate(
            "This configuration is valid.",
            context_kwargs={"current_message": "请用英文回答。"},
        )

        self.assertIn("semantic_answer_language_drift", unexpected.issue_codes)
        self.assertNotIn("semantic_answer_language_drift", requested.issue_codes)

    def test_unsupported_experience_and_current_market_claim_are_rejected(self):
        experience = validate("我昨天去电影院看了这部电影，感觉很不错。")
        market = validate("比特币现在价格是 100000 美元。")

        self.assertIn("unsupported_personal_experience", experience.issue_codes)
        self.assertIn("unsupported_current_fact", market.issue_codes)

    def test_grounding_fact_can_support_matching_personal_experience(self):
        report = validate(
            "我昨天去电影院看了这部电影。",
            context_kwargs={
                "grounding_facts": ("角色自我事实：昨天去电影院看了这部电影",)
            },
        )

        self.assertNotIn("unsupported_personal_experience", report.issue_codes)

    def test_forbidden_other_subject_fact_fragments_are_enforced(self):
        context = {
            "forbidden_fact_fragments": ("另一个用户刚做完手术",),
        }
        forbidden = validate(
            "可以，不过另一个用户刚做完手术。",
            context_kwargs=context,
        )

        self.assertIn("forbidden_fact_fragment", forbidden.issue_codes)

    def test_anchor_coverage_is_telemetry_and_natural_paraphrase_is_not_blocked(self):
        message = "请解释 Docker 和 Kubernetes 的差别。"
        req, anchor = anchored_request(message)
        text = "一个负责单机容器运行，另一个负责集群编排。"
        result = parse_reply_composer_output(req, text)
        report = validate_reply_composer_output(
            request=req,
            result=result,
            raw_output=text,
            context=semantic_context(message, req, anchor),
        )

        self.assertTrue(report.is_valid, report.issues)
        self.assertIsNotNone(report.anchor_coverage)
        self.assertFalse(report.anchor_coverage.current_topic_supported)
        self.assertEqual(report.disposition, OutputDisposition.PASS)

    def test_obvious_context_drift_is_repairable_and_trace_is_content_free(self):
        message = "请解释 Docker 和 Kubernetes 的差别。"
        wrong = "蛋糕需要先把鸡蛋和面粉搅拌均匀。"
        req, anchor = anchored_request(
            message,
            context_references=(wrong,),
        )
        result = parse_reply_composer_output(req, wrong)
        report = validate_reply_composer_output(
            request=req,
            result=result,
            raw_output=wrong,
            context=semantic_context(
                message,
                req,
                anchor,
            ),
        )

        self.assertIn("current_question_context_drift", report.issue_codes)
        self.assertEqual(report.disposition, OutputDisposition.REPAIR_ONCE)
        rendered = json.dumps(report.trace_metadata(), ensure_ascii=False)
        for forbidden in (
            message,
            wrong,
            req.target_message_id,
            req.target_sender_key,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_action_only_request_may_answer_from_memory_without_false_drift(self):
        message = "告诉我你记得的内容"
        remembered = "你喜欢海边，也喜欢在晚上散步。"
        req, anchor = anchored_request(
            message,
            context_references=(remembered,),
        )
        result = parse_reply_composer_output(req, remembered)
        report = validate_reply_composer_output(
            request=req,
            result=result,
            raw_output=remembered,
            context=semantic_context(
                message,
                req,
                anchor,
            ),
        )

        self.assertTrue(report.is_valid, report.issues)
        self.assertNotIn("current_question_context_drift", report.issue_codes)

    def test_anchor_binding_mismatch_is_blocking(self):
        message = "请解释 Docker。"
        req, anchor = anchored_request(message)
        other_req, other_anchor = anchored_request("请解释 Python。")
        result = parse_reply_composer_output(req, "Docker 是容器运行平台。")
        with self.assertRaises(ValueError):
            build_output_validation_context(
                composer_request=req,
                current_message=message,
                expected_target_message_id=req.target_message_id,
                expected_target_sender_key=req.target_sender_key,
                is_owner=False,
                semantic_contract=semantic_context(
                    message, req, anchor
                ).semantic_contract,
                current_question_anchor=other_anchor,
            )

    def test_empty_or_target_binding_mismatch_is_blocking(self):
        empty = validate("")
        with self.assertRaises(ValueError):
            validate(
                "可以呀。",
                context_kwargs={"expected_target_message_id": "message-b"},
            )

        self.assertIn("empty_visible_reply", empty.issue_codes)
        self.assertEqual(empty.disposition, OutputDisposition.REPAIR_ONCE)

    def test_generic_validator_does_not_encode_character_style(self):
        source = (ROOT / "core" / "output_validator_v2.py").read_text(
            encoding="utf-8"
        )

        for token in ("亚托莉", "ATRI", "高性能机器人", "才没有", "校准失误"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
