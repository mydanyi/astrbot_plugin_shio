import unittest
from types import SimpleNamespace

from astrbot_plugin_shio.core.context_assembler import (
    REFERENCE_CONTEXT_EXTRA,
    REPLY_TARGET_EXTRA,
    FactSelection,
    ProvenancedFact,
    assemble_context_views,
    build_direct_reply_target,
    build_reference_context,
    adapt_livingmemory_facts,
    ensure_direct_reply_target,
    ensure_reference_context,
    select_facts_for_target,
)
from astrbot_plugin_shio.core.identity import resolve_principal
from astrbot_plugin_shio.core.identity import TurnEnvelope
from astrbot_plugin_shio.core.conversation_ledger import (
    LedgerRecord,
    LedgerRole,
    LedgerSourceKind,
    build_inbound_identity_metadata,
    build_participant_display,
    ledger_content_digest,
)


def turn(**overrides):
    values = {
        "session_id": "group-7",
        "message_id": "current-msg",
        "scope_key": "platform:p|bot:b|group:group-7",
        "sender_key": "platform:p|bot:b|group:group-7|user:guest-b",
        "sender_id": "guest-b",
        "platform_id": "p",
        "bot_id": "b",
        "chat_type": "group",
        "group_id": "group-7",
        "reply_to_message_id": "owner-old-msg",
        "reply_to_sender_id": "owner-a",
        "timestamp": 1_765_000_000,
        "timestamp_source": "message",
        "source_kind": "inbound",
        "degradation_reasons": (),
    }
    values.update(overrides)
    return TurnEnvelope(**values)


class FakeEvent:
    def __init__(self):
        self.extras = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class ReplyTargetTests(unittest.TestCase):
    def test_direct_target_binds_current_message_not_quoted_message(self):
        target = build_direct_reply_target(turn(), "我现在问的问题")

        self.assertEqual(target.message_id, "current-msg")
        self.assertEqual(target.referenced_message_id, "owner-old-msg")
        self.assertEqual(
            target.sender_key,
            "platform:p|bot:b|group:group-7|user:guest-b",
        )
        self.assertNotIn("owner-a", target.sender_key)
        self.assertTrue(target.is_actionable)

    def test_missing_structural_target_is_explicitly_degraded(self):
        target = build_direct_reply_target(
            turn(message_id="", sender_key=""),
            "缺字段",
        )

        self.assertFalse(target.is_actionable)
        self.assertIn("missing_message_id", target.degradation_reasons)
        self.assertIn("missing_sender_key", target.degradation_reasons)

    def test_content_is_represented_by_digest_not_copied_into_target(self):
        content = "当前用户的私密问题"
        target = build_direct_reply_target(turn(), content)

        self.assertNotIn(content, repr(target))
        self.assertEqual(len(target.content_digest), 64)

    def test_target_is_stable_for_the_lifetime_of_an_event(self):
        event = FakeEvent()
        first = ensure_direct_reply_target(event, turn(), "第一版问题")
        second = ensure_direct_reply_target(
            event,
            turn(message_id="different"),
            "后来被修改的文本",
        )

        self.assertIs(first, second)
        self.assertEqual(second.message_id, "current-msg")
        self.assertIs(event.get_extra(REPLY_TARGET_EXTRA), first)


