from __future__ import annotations

import asyncio
import dataclasses
import json
import unittest
from types import SimpleNamespace

from astrbot_plugin_shio.core.contracts import (
    ExternalPluginEvidence,
    HistoryVisibility,
    IngressDecision,
    IngressDisposition,
    MemoryDecision,
    MemoryMode,
    PluginEvidenceStatus,
    SenderKind,
)
from astrbot_plugin_shio.core.conversation_event import (
    ConversationRevisionBook,
    build_ingress_event,
)
from astrbot_plugin_shio.core.identity import (
    PrincipalContext,
    TurnEnvelope,
    build_scope_key,
    build_sender_key,
)
from astrbot_plugin_shio.core.ingress_admission import (
    IngressAdmissionController,
)
from astrbot_plugin_shio.core.memory_policy import (
    MemoryPolicy,
    MemoryPolicyIneligible,
    MemoryPolicyResult,
)
from astrbot_plugin_shio.core.plugin_adapters.livingmemory import (
    EXPECTED_PLUGIN_NAME,
    LivingMemoryAdapter,
    LivingMemoryAdapterResult,
    LivingMemoryReadRequest,
)


def accepted_turn(*, chat_type: str = "group", owner: bool = False):
    session_id = "session-private" if chat_type == "private" else "session-group"
    group_id = "group-7" if chat_type == "group" else ""
    sender_id = "owner-1" if owner else "peer-1"
    scope_key = build_scope_key(
        platform_id="test-platform",
        bot_id="test-bot",
        chat_type=chat_type,
        group_id=group_id,
        session_id=session_id,
    )
    sender_key = build_sender_key(scope_key, sender_id)
    envelope = TurnEnvelope(
        session_id=session_id,
        message_id=f"message-{chat_type}-{'owner' if owner else 'peer'}",
        scope_key=scope_key,
        sender_key=sender_key,
        sender_id=sender_id,
        platform_id="test-platform",
        bot_id="test-bot",
        chat_type=chat_type,
        group_id=group_id,
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=100.0,
        timestamp_source="synthetic",
        source_kind="inbound",
        degradation_reasons=(),
    )
    principal = PrincipalContext(
        sender_key=sender_key,
        sender_id=sender_id,
        is_owner=owner,
        relationship_role="owner" if owner else f"{chat_type}_peer",
        verification_source="synthetic_structural_id",
    )
    revision_book = ConversationRevisionBook()
    ingress = build_ingress_event(
        envelope=envelope,
        principal=principal,
        sender_kind=SenderKind.HUMAN,
        content="当前问题必须优先",
        revision_candidate=revision_book.peek(scope_key),
    )
    controller = IngressAdmissionController(revision_book)
    admission = controller.admit(
        ingress,
        gate_observation=controller.issue_gate_observation(
            ingress,
            status=PluginEvidenceStatus.VERIFIED,
            banned=False,
        ),
    )
    assert admission.conversation_event is not None
    return admission.decision, admission.conversation_event


def recent_row(event, content: str, **overrides):
    row = {
        "id": "recent-1",
        "session_id": event.envelope.session_id,
        "role": "user",
        "content": content,
        "sender_id": event.envelope.sender_id,
        "timestamp": 90.0,
        "confidence": 0.9,
        "relevance": 0.9,
        "metadata": {},
    }
    row.update(overrides)
    return row


def provided_request(results, *, extra_text: str = "", prompt: str = "当前问题"):
    contexts = []
    if results is not None:
        contexts.append(
            {
                "role": "tool",
                "name": "recall_long_term_memory",
                "tool_call_id": "fake_recall_synthetic",
                "content": json.dumps({"results": results}, ensure_ascii=False),
            }
        )
    parts = [SimpleNamespace(text=extra_text)] if extra_text else []
    return SimpleNamespace(
        contexts=contexts,
        extra_user_content_parts=parts,
        prompt=prompt,
    )


