import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.conversation_ledger import (
    ConversationLedger,
    LedgerRole,
    LedgerSourceKind,
    adapt_astrbot_history,
    ledger_content_digest,
    records_for_sender_thread,
)
from astrbot_plugin_shio.core.identity import TurnEnvelope


def envelope(
    *,
    scope_key="platform:p|bot:b|group:g",
    message_id="msg-current",
    sender_key="platform:p|bot:b|group:g|user:guest-a",
    reply_to_message_id="msg-old",
):
    return TurnEnvelope(
        session_id="g",
        message_id=message_id,
        scope_key=scope_key,
        sender_key=sender_key,
        sender_id="guest-a",
        platform_id="p",
        bot_id="b",
        chat_type="group",
        group_id="g",
        reply_to_message_id=reply_to_message_id,
        reply_to_sender_id="guest-b",
        timestamp=1_765_000_000,
        timestamp_source="message",
        source_kind="inbound",
        degradation_reasons=(),
    )


class ConversationLedgerTests(unittest.TestCase):
    def test_persisted_group_inbound_survives_restart_with_bounded_tail(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = Path(root) / "public_group_ledger.json"
            first = ConversationLedger(
                max_records_per_scope=16,
                state_path=state_path,
                max_persisted_records_per_scope=8,
            )
            for index in range(10):
                first.record_inbound(
                    envelope(message_id=f"persist-{index}"),
                    f"公开群聊-{index}",
                )
            first.record_outbound(
                scope_key="platform:p|bot:b|group:g",
                session_id="g",
                message_id="private-bot-output",
                bot_sender_key="platform:p|bot:b|group:g|user:bot",
                target_sender_key="platform:p|bot:b|group:g|user:guest-a",
                reply_to_message_id="persist-9",
                timestamp=1_765_000_001,
                content="不应作为可恢复公开入站消息持久化",
            )
            first.flush()

            restored = ConversationLedger(
                max_records_per_scope=16,
                state_path=state_path,
                max_persisted_records_per_scope=8,
            )
            records = restored.records("platform:p|bot:b|group:g")

            self.assertEqual(len(records), 8)
            self.assertEqual(
                [record.message_id for record in records],
                [f"persist-{index}" for index in range(2, 10)],
            )
            self.assertTrue(
                all(record.source_kind is LedgerSourceKind.INBOUND for record in records)
            )
            self.assertNotIn("private-bot-output", state_path.read_text("utf-8"))
            self.assertTrue(restored.persistence_metadata()["persistence_loaded"])

    def test_corrupt_persisted_ledger_fails_closed_without_exposing_payload(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = Path(root) / "public_group_ledger.json"
            state_path.write_text(
                json.dumps({"schema_version": 1, "records": [{"content": "marker"}]}),
                encoding="utf-8",
            )

            restored = ConversationLedger(state_path=state_path)

            self.assertEqual(restored.records("platform:p|bot:b|group:g"), ())
            metadata = restored.persistence_metadata()
            self.assertFalse(metadata["persistence_loaded"])
            self.assertEqual(metadata["persistence_failure_code"], "state_invalid")
            self.assertNotIn("marker", repr(metadata))

    def test_four_source_kinds_remain_typed(self):
        ledger = ConversationLedger()
        turn = envelope()
        inbound = ledger.record_inbound(turn, "当前问题")
        reference = ledger.record_reference(
            turn,
            referenced_content="被引用内容",
            referenced_sender_key="platform:p|bot:b|group:g|user:guest-b",
        )
        outbound = ledger.record_outbound(
            scope_key=turn.scope_key,
            session_id=turn.session_id,
            message_id="bot-msg",
            bot_sender_key="platform:p|bot:b|group:g|user:bot",
            target_sender_key=turn.sender_key,
            reply_to_message_id=turn.message_id,
            timestamp=1_765_000_001,
            content="机器人回复",
        )
        tool = ledger.record_tool_result(
            scope_key=turn.scope_key,
            session_id=turn.session_id,
            source_id="call-1",
            tool_name="public_web_read",
            timestamp=1_765_000_000.5,
            content="公开资料结果",
        )

        self.assertEqual(
            [record.source_kind for record in ledger.records(turn.scope_key)],
            [
                LedgerSourceKind.INBOUND,
                LedgerSourceKind.REFERENCE,
                LedgerSourceKind.OUTBOUND,
                LedgerSourceKind.TOOL_RESULT,
            ],
        )
        self.assertEqual(inbound.role, LedgerRole.USER)
        self.assertEqual(reference.role, LedgerRole.CONTEXT)
        self.assertEqual(outbound.role, LedgerRole.ASSISTANT)
        self.assertEqual(tool.role, LedgerRole.TOOL)

    def test_reference_keeps_actor_and_referenced_sender_distinct(self):
        ledger = ConversationLedger()
        turn = envelope()

        record = ledger.record_reference(
            turn,
            referenced_content="旧消息",
            referenced_sender_key="platform:p|bot:b|group:g|user:owner-b",
        )

        self.assertEqual(record.sender_key, turn.sender_key)
        self.assertNotEqual(record.sender_key, record.referenced_sender_key)
        self.assertEqual(record.reply_to_message_id, "msg-old")
        self.assertEqual(record.source_id, "msg-old")

    def test_tool_result_cannot_become_assistant_history(self):
        ledger = ConversationLedger()
        record = ledger.record_tool_result(
            scope_key="platform:p|bot:b|group:g",
            session_id="g",
            source_id="call-1",
            tool_name="search",
            timestamp=1,
            content="result",
        )

        self.assertEqual(record.source_kind, LedgerSourceKind.TOOL_RESULT)
        self.assertEqual(record.role, LedgerRole.TOOL)
        self.assertEqual(record.sender_key, "")

    def test_scope_buckets_are_separate_and_bounded(self):
        ledger = ConversationLedger(max_records_per_scope=8)
        for index in range(10):
            ledger.record_inbound(
                envelope(message_id=f"a-{index}"),
                f"message-{index}",
            )
        ledger.record_inbound(
            envelope(
                scope_key="platform:p|bot:b|group:other",
                message_id="other-1",
                sender_key="platform:p|bot:b|group:other|user:guest-a",
            ),
            "other",
        )

        self.assertEqual(len(ledger.records("platform:p|bot:b|group:g")), 8)
        self.assertEqual(
            [record.message_id for record in ledger.records("platform:p|bot:b|group:g")],
            [f"a-{index}" for index in range(2, 10)],
        )
        self.assertEqual(len(ledger.records("platform:p|bot:b|group:other")), 1)

    def test_unscoped_records_fail_closed(self):
        ledger = ConversationLedger()

        with self.assertRaisesRegex(ValueError, "scope_key"):
            ledger.record_inbound(envelope(scope_key=""), "不能进入共享桶")

    def test_content_digest_is_stable_but_not_plaintext(self):
        digest = ledger_content_digest("一段测试内容")

        self.assertEqual(digest, ledger_content_digest("一段测试内容"))
        self.assertNotIn("测试内容", digest)
        self.assertEqual(len(digest), 64)

    def test_message_reply_and_sender_indexes_are_exact(self):
        ledger = ConversationLedger()
        turn = envelope()
        inbound = ledger.record_inbound(turn, "当前问题")
        reference = ledger.record_reference(turn, referenced_content="引用")
        outbound = ledger.record_outbound(
            scope_key=turn.scope_key,
            session_id=turn.session_id,
            message_id="bot-msg",
            bot_sender_key="platform:p|bot:b|group:g|user:bot",
            target_sender_key=turn.sender_key,
            reply_to_message_id=turn.message_id,
            timestamp=1_765_000_001,
            content="回复",
        )

        self.assertEqual(
            ledger.records_by_message_id(turn.scope_key, turn.message_id),
            (inbound, reference),
        )
        self.assertEqual(
            ledger.records_replying_to(turn.scope_key, turn.message_id),
            (outbound,),
        )
        self.assertEqual(
            ledger.records_by_sender(turn.scope_key, turn.sender_key),
            (inbound, reference),
        )

    def test_indexes_never_cross_scope(self):
        ledger = ConversationLedger()
        first = ledger.record_inbound(envelope(), "scope-a")
        other_scope = "platform:p|bot:b|group:other"
        second = ledger.record_inbound(
            envelope(
                scope_key=other_scope,
                sender_key=f"{other_scope}|user:guest-a",
            ),
            "scope-b",
        )

        self.assertEqual(
            ledger.records_by_message_id(first.scope_key, "msg-current"),
            (first,),
        )
        self.assertEqual(
            ledger.records_by_message_id(second.scope_key, "msg-current"),
            (second,),
        )

    def test_pruned_records_are_removed_from_all_indexes(self):
        ledger = ConversationLedger(max_records_per_scope=8)
        records = []
        for index in range(9):
            records.append(
                ledger.record_inbound(
                    envelope(message_id=f"msg-{index}"),
                    f"content-{index}",
                )
            )

        self.assertEqual(
            ledger.records_by_message_id(records[0].scope_key, "msg-0"),
            (),
        )
        self.assertNotIn(
            records[0],
            ledger.records_by_sender(records[0].scope_key, records[0].sender_key),
        )
        self.assertEqual(
            ledger.records_by_message_id(records[-1].scope_key, "msg-8"),
            (records[-1],),
        )


class LegacyHistoryAdapterTests(unittest.TestCase):
    scope = "platform:p|bot:b|group:g"
    guest_a = f"{scope}|user:guest-a"
    guest_b = f"{scope}|user:guest-b"

    def test_unknown_assistant_is_not_claimed_by_adjacent_user(self):
        records = adapt_astrbot_history(
            [
                {"role": "user", "content": "A 的问题", "sender_id": "guest-a"},
                {"role": "assistant", "content": "没有目标元数据的旧回复"},
                {"role": "user", "content": "B 的问题", "sender_id": "guest-b"},
            ],
            scope_key=self.scope,
            session_id="g",
            group_id="g",
            bot_sender_key=f"{self.scope}|user:bot",
        )

        self.assertEqual(records[1].attribution_status, "unknown_target")
        self.assertEqual(records[1].target_sender_key, "")
        self.assertEqual(
            [record.content for record in records_for_sender_thread(records, self.guest_b)],
            ["B 的问题"],
        )

    def test_explicit_assistant_target_enters_only_that_sender_thread(self):
        records = adapt_astrbot_history(
            [
                {"role": "user", "content": "A", "sender_id": "guest-a"},
                {
                    "role": "assistant",
                    "content": "明确回复 A",
                    "target_sender_id": "guest-a",
                },
                {"role": "user", "content": "B", "sender_id": "guest-b"},
            ],
            scope_key=self.scope,
            session_id="g",
            group_id="g",
        )

        self.assertEqual(records[1].attribution_status, "verified_target")
        self.assertEqual(
            [record.content for record in records_for_sender_thread(records, self.guest_a)],
            ["A", "明确回复 A"],
        )
        self.assertEqual(
            [record.content for record in records_for_sender_thread(records, self.guest_b)],
            ["B"],
        )

    def test_unlabelled_user_and_wrong_group_are_not_attributed(self):
        records = adapt_astrbot_history(
            [
                {"role": "user", "content": "无 sender"},
                {
                    "role": "user",
                    "content": "另一个群",
                    "sender_id": "guest-a",
                    "group_id": "other-group",
                },
            ],
            scope_key=self.scope,
            session_id="g",
            group_id="g",
        )

        self.assertEqual(records[0].attribution_status, "unknown_sender")
        self.assertEqual(records[1].attribution_status, "scope_mismatch")
        self.assertEqual(records_for_sender_thread(records, self.guest_a), ())

    def test_metadata_fields_are_read_without_modifying_source(self):
        source = {
            "role": "assistant",
            "content": "旧回复",
            "metadata": {
                "target_sender_id": "guest-b",
                "message_id": "old-bot-msg",
            },
        }
        snapshot = repr(source)

        records = adapt_astrbot_history(
            [source],
            scope_key=self.scope,
            session_id="g",
        )

        self.assertEqual(records[0].target_sender_key, self.guest_b)
        self.assertEqual(records[0].message_id, "old-bot-msg")
        self.assertEqual(repr(source), snapshot)


if __name__ == "__main__":
    unittest.main()