class ReferenceContextTests(unittest.TestCase):
    def test_quoted_owner_is_separate_from_current_guest_principal(self):
        current_turn = turn()
        target = build_direct_reply_target(current_turn, "我引用主人的话来提问")
        reference = build_reference_context(current_turn)
        principal = resolve_principal(
            sender_id=current_turn.sender_id,
            sender_key=current_turn.sender_key,
            chat_type=current_turn.chat_type,
            owner_ids={"owner-a"},
            verification_source="astrbot_event_sender_id",
            identity_verified=True,
        )

        self.assertIsNotNone(reference)
        self.assertEqual(reference.message_id, "owner-old-msg")
        self.assertEqual(
            reference.sender_key,
            "platform:p|bot:b|group:group-7|user:owner-a",
        )
        self.assertEqual(target.sender_key, current_turn.sender_key)
        self.assertFalse(principal.is_owner)
        self.assertEqual(principal.relationship_role, "group_peer")

    def test_reference_without_sender_id_keeps_unknown_attribution(self):
        reference = build_reference_context(turn(reply_to_sender_id=""))

        self.assertIsNotNone(reference)
        self.assertEqual(reference.sender_key, "")
        self.assertEqual(reference.attribution_status, "unknown_sender")
        self.assertIn(
            "missing_referenced_sender_id",
            reference.degradation_reasons,
        )

    def test_non_reply_has_no_reference_context(self):
        event = FakeEvent()

        reference = ensure_reference_context(
            event,
            turn(reply_to_message_id="", reply_to_sender_id=""),
        )

        self.assertIsNone(reference)
        self.assertIsNone(event.get_extra(REFERENCE_CONTEXT_EXTRA))

    def test_reference_context_is_cached_without_touching_reply_target(self):
        event = FakeEvent()
        current_turn = turn()
        target = ensure_direct_reply_target(event, current_turn, "当前问题")
        reference = ensure_reference_context(event, current_turn)

        self.assertIs(event.get_extra(REFERENCE_CONTEXT_EXTRA), reference)
        self.assertIs(event.get_extra(REPLY_TARGET_EXTRA), target)
        self.assertNotEqual(reference.message_id, target.message_id)


