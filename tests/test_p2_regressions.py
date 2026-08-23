import unittest
from types import SimpleNamespace

from astrbot_plugin_shio.core.context_assembler import (
    adapt_livingmemory_facts,
    assemble_context_views,
    build_direct_reply_target,
    build_reference_context,
    select_facts_for_target,
)
from astrbot_plugin_shio.core.conversation_ledger import adapt_astrbot_history
from astrbot_plugin_shio.core.identity import TurnEnvelope, resolve_principal


SCOPE = "platform:test|bot:shio|group:test-group"
OWNER = f"{SCOPE}|user:owner-b"
GUEST = f"{SCOPE}|user:guest-a"


def current_turn(**overrides):
    values = {
        "session_id": "test-group",
        "message_id": "msg-b",
        "scope_key": SCOPE,
        "sender_key": OWNER,
        "sender_id": "owner-b",
        "platform_id": "test",
        "bot_id": "shio",
        "chat_type": "group",
        "group_id": "test-group",
        "reply_to_message_id": "",
        "reply_to_sender_id": "",
        "timestamp": 1_765_000_100,
        "timestamp_source": "message",
        "source_kind": "inbound",
        "degradation_reasons": (),
    }
    values.update(overrides)
    return TurnEnvelope(**values)


def typed_history(contexts):
    return adapt_astrbot_history(
        contexts,
        scope_key=SCOPE,
        session_id="test-group",
        group_id="test-group",
        bot_sender_key=f"{SCOPE}|user:shio",
    )


class P2TypedPipelineRegressions(unittest.TestCase):
    def test_delayed_reply_for_previous_user_never_enters_current_replyer_thread(self):
        contexts = [
            {
                "role": "user",
                "content": "甲指出一次校准错误",
                "message_id": "msg-a",
                "sender_id": "guest-a",
            },
            {
                "role": "user",
                "content": "乙提出当前请求",
                "message_id": "msg-b",
                "sender_id": "owner-b",
            },
            {
                "role": "assistant",
                "content": "延迟到达的甲的回复",
                "message_id": "bot-a-1",
                "reply_to_message_id": "msg-a",
                "target_sender_id": "guest-a",
            },
        ]
        target = build_direct_reply_target(current_turn(), "乙提出当前请求")
        selection = select_facts_for_target((), target_sender_key=OWNER)

        assembled = assemble_context_views(
            typed_history(contexts),
            reply_target=target,
            reference=None,
            fact_selection=selection,
        )
        self.assertEqual(assembled.replyer_thread, ())
        self.assertNotIn(
            "延迟到达的甲的回复",
            [record.content for record in assembled.replyer_thread],
        )
        self.assertEqual(assembled.reply_target.message_id, "msg-b")
        self.assertEqual(assembled.reply_target.sender_key, OWNER)

    def test_every_delayed_bubble_keeps_previous_user_target_after_switch(self):
        contexts = [
            {
                "role": "user",
                "content": "甲的问题",
                "message_id": "msg-a",
                "sender_id": "guest-a",
            },
            {
                "role": "user",
                "content": "乙的当前问题",
                "message_id": "msg-b",
                "sender_id": "owner-b",
            },
            {
                "role": "assistant",
                "content": "甲的延迟气泡一",
                "message_id": "bot-a-1",
                "reply_to_message_id": "msg-a",
                "target_sender_id": "guest-a",
            },
            {
                "role": "assistant",
                "content": "甲的延迟气泡二",
                "message_id": "bot-a-2",
                "reply_to_message_id": "bot-a-1",
                "target_sender_id": "guest-a",
            },
        ]
        target = build_direct_reply_target(current_turn(), "乙的当前问题")
        assembled = assemble_context_views(
            typed_history(contexts),
            reply_target=target,
            reference=None,
            fact_selection=select_facts_for_target((), target_sender_key=OWNER),
        )

        self.assertEqual(assembled.replyer_thread, ())
        self.assertEqual(
            [record.target_sender_key for record in assembled.public_background[-2:]],
            [GUEST, GUEST],
        )

    def test_quoted_old_message_is_reference_not_current_principal_or_plan_target(self):
        envelope = current_turn(
            message_id="msg-b-quote",
            reply_to_message_id="msg-a-old",
            reply_to_sender_id="guest-a",
        )
        target = build_direct_reply_target(envelope, "乙询问甲说的内容")
        reference = build_reference_context(envelope)
        principal = resolve_principal(
            sender_id=envelope.sender_id,
            sender_key=envelope.sender_key,
            chat_type=envelope.chat_type,
            owner_ids={"owner-b"},
            verification_source="test_event_sender_id",
            identity_verified=True,
        )
        assembled = assemble_context_views(
            typed_history(
                [
                    {
                        "role": "user",
                        "content": "甲的旧消息",
                        "message_id": "msg-a-old",
                        "sender_id": "guest-a",
                    },
                    {
                        "role": "user",
                        "content": "乙询问甲说的内容",
                        "message_id": "msg-b-quote",
                        "sender_id": "owner-b",
                        "reply_to_message_id": "msg-a-old",
                    },
                ]
            ),
            reply_target=target,
            reference=reference,
            fact_selection=select_facts_for_target((), target_sender_key=OWNER),
        )
        self.assertTrue(principal.is_owner)
        self.assertEqual(assembled.reference.message_id, "msg-a-old")
        self.assertEqual(assembled.reference.sender_key, GUEST)
        self.assertIn(
            "甲的旧消息",
            [record.content for record in assembled.public_background],
        )
        self.assertEqual(assembled.reply_target.message_id, "msg-b-quote")
        self.assertEqual(assembled.reply_target.sender_key, OWNER)

    def test_other_users_memory_stays_out_of_current_users_speech_plan(self):
        request = SimpleNamespace(
            contexts=[
                {
                    "role": "tool",
                    "name": "recall_long_term_memory",
                    "content": (
                        '{"results":['
                        '{"id":"owner-fact","sender_id":"owner-b",'
                        '"content":"乙喜欢温水","confidence":0.9},'
                        '{"id":"guest-fact","sender_id":"guest-a",'
                        '"content":"甲刚做完手术","confidence":0.95}'
                        ']}'
                    ),
                }
            ],
            extra_user_content_parts=[],
            prompt="乙提出当前问题",
        )
        facts = adapt_livingmemory_facts(request, scope_key=SCOPE)
        selection = select_facts_for_target(facts, target_sender_key=OWNER)
        target = build_direct_reply_target(current_turn(), "乙提出当前问题")

        self.assertEqual(
            [item.source_id for item in selection.must_include_candidates],
            ["owner-fact"],
        )
        self.assertEqual(selection.must_include_candidates[0].subject_key, OWNER)
        self.assertNotIn(
            "guest-fact",
            [item.source_id for item in selection.must_include_candidates],
        )


if __name__ == "__main__":
    unittest.main()