class CountingReader:
    def __init__(self, value=(), error: BaseException | None = None):
        self.value = value
        self.error = error
        self.calls: list[LivingMemoryReadRequest] = []

    async def __call__(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.value


class MemoryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_private_owner_peer_each_get_one_idempotent_decision_and_evidence(self):
        for chat_type in ("group", "private"):
            for owner in (False, True):
                with self.subTest(chat_type=chat_type, owner=owner):
                    admission, event = accepted_turn(
                        chat_type=chat_type,
                        owner=owner,
                    )
                    reader = CountingReader(
                        [recent_row(event, "合成的近期偏好")]
                    )
                    adapter = LivingMemoryAdapter.verified(reader=reader)
                    policy = MemoryPolicy()

                    first = await policy.decide(
                        admission,
                        event,
                        adapter=adapter,
                        include_recent=True,
                        semantic_required=False,
                    )
                    second = await policy.decide(
                        admission,
                        event,
                        adapter=adapter,
                        include_recent=True,
                        semantic_required=False,
                    )

                    self.assertIs(first, second)
                    self.assertIsInstance(first, MemoryPolicyResult)
                    self.assertIsInstance(first.decision, MemoryDecision)
                    self.assertIsInstance(
                        first.plugin_evidence,
                        ExternalPluginEvidence,
                    )
                    self.assertIs(first.decision.mode, MemoryMode.RECENT_ONLY)
                    self.assertIs(
                        first.plugin_evidence.status,
                        PluginEvidenceStatus.VERIFIED,
                    )
                    self.assertTrue(first.plugin_evidence.trusted)
                    self.assertIs(
                        first.plugin_evidence.history_visibility,
                        HistoryVisibility.CURRENT_TURN_REFERENCE,
                    )
                    self.assertEqual(len(reader.calls), 1)
                    self.assertEqual(first.read_count, 1)
                    self.assertTrue(first.current_message_precedence)
                    self.assertEqual(first.context_order[0], "current_message")
                    self.assertEqual(first.chat_type, chat_type)
                    self.assertEqual(
                        first.relationship_role,
                        event.principal.relationship_role,
                    )

    async def test_concurrent_reentry_is_single_flight_and_returns_one_result(self):
        admission, event = accepted_turn()

        class SlowReader(CountingReader):
            async def __call__(self, request):
                self.calls.append(request)
                await asyncio.sleep(0)
                return self.value

        reader = SlowReader([recent_row(event, "并发下只读取一次")])
        adapter = LivingMemoryAdapter.verified(reader=reader)
        policy = MemoryPolicy()

        results = await asyncio.gather(
            *(
                policy.decide(
                    admission,
                    event,
                    adapter=adapter,
                    include_recent=True,
                )
                for _ in range(4)
            )
        )

        self.assertTrue(all(result is results[0] for result in results))
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(results[0].read_count, 1)

    async def test_provided_semantic_recall_and_recent_merge_dedupe_under_one_read(self):
        admission, event = accepted_turn()
        reader = CountingReader(
            [
                recent_row(event, "喜欢海边", id="recent-sea"),
                recent_row(event, "近期正在学游泳", id="recent-swim"),
            ]
        )
        provided = provided_request(
            [
                {
                    "id": "semantic-sea",
                    "sender_id": event.envelope.sender_id,
                    "content": "喜欢海边",
                    "confidence": 0.95,
                    "score": 0.95,
                    "scope": "personal",
                },
                {
                    "id": "semantic-color",
                    "sender_id": event.envelope.sender_id,
                    "content": "偏爱蓝色",
                    "confidence": 0.92,
                    "score": 0.91,
                    "scope": "personal",
                },
            ]
        )

        result = await MemoryPolicy().decide(
            admission,
            event,
            adapter=LivingMemoryAdapter.verified(reader=reader),
            provided_recall=provided,
            include_recent=True,
            semantic_required=True,
            max_results=5,
        )

        contents = [fact.content for fact in result.decision.selected_facts]
        self.assertIs(result.decision.mode, MemoryMode.SEMANTIC_RECALL)
        self.assertEqual(contents.count("喜欢海边"), 1)
        self.assertIn("近期正在学游泳", contents)
        self.assertIn("偏爱蓝色", contents)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(result.read_count, 1)
        self.assertGreaterEqual(result.provided_record_count, 2)

    async def test_skip_emits_one_verified_evidence_without_read(self):
        admission, event = accepted_turn(chat_type="private")
        reader = CountingReader(error=AssertionError("skip must not read"))

        result = await MemoryPolicy().decide(
            admission,
            event,
            adapter=LivingMemoryAdapter.verified(reader=reader),
            include_recent=False,
            semantic_required=False,
        )

        self.assertIs(result.decision.mode, MemoryMode.SKIP)
        self.assertEqual(result.decision.selected_facts, ())
        self.assertEqual(result.read_count, 0)
        self.assertEqual(reader.calls, [])
        self.assertIs(
            result.plugin_evidence.status,
            PluginEvidenceStatus.VERIFIED,
        )

    async def test_all_external_failure_states_are_explicit_and_closed(self):
        admission, event = accepted_turn()
        no_read_states = (
            PluginEvidenceStatus.MISSING,
            PluginEvidenceStatus.DISABLED,
            PluginEvidenceStatus.INTERFACE_CHANGED,
            PluginEvidenceStatus.HOOK_ORDER_INVALID,
        )
        for status in no_read_states:
            with self.subTest(status=status):
                result = await MemoryPolicy().decide(
                    admission,
                    event,
                    adapter=LivingMemoryAdapter.degraded(status),
                    provided_recall=provided_request(
                        [
                            {
                                "sender_id": event.envelope.sender_id,
                                "content": "降级时不能消费",
                                "score": 1.0,
                                "confidence": 1.0,
                            }
                        ]
                    ),
                    include_recent=True,
                    semantic_required=True,
                )
                self.assertIs(result.decision.mode, MemoryMode.DEGRADED)
                self.assertEqual(result.decision.selected_facts, ())
                self.assertIs(result.plugin_evidence.status, status)
                self.assertFalse(result.plugin_evidence.trusted)
                self.assertEqual(result.read_count, 0)

        failure_cases = (
            (TimeoutError("raw timeout detail"), PluginEvidenceStatus.TIMEOUT),
            (RuntimeError("raw internal detail"), PluginEvidenceStatus.ERROR),
        )
        for error, expected_status in failure_cases:
            with self.subTest(status=expected_status):
                reader = CountingReader(error=error)
                result = await MemoryPolicy().decide(
                    admission,
                    event,
                    adapter=LivingMemoryAdapter.verified(reader=reader),
                    include_recent=True,
                )
                self.assertIs(result.decision.mode, MemoryMode.DEGRADED)
                self.assertIs(result.plugin_evidence.status, expected_status)
                self.assertEqual(result.read_count, 1)
                self.assertEqual(len(reader.calls), 1)
                self.assertNotIn("raw", repr(result))

        reader = CountingReader(value={"unexpected": "shape"})
        malformed = await MemoryPolicy().decide(
            admission,
            event,
            adapter=LivingMemoryAdapter.verified(reader=reader),
            include_recent=True,
        )
        self.assertIs(
            malformed.plugin_evidence.status,
            PluginEvidenceStatus.INTERFACE_CHANGED,
        )
        self.assertIs(malformed.decision.mode, MemoryMode.DEGRADED)
        self.assertEqual(malformed.read_count, 1)

    async def test_other_bot_plugin_and_banned_rows_never_enter_selected_or_public(self):
        admission, event = accepted_turn()
        reader = CountingReader(
            [
                recent_row(event, "允许的当前用户内容", id="safe-current"),
                recent_row(
                    event,
                    "另一个人的私密内容",
                    id="other-user",
                    sender_id="peer-2",
                ),
                recent_row(
                    event,
                    "被封禁内容",
                    id="banned-row",
                    banned=True,
                ),
                recent_row(
                    event,
                    "机器人内容",
                    id="bot-row",
                    sender_kind="known_bot",
                ),
                recent_row(
                    event,
                    "Parser 自动输出",
                    id="plugin-row",
                    plugin_source="parser",
                ),
                recent_row(
                    event,
                    "表情查询内容",
                    id="meme-row",
                    source_kind="meme_query",
                ),
            ]
        )

        result = await MemoryPolicy().decide(
            admission,
            event,
            adapter=LivingMemoryAdapter.verified(reader=reader),
            include_recent=True,
        )

        self.assertEqual(
            [fact.content for fact in result.current_subject_facts],
            ["允许的当前用户内容"],
        )
        self.assertEqual(result.public_background_facts, ())
        rendered = "\n".join(
            fact.content for fact in result.decision.selected_facts
        )
        for forbidden in (
            "另一个人的私密内容",
            "被封禁内容",
            "机器人内容",
            "Parser 自动输出",
            "表情查询内容",
        ):
            self.assertNotIn(forbidden, rendered)
        counts = dict(result.exclusion_counts)
        self.assertGreaterEqual(counts.get("other_subject", 0), 1)
        self.assertGreaterEqual(counts.get("banned_source", 0), 1)
        self.assertGreaterEqual(counts.get("bot_source", 0), 1)
        self.assertGreaterEqual(counts.get("plugin_source", 0), 2)

    async def test_subjectless_or_low_relevance_personal_text_is_never_promoted(self):
        admission, event = accepted_turn(chat_type="group")
        provided = provided_request(
            [
                {
                    "id": "unknown-personal",
                    "content": "不知道属于谁的私事",
                    "scope": "personal",
                    "confidence": 1.0,
                    "score": 1.0,
                },
                {
                    "id": "low-public",
                    "content": "低相关群背景",
                    "scope": "group",
                    "confidence": 0.9,
                    "score": 0.1,
                },
                {
                    "id": "low-current",
                    "sender_id": event.envelope.sender_id,
                    "content": "低相关个人文本",
                    "scope": "personal",
                    "confidence": 0.9,
                    "score": 0.1,
                },
                {
                    "id": "safe-public",
                    "content": "高相关公共事实",
                    "scope": "group",
                    "confidence": 0.9,
                    "score": 0.9,
                },
            ]
        )

        result = await MemoryPolicy().decide(
            admission,
            event,
            adapter=LivingMemoryAdapter.verified(),
            provided_recall=provided,
            include_recent=False,
            semantic_required=True,
        )

        self.assertEqual(result.current_subject_facts, ())
        self.assertEqual(
            [fact.content for fact in result.public_background_facts],
            ["高相关公共事实"],
        )
        selected = "\n".join(
            fact.content for fact in result.decision.selected_facts
        )
        self.assertNotIn("不知道属于谁的私事", selected)
        self.assertNotIn("低相关群背景", selected)
        self.assertNotIn("低相关个人文本", selected)

    async def test_private_scope_rejects_group_background_but_allows_public(self):
        admission, event = accepted_turn(chat_type="private")
        provided = provided_request(
            [
                {
                    "id": "group-only",
                    "content": "群内公共背景",
                    "scope": "group",
                    "confidence": 0.9,
                    "score": 0.9,
                },
                {
                    "id": "global-public",
                    "content": "公开背景",
                    "scope": "public",
                    "confidence": 0.9,
                    "score": 0.9,
                },
            ]
        )

        result = await MemoryPolicy().decide(
            admission,
            event,
            adapter=LivingMemoryAdapter.verified(),
            provided_recall=provided,
            include_recent=False,
            semantic_required=True,
        )

        self.assertEqual(
            [fact.content for fact in result.public_background_facts],
            ["公开背景"],
        )

    async def test_unstructured_rag_injection_is_counted_but_never_promoted(self):
        admission, event = accepted_turn()
        secret = "这是没有主体来源的私密旧对话"
        block = f"<RAG-Faiss-Memory>{secret}</RAG-Faiss-Memory>"
        provided = provided_request(None, extra_text=block, prompt=f"当前问题\n{block}")

        result = await MemoryPolicy().decide(
            admission,
            event,
            adapter=LivingMemoryAdapter.verified(),
            provided_recall=provided,
            include_recent=False,
            semantic_required=True,
        )

        self.assertEqual(result.decision.selected_facts, ())
        self.assertGreaterEqual(
            dict(result.exclusion_counts).get("unstructured_recall", 0),
            1,
        )
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, json.dumps(result.trace_metadata(), ensure_ascii=False))

    async def test_current_message_order_and_repr_trace_are_content_and_id_free(self):
        admission, event = accepted_turn()
        secret = "极其敏感的合成偏好文本"
        reader = CountingReader([recent_row(event, secret)])
        adapter = LivingMemoryAdapter.verified(reader=reader)

        result = await MemoryPolicy().decide(
            admission,
            event,
            adapter=adapter,
            include_recent=True,
        )

        self.assertEqual(result.context_order[0], "current_message")
        self.assertTrue(result.current_message_precedence)
        rendered = repr(result) + json.dumps(
            result.trace_metadata(),
            ensure_ascii=False,
        )
        for forbidden in (
            secret,
            event.envelope.message_id,
            event.envelope.sender_id,
            event.envelope.sender_key,
            event.envelope.session_id,
            event.envelope.scope_key,
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertNotIn(secret, repr(adapter))
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.read_count = 2

    async def test_non_accept_human_turn_never_reads_or_creates_memory_result(self):
        admission, event = accepted_turn()
        dropped = IngressDecision(
            binding=admission.binding,
            sender_kind=SenderKind.HUMAN,
            disposition=IngressDisposition.DROP_BANNED,
            gate_evidence=admission.gate_evidence,
            reason_codes=("reneban_banned",),
        )
        reader = CountingReader(error=AssertionError("dropped turn read"))

        with self.assertRaises(MemoryPolicyIneligible):
            await MemoryPolicy().decide(
                dropped,
                event,
                adapter=LivingMemoryAdapter.verified(reader=reader),
                include_recent=True,
            )

        self.assertEqual(reader.calls, [])

    async def test_verified_public_reader_receives_one_bounded_transport_request(self):
        admission, event = accepted_turn()
        reader = CountingReader([recent_row(event, "公开 transport 近期消息")])
        adapter = LivingMemoryAdapter.verified(reader=reader)

        result = await MemoryPolicy().decide(
            admission,
            event,
            adapter=adapter,
            include_recent=True,
            max_results=4,
        )

        self.assertIs(result.plugin_evidence.status, PluginEvidenceStatus.VERIFIED)
        self.assertEqual(EXPECTED_PLUGIN_NAME, "astrbot_plugin_livingmemory")
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(reader.calls[0].session_id, event.envelope.session_id)
        self.assertEqual(reader.calls[0].scope_key, event.envelope.scope_key)
        self.assertLessEqual(reader.calls[0].recent_limit, 20)


class LivingMemoryAdapterShapeTests(unittest.TestCase):
    def test_adapter_result_and_request_are_safe_typed_objects(self):
        admission, event = accepted_turn()
        request = LivingMemoryReadRequest(
            binding=event.binding,
            recent_limit=5,
        )
        self.assertNotIn(event.envelope.session_id, repr(request))
        degraded = LivingMemoryAdapterResult.degraded(
            PluginEvidenceStatus.MISSING,
            reason_code="plugin_missing",
        )
        self.assertEqual(degraded.candidates, ())
        self.assertEqual(degraded.read_count, 0)
        self.assertNotIn("session", repr(degraded))


if __name__ == "__main__":
    unittest.main()