class ProvenancedFactTests(unittest.TestCase):
    scope = "platform:p|bot:b|group:group-7"

    def request(self, *, contexts=None, extra_parts=None, prompt="当前问题"):
        return SimpleNamespace(
            contexts=list(contexts or []),
            extra_user_content_parts=list(extra_parts or []),
            prompt=prompt,
        )

    def test_structured_sender_becomes_explicit_personal_subject(self):
        request = self.request(
            contexts=[
                {
                    "role": "tool",
                    "tool_call_id": "fake_recall_1",
                    "name": "recall_long_term_memory",
                    "content": (
                        '{"results":[{"id":"memory-1","platform_id":"p","sender_id":"guest-b",'
                        '"content":"喜欢海边","confidence":0.82,"timestamp":123}]}'
                    ),
                }
            ]
        )

        facts = adapt_livingmemory_facts(request, scope_key=self.scope)

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].scope, "personal")
        self.assertEqual(facts[0].subject_key, "platform:p|account:guest-b")
        self.assertEqual(facts[0].source_kind, "livingmemory_tool_recall")
        self.assertEqual(facts[0].source_id, "memory-1")
        self.assertEqual(facts[0].observed_at, 123)
        self.assertEqual(facts[0].confidence, 0.82)

    def test_other_users_structured_fact_is_not_relabelled_as_current_user(self):
        request = self.request(
            contexts=[
                {
                    "role": "tool",
                    "name": "recall_long_term_memory",
                    "content": (
                        '{"results":[{"platform_id":"p","sender_id":"guest-a",'
                        '"content":"正在修东西"}]}'
                    ),
                }
            ]
        )

        facts = adapt_livingmemory_facts(request, scope_key=self.scope)

        self.assertEqual(facts[0].subject_key, "platform:p|account:guest-a")
        self.assertNotEqual(facts[0].subject_key, "platform:p|account:guest-b")

    def test_unstructured_recall_is_low_confidence_background(self):
        request = self.request(
            prompt=(
                "当前问题\n<RAG-Faiss-Memory>有人以前提过喜欢海边"
                "</RAG-Faiss-Memory>"
            )
        )

        facts = adapt_livingmemory_facts(
            request,
            scope_key=self.scope,
            observed_at=456,
        )

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].subject_key, "")
        self.assertEqual(facts[0].scope, "group_background")
        self.assertLessEqual(facts[0].confidence, 0.35)
        self.assertEqual(facts[0].observed_at, 456)

    def test_foreign_scope_subject_key_is_rejected(self):
        request = self.request(
            contexts=[
                {
                    "role": "tool",
                    "name": "recall_long_term_memory",
                    "content": (
                        '{"results":[{"subject_key":'
                        '"platform:p|bot:b|group:other|user:owner",'
                        '"content":"伪造跨群主体","confidence":0.99}]}'
                    ),
                }
            ]
        )

        facts = adapt_livingmemory_facts(request, scope_key=self.scope)

        self.assertEqual(facts[0].subject_key, "")
        self.assertEqual(facts[0].scope, "group_background")
        self.assertLessEqual(facts[0].confidence, 0.35)

    def test_shio_verified_context_is_not_mistaken_for_memory(self):
        request = self.request(
            extra_parts=[
                SimpleNamespace(
                    text='<shio_verified_context owner="true">可信身份</shio_verified_context>'
                )
            ]
        )

        self.assertEqual(
            adapt_livingmemory_facts(request, scope_key=self.scope),
            (),
        )

    def test_only_current_high_confidence_personal_fact_is_required(self):
        request = self.request(
            contexts=[
                {
                    "role": "tool",
                    "name": "recall_long_term_memory",
                    "content": (
                        '{"results":['
                        '{"platform_id":"p","sender_id":"guest-b","content":"喜欢海边","confidence":0.8},'
                        '{"platform_id":"p","sender_id":"guest-a","content":"刚做完手术","confidence":0.9},'
                        '{"platform_id":"p","sender_id":"guest-b","content":"也许怕冷","confidence":0.3}'
                        ']}'
                    ),
                }
            ]
        )
        facts = adapt_livingmemory_facts(request, scope_key=self.scope)

        selection = select_facts_for_target(
            facts,
            target_sender_key="platform:p|account:guest-b",
        )

        self.assertEqual(
            [fact.content for fact in selection.must_include_candidates],
            ["喜欢海边"],
        )
        self.assertEqual(
            [fact.content for fact in selection.uncertain_target_facts],
            ["也许怕冷"],
        )
        self.assertEqual(
            [fact.content for fact in selection.other_subject_facts],
            ["刚做完手术"],
        )

    def test_unknown_personal_claim_is_downgraded_to_background(self):
        unknown_personal = ProvenancedFact(
            subject_key="",
            scope="personal",
            content="不知道是谁的个人经历",
            source_kind="test",
            source_id="fact-1",
            observed_at=0,
            confidence=0.95,
        )

        selection = select_facts_for_target(
            [unknown_personal],
            target_sender_key=f"{self.scope}|user:guest-b",
        )

        self.assertEqual(selection.must_include_candidates, ())
        self.assertEqual(len(selection.public_background), 1)
        self.assertEqual(selection.public_background[0].scope, "group_background")
        self.assertLessEqual(selection.public_background[0].confidence, 0.35)


