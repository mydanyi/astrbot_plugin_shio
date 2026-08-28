from __future__ import annotations

import dataclasses
import hashlib
import unittest
from types import SimpleNamespace

from astrbot_plugin_shio.core.action_planner import PlannedAction, StructuralOutcome
from astrbot_plugin_shio.core.capability_policy import (
    CapabilityClass,
    SideEffectClass,
    ToolClassification,
    build_guest_capability_policy,
    classify_tool,
)
from astrbot_plugin_shio.core.content_intent_builder import (
    attach_grounding_facts,
    build_content_intent_seed,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.contracts import (
    ActionDecision,
    ActionKind,
    DecisionBinding,
    KnowledgeGapDecision,
    KnowledgeNeed,
    ContractViolation,
)
from astrbot_plugin_shio.core.current_question_anchor import (
    build_current_question_anchor,
)
from astrbot_plugin_shio.core.grounding_adapter import (
    EvidenceOutcomeKind,
    adapt_grounding_evidence,
)
from astrbot_plugin_shio.core.identity import PrincipalContext
from astrbot_plugin_shio.core.tool_broker import (
    AcquisitionRequest,
    broker_tool_request,
    build_search_request_shape,
)
from astrbot_plugin_shio.core.tool_result import (
    ToolResultVisibility,
    adapt_tool_call_results,
)


def _binding(
    *,
    revision: int = 1,
    epoch: int = 7,
    sender: str = "peer-a",
    message: str | None = None,
) -> DecisionBinding:
    message = message or f"请查证合成术语-{revision}"
    scope = "platform:test|bot:shio|group:grounding"
    return DecisionBinding(
        scope_key=scope,
        session_id="session-grounding",
        current_message_id=f"message-grounding-{revision}",
        current_sender_key=f"{scope}|user:{sender}",
        current_content_digest=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        conversation_revision=revision,
        generation_epoch=epoch,
        trace_id=f"{revision:032x}",
    )


def _target(binding: DecisionBinding) -> ReplyTarget:
    return ReplyTarget(
        message_id=binding.current_message_id,
        sender_key=binding.current_sender_key,
        session_id=binding.session_id,
        scope_key=binding.scope_key,
        content_digest=binding.current_content_digest,
        source_kind="inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )


def _runtime_tool(name: str = "anysearch_search") -> SimpleNamespace:
    parameter = "query" if name == "anysearch_search" else "url"
    return SimpleNamespace(
        name=name,
        description="Public web search",
        parameters={"type": "object", "properties": {parameter: {"type": "string"}}},
        metadata={},
        handler_module_path="data.plugins.astrbot_plugin_anysearch.main",
        active=True,
    )


def _request(
    binding: DecisionBinding | None = None,
    *,
    now: float = 100.0,
    query: str = "合成术语是什么意思",
):
    binding = binding or _binding()
    tool = _runtime_tool()
    classification: ToolClassification = classify_tool(tool)
    principal = PrincipalContext(
        sender_key=binding.current_sender_key,
        sender_id=binding.current_sender_key.rsplit("|user:", 1)[-1],
        is_owner=False,
        relationship_role="group_peer",
        verification_source="astrbot_event_sender_id:owner_allowlist_miss",
    )
    policy = build_guest_capability_policy(
        principal,
        configured_tool_names=("anysearch_search",),
    )
    gap = KnowledgeGapDecision(
        binding=binding,
        need=KnowledgeNeed.UNKNOWN_TERM,
        requires_evidence=True,
        requested_capability=CapabilityClass.PUBLIC_WEB_READ,
        max_tool_calls=1,
    )
    action = PlannedAction(
        action=ActionDecision(
            binding=binding,
            kind=ActionKind.USE_TOOL,
            reply_target=_target(binding),
            capability_intent=CapabilityClass.PUBLIC_WEB_READ.value,
            reason_codes=("fixture_tool",),
        ),
        structural_outcome=StructuralOutcome.CONTINUE,
        planner_reason_codes=("fixture_tool",),
    )
    return (
        broker_tool_request(
            planned_action=action,
            knowledge_gap=gap,
            capability_policy=policy,
            runtime_tools=(classification,),
            configured_tool_names=("anysearch_search",),
            request_shape=build_search_request_shape(binding, query=query),
            now=now,
        ),
        tool,
    )


def _batch(
    content,
    *,
    call_id: str = "call-current",
    result_call_id: str | None = None,
    name: str = "anysearch_search",
    arguments=None,
    is_error: bool = False,
):
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments={"query": "must-not-survive"} if arguments is None else arguments,
        ),
    )
    result = SimpleNamespace(
        id="result-current",
        tool_call_id=result_call_id if result_call_id is not None else call_id,
        content=content,
        is_error=is_error,
    )
    return SimpleNamespace(
        tool_calls_info=SimpleNamespace(tool_calls=[call]),
        tool_calls_result=[result],
    )