class ContextViewTests(unittest.TestCase):
    scope = "platform:p|bot:b|group:group-7"
    guest_a = f"{scope}|user:guest-a"
    guest_b = f"{scope}|user:guest-b"

    @staticmethod
    def empty_facts():
        return FactSelection((), (), (), ())

    def test_identity_and_addressing_metadata_survive_view_assembly_exactly(self):
        metadata = build_inbound_identity_metadata(
            display_name="上下文测试发言者",
            display_name_source="event_sender",
            referenced_sender=build_participant_display(
                self.guest_a,
                display_name="上下文测试引用者",
                display_name_source="reply_component",
            ),
            mention_targets=(
                build_participant_display(
                    self.guest_a,
                    display_name="上下文测试提及者",
                    display_name_source="mention_component",
                ),
            ),
            has_reply_edge=True,
        )
        record = LedgerRecord(
            sequence=1,
            source_kind=LedgerSourceKind.INBOUND,
            role=LedgerRole.USER,
            scope_key=self.scope,
            session_id="group-7",
            message_id="prior-b",
            sender_key=self.guest_b,
            reply_to_message_id="prior-a",
            referenced_sender_key=self.guest_a,
            target_sender_key="",
            timestamp=1.0,
            content="上下文纯正文",
            content_digest=ledger_content_digest("上下文纯正文"),
            source_id="prior-b",
            attribution_status="verified",
            identity_metadata=metadata,
        )

        assembled = assemble_context_views(
            (record,),
            reply_target=build_direct_reply_target(turn(), "B 的当前问题"),
            reference=None,
            fact_selection=self.empty_facts(),
        )

        self.assertIs(assembled.planner_records[0], record)
        self.assertIs(assembled.replyer_thread[0].identity_metadata, metadata)
        self.assertEqual(assembled.replyer_thread[0].content, "上下文纯正文")
        self.assertNotIn("上下文测试发言者", record.content)

    def records(self):
        from astrbot_plugin_shio.core.conversation_ledger import adapt_astrbot_history

        return adapt_astrbot_history(
            [
                {"role": "user", "content": "A 的个人经历", "sender_id": "guest-a"},
                {"role": "assistant", "content": "没有目标的旧回复"},
                {"role": "user", "content": "B 的旧问题", "sender_id": "guest-b"},
                {
                    "role": "assistant",
                    "content": "明确回复 B",
                    "target_sender_id": "guest-b",
                },
                {
                    "role": "user",
                    "content": "B 的当前问题",
                    "sender_id": "guest-b",
                    "message_id": "current-msg",
                },
            ],
            scope_key=self.scope,
            session_id="group-7",
            group_id="group-7",
        )

    def test_planner_sees_typed_group_panorama(self):
        target = build_direct_reply_target(turn(), "B 的当前问题")

        assembled = assemble_context_views(
            self.records(),
            reply_target=target,
            reference=build_reference_context(turn()),
            fact_selection=self.empty_facts(),
        )

        self.assertEqual(len(assembled.planner_records), 5)
        self.assertTrue(
            all(record.source_kind.value for record in assembled.planner_records)
        )
        self.assertEqual(assembled.reference.message_id, "owner-old-msg")

    def test_replyer_thread_uses_only_explicit_target_and_excludes_current_prompt(self):
        assembled = assemble_context_views(
            self.records(),
            reply_target=build_direct_reply_target(turn(), "B 的当前问题"),
            reference=None,
            fact_selection=self.empty_facts(),
        )

        self.assertEqual(
            [record.content for record in assembled.replyer_thread],
            ["B 的旧问题", "明确回复 B"],
        )
        self.assertNotIn(
            "A 的个人经历",
            [record.content for record in assembled.replyer_thread],
        )
        self.assertNotIn(
            "没有目标的旧回复",
            [record.content for record in assembled.replyer_thread],
        )

    def test_other_users_and_unknown_assistant_are_labelled_background(self):
        assembled = assemble_context_views(
            self.records(),
            reply_target=build_direct_reply_target(turn(), "B 的当前问题"),
            reference=None,
            fact_selection=self.empty_facts(),
        )

        background = {record.content: record for record in assembled.public_background}
        self.assertEqual(background["A 的个人经历"].sender_key, self.guest_a)
        self.assertEqual(
            background["没有目标的旧回复"].attribution_status,
            "unknown_target",
        )

    def test_limits_keep_newest_records_without_crossing_scope(self):
        foreign = list(self.records())
        foreign_record = foreign[0]
        from dataclasses import replace

        assembled = assemble_context_views(
            [replace(foreign_record, scope_key="platform:p|bot:b|group:other"), *self.records()],
            reply_target=build_direct_reply_target(turn(), "B 的当前问题"),
            reference=None,
            fact_selection=self.empty_facts(),
            planner_record_limit=3,
            replyer_record_limit=1,
            background_record_limit=1,
        )

        self.assertEqual(len(assembled.planner_records), 3)
        self.assertEqual(len(assembled.replyer_thread), 1)
        self.assertLessEqual(len(assembled.public_background), 1)
        self.assertTrue(
            all(record.scope_key == self.scope for record in assembled.planner_records)
        )


if __name__ == "__main__":
    unittest.main()