def _typed(
    request: AcquisitionRequest,
    tool: SimpleNamespace,
    content,
    *,
    observed_at: float = 105.0,
    raw=None,
):
    return adapt_tool_call_results(
        raw
        if raw is not None
        else _batch(content, arguments=request.materialize_arguments()),
        tools=(tool,),
        scope_key=request.binding.scope_key,
        target_sender_key=request.binding.current_sender_key,
        acquisition_request=request,
        observed_at=observed_at,
    )


class GroundingAdapterTests(unittest.TestCase):
    def test_bound_result_requires_code_clock(self):
        request, tool = _request()

        with self.assertRaisesRegex(ContractViolation, "tool_result_clock_required"):
            adapt_tool_call_results(
                _batch(
                    "result",
                    arguments=request.materialize_arguments(),
                ),
                tools=(tool,),
                scope_key=request.binding.scope_key,
                target_sender_key=request.binding.current_sender_key,
                acquisition_request=request,
            )

    def test_attested_result_becomes_stable_same_turn_grounding_facts(self):
        request, tool = _request()
        content = {
            "results": [
                {"title": "术语解释", "snippet": "这是一个合成测试术语。"},
                {"answer": "它只用于验证公开资料取证链。"},
            ],
            "debug": {"api_key": "sk-private-token-must-not-survive"},
        }
        typed = _typed(request, tool, content)

        outcome = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=request.binding,
            now=105.0,
        )
        repeated = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=request.binding,
            now=105.0,
        )

        self.assertIs(outcome.kind, EvidenceOutcomeKind.ACCEPTED)
        self.assertGreaterEqual(len(outcome.facts), 2)
        self.assertTrue(all(fact.binding == request.binding for fact in outcome.facts))
        self.assertTrue(all(len(fact.source_digest) == 64 for fact in outcome.facts))
        self.assertEqual(
            tuple(fact.fact_id for fact in outcome.facts),
            tuple(fact.fact_id for fact in repeated.facts),
        )
        self.assertEqual(len(typed[0].content_digest), 64)
        self.assertEqual(len(typed[0].call_arguments_digest), 64)
        self.assertTrue(typed[0].arguments_attested)

    def test_unrelated_search_claims_never_become_grounding_facts(self):
        message = "醒醒起床，让我检查一下身体，看看有没有修好"
        request, tool = _request(
            _binding(message=message),
            query=message,
        )
        typed = _typed(
            request,
            tool,
            {
                "results": [
                    {"snippet": "可以一起预约治疗师，因为抑郁会影响思维。"},
                    {"snippet": "能够帮助她的方法可能是刺激身体活动。"},
                ]
            },
        )

        outcome = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=request.binding,
            now=105.0,
        )

        self.assertIs(outcome.kind, EvidenceOutcomeKind.IRRELEVANT_RESULT)
        self.assertEqual(outcome.facts, ())

    def test_relevant_search_claims_survive_relevance_gate(self):
        cases = (
            (
                "请查证合成术语是什么意思",
                {"answer": "合成测试术语只用于验证公开资料取证链。"},
            ),
            (
                "查一下北京明天的天气",
                {"answer": "北京明天天气预计有雨。"},
            ),
            (
                "查一下北京明天会不会下雨",
                {"answer": "北京明日预计有雨。"},
            ),
            (
                "你知道显卡吗？",
                {"answer": "显卡负责处理图形计算任务。"},
            ),
        )
        for index, (message, content) in enumerate(cases, start=20):
            with self.subTest(message=message):
                request, tool = _request(
                    _binding(revision=index, message=message),
                    query=message,
                )
                outcome = adapt_grounding_evidence(
                    request=request,
                    results=_typed(request, tool, content),
                    current_binding=request.binding,
                    now=105.0,
                )
                self.assertIs(outcome.kind, EvidenceOutcomeKind.ACCEPTED)
                self.assertTrue(outcome.facts)

    def test_content_intent_accepts_grounding_without_semantic_rewrite(self):
        request, tool = _request()
        typed = _typed(request, tool, {"answer": "公开资料给出了定义。"})
        outcome = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=request.binding,
            now=105.0,
        )
        message = "请查证合成术语-1"
        seed = build_content_intent_seed(
            anchor=build_current_question_anchor(request.binding, message),
            reply_target=_target(request.binding),
        )

        grounded = attach_grounding_facts(seed, outcome.facts)

        self.assertEqual(grounded.intent.grounding_facts, outcome.facts)
        self.assertEqual(grounded.intent.required_atoms, seed.intent.required_atoms)
        self.assertEqual(grounded.intent.answer_language, seed.intent.answer_language)

    def test_json_protocol_url_path_and_tokens_never_enter_claim_repr_or_trace(self):
        request, tool = _request()
        secret = "sk-this-token-is-private"
        content = {
            "results": [
                {
                    "title": "公开定义 https://example.invalid/private",
                    "snippet": "结论保留，文件 C:\\private\\notes.txt 不保留。",
                    "text": "<channel>thought</channel><channel>final</channel> 可验证说明。",
                },
                {"answer": f"api_key={secret} 这个字段不能出现。"},
            ],
            "arguments": {"query": "must-not-survive"},
        }
        typed = _typed(request, tool, content)

        outcome = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=request.binding,
            now=105.0,
        )
        rendered = "\n".join(fact.claim for fact in outcome.facts)
        diagnostics = repr(typed) + repr(outcome) + repr(outcome.trace_metadata())

        for forbidden in (
            "https://",
            "C:\\private",
            secret,
            "api_key",
            "arguments",
            "<channel>",
            "must-not-survive",
            "call-current",
            "result-current",
        ):
            self.assertNotIn(forbidden, rendered + diagnostics)
        self.assertNotIn("{", rendered)
        self.assertNotIn("}", rendered)

    def test_missing_or_orphan_call_result_has_zero_facts(self):
        request, tool = _request()
        missing_and_orphan = _typed(
            request,
            tool,
            "ignored",
            raw=_batch(
                "orphan content",
                result_call_id="different-call",
                arguments=request.materialize_arguments(),
            ),
        )

        outcome = adapt_grounding_evidence(
            request=request,
            results=missing_and_orphan,
            current_binding=request.binding,
            now=105.0,
        )

        self.assertIn(
            outcome.kind,
            {EvidenceOutcomeKind.MISSING_RESULT, EvidenceOutcomeKind.ORPHAN_RESULT},
        )
        self.assertEqual(outcome.facts, ())

    def test_error_and_timeout_are_honest_zero_fact_outcomes(self):
        request, tool = _request(now=100.0)
        error = _typed(
            request,
            tool,
            "provider failed",
            raw=_batch(
                "provider failed",
                arguments=request.materialize_arguments(),
                is_error=True,
            ),
        )
        late = _typed(request, tool, {"answer": "late"}, observed_at=request.selection.deadline + 0.1)

        error_outcome = adapt_grounding_evidence(
            request=request,
            results=error,
            current_binding=request.binding,
            now=105.0,
        )
        late_outcome = adapt_grounding_evidence(
            request=request,
            results=late,
            current_binding=request.binding,
            now=request.selection.deadline + 0.1,
        )

        self.assertIs(error_outcome.kind, EvidenceOutcomeKind.TOOL_ERROR)
        self.assertIs(late_outcome.kind, EvidenceOutcomeKind.TIMEOUT)
        self.assertEqual(error_outcome.facts, ())
        self.assertEqual(late_outcome.facts, ())

        before_request = _typed(request, tool, {"answer": "impossible"}, observed_at=99.0)
        impossible_outcome = adapt_grounding_evidence(
            request=request,
            results=before_request,
            current_binding=request.binding,
            now=105.0,
        )
        self.assertIs(impossible_outcome.kind, EvidenceOutcomeKind.INVALID_RESULT)
        self.assertEqual(impossible_outcome.facts, ())

    def test_actual_arguments_must_exactly_match_brokered_shape(self):
        request, tool = _request()
        mutations = (
            {"query": "tampered query"},
            {"query": "合成术语是什么意思", "extra": "not allowed"},
            {},
            '{"query":"tampered string payload"}',
        )
        for arguments in mutations:
            with self.subTest(arguments_type=type(arguments).__name__, arguments=arguments):
                typed = _typed(
                    request,
                    tool,
                    "apparently valid evidence",
                    raw=_batch("apparently valid evidence", arguments=arguments),
                )
                outcome = adapt_grounding_evidence(
                    request=request,
                    results=typed,
                    current_binding=request.binding,
                    now=105.0,
                )
                self.assertIs(outcome.kind, EvidenceOutcomeKind.ARGUMENT_MISMATCH)
                self.assertEqual(outcome.facts, ())

        matching_json = _typed(
            request,
            tool,
            "合成术语的有效公开证据",
            raw=_batch(
                "合成术语的有效公开证据",
                arguments='{"query":"合成术语是什么意思"}',
            ),
        )
        accepted = adapt_grounding_evidence(
            request=request,
            results=matching_json,
            current_binding=request.binding,
            now=105.0,
        )
        self.assertIs(accepted.kind, EvidenceOutcomeKind.ACCEPTED)

    def test_duplicate_selected_call_never_produces_grounding(self):
        request, tool = _request()
        first = _batch(
            "first",
            call_id="call-first",
            arguments=request.materialize_arguments(),
        )
        second = _batch(
            "second",
            call_id="call-second",
            arguments=request.materialize_arguments(),
        )
        typed = _typed(request, tool, "ignored", raw=[first, second])

        outcome = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=request.binding,
            now=105.0,
        )

        self.assertIs(outcome.kind, EvidenceOutcomeKind.MULTIPLE_RESULTS)
        self.assertEqual(outcome.facts, ())

    def test_stale_current_binding_or_result_epoch_never_crosses_turns(self):
        request, tool = _request()
        typed = _typed(request, tool, {"answer": "current evidence"})
        next_turn = _binding(revision=2, epoch=8)

        next_outcome = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=next_turn,
            now=105.0,
        )
        wrong_epoch_result = dataclasses.replace(
            typed[0],
            binding=_binding(revision=1, epoch=8),
        )
        epoch_outcome = adapt_grounding_evidence(
            request=request,
            results=(wrong_epoch_result,),
            current_binding=request.binding,
            now=105.0,
        )

        self.assertIs(next_outcome.kind, EvidenceOutcomeKind.STALE_BINDING)
        self.assertIs(epoch_outcome.kind, EvidenceOutcomeKind.STALE_BINDING)
        self.assertEqual(next_outcome.facts, ())
        self.assertEqual(epoch_outcome.facts, ())

        other_sender = _binding(sender="peer-b")
        sender_outcome = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=other_sender,
            now=105.0,
        )
        self.assertIs(sender_outcome.kind, EvidenceOutcomeKind.OTHER_SENDER)
        self.assertEqual(sender_outcome.facts, ())

    def test_wrong_sender_scope_action_shape_or_deadline_is_rejected(self):
        request, tool = _request()
        result = _typed(request, tool, {"answer": "evidence"})[0]
        mutations = (
            ("target_sender_key", request.binding.current_sender_key + "-other", EvidenceOutcomeKind.OTHER_SENDER),
            ("scope_key", request.binding.scope_key + "-other", EvidenceOutcomeKind.BINDING_MISMATCH),
            ("action_id", "f" * 64, EvidenceOutcomeKind.ACTION_MISMATCH),
            ("request_shape_digest", "e" * 64, EvidenceOutcomeKind.ACTION_MISMATCH),
            ("acquisition_deadline", request.selection.deadline + 1, EvidenceOutcomeKind.DEADLINE_MISMATCH),
        )
        for field, value, expected in mutations:
            with self.subTest(field=field):
                outcome = adapt_grounding_evidence(
                    request=request,
                    results=(dataclasses.replace(result, **{field: value}),),
                    current_binding=request.binding,
                    now=105.0,
                )
                self.assertIs(outcome.kind, expected)
                self.assertEqual(outcome.facts, ())

    def test_wrong_tool_capability_source_or_visibility_is_rejected(self):
        request, tool = _request()
        result = _typed(request, tool, {"answer": "evidence"})[0]
        mutations = (
            ("tool_name", "anysearch_extract", EvidenceOutcomeKind.TOOL_MISMATCH),
            ("capability", CapabilityClass.CHAT_RETRIEVAL, EvidenceOutcomeKind.CAPABILITY_MISMATCH),
            ("side_effect", SideEffectClass.SCOPED_READ, EvidenceOutcomeKind.CAPABILITY_MISMATCH),
            ("tool_source", "unattested.clone", EvidenceOutcomeKind.SOURCE_MISMATCH),
            ("source_attested", False, EvidenceOutcomeKind.SOURCE_MISMATCH),
            ("source_kind", "untrusted_result_source", EvidenceOutcomeKind.SOURCE_MISMATCH),
            ("visibility", ToolResultVisibility.INTERNAL_ONLY, EvidenceOutcomeKind.VISIBILITY_DENIED),
        )
        for field, value, expected in mutations:
            with self.subTest(field=field):
                outcome = adapt_grounding_evidence(
                    request=request,
                    results=(dataclasses.replace(result, **{field: value}),),
                    current_binding=request.binding,
                    now=105.0,
                )
                self.assertIs(outcome.kind, expected)
                self.assertEqual(outcome.facts, ())

    def test_empty_or_protocol_only_content_produces_no_fact(self):
        request, tool = _request()
        typed = _typed(
            request,
            tool,
            '<channel>thought</channel>{"arguments":{"query":"secret"}}',
        )

        outcome = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=request.binding,
            now=105.0,
        )

        self.assertIs(outcome.kind, EvidenceOutcomeKind.UNSAFE_CONTENT)
        self.assertEqual(outcome.facts, ())

    def test_outcome_and_typed_result_diagnostics_hide_identity_and_payload(self):
        request, tool = _request()
        secret = "private-result-payload"
        typed = _typed(request, tool, {"answer": secret})
        outcome = adapt_grounding_evidence(
            request=request,
            results=typed,
            current_binding=request.binding,
            now=105.0,
        )

        rendered = repr(typed[0]) + repr(outcome) + repr(outcome.trace_metadata())

        for forbidden in (
            secret,
            request.binding.current_sender_key,
            request.binding.scope_key,
            request.action_id,
            "call-current",
            "result-current",
        ):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
