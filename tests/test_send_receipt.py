import unittest

from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.send_receipt import (
    CURRENT_GENERIC_SEND_CAPABILITY,
    InternalSendReceiptLedger,
    PlatformReceiptSource,
    SegmentSendStatus,
)


def target(**overrides):
    values = {
        "message_id": "incoming-message-1",
        "sender_key": "platform:p|user:u1",
        "session_id": "session-1",
        "scope_key": "platform:p|bot:b|group:g",
        "content_digest": "digest",
        "source_kind": "current_inbound",
        "referenced_message_id": "",
        "degradation_reasons": (),
    }
    values.update(overrides)
    return ReplyTarget(**values)


class InternalSendReceiptLedgerTests(unittest.TestCase):
    def setUp(self):
        ids = iter(("reply-a", "reply-b"))
        times = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0))
        self.ledger = InternalSendReceiptLedger(
            id_factory=lambda: next(ids),
            now_fn=lambda: next(times),
        )

    def test_generic_astrbot_contract_has_no_portable_platform_message_id(self):
        capability = CURRENT_GENERIC_SEND_CAPABILITY

        self.assertFalse(capability.event_send_returns_platform_message_id)
        self.assertFalse(capability.message_event_result_contains_platform_message_id)
        self.assertFalse(capability.context_send_returns_platform_message_id)
        self.assertFalse(capability.portable_platform_receipt_available)

    def test_binds_internal_reply_and_segments_to_exact_target(self):
        record = self.ledger.begin_reply(
            target=target(),
            visible_segments=("第一句", "第二句"),
            expression_ids=("expression-a", "expression-a", "expression-b"),
        )

        self.assertEqual(record.internal_reply_id, "shio-reply-a")
        self.assertEqual(record.target_message_id, "incoming-message-1")
        self.assertEqual(record.target_sender_key, "platform:p|user:u1")
        self.assertEqual(record.target_content_digest, "digest")
        self.assertEqual(record.target_source_kind, "current_inbound")
        self.assertEqual(record.expression_ids, ("expression-a", "expression-b"))
        self.assertEqual([s.segment_id for s in record.segments], [
            "shio-reply-a:0",
            "shio-reply-a:1",
        ])
        self.assertTrue(all(s.status == SegmentSendStatus.PLANNED for s in record.segments))
        self.assertNotEqual(record.segments[0].visible_text_digest, record.segments[1].visible_text_digest)
        self.assertEqual(record.segments[0].visible_text, "第一句")

    def test_records_attempt_then_success_without_inventing_platform_id(self):
        record = self.ledger.begin_reply(target=target(), visible_segments=("发送内容",))
        segment_id = record.segments[0].segment_id

        attempted = self.ledger.mark_attempted(segment_id)
        succeeded = self.ledger.mark_succeeded(segment_id)

        self.assertEqual(attempted.segments[0].status, SegmentSendStatus.ATTEMPTED)
        self.assertEqual(succeeded.segments[0].status, SegmentSendStatus.SUCCEEDED)
        self.assertEqual(succeeded.segments[0].platform_message_id, "")
        self.assertIsNone(succeeded.segments[0].platform_receipt_source)
        self.assertTrue(succeeded.all_succeeded)

    def test_fresh_ledger_cannot_confirm_a_pre_restart_segment(self):
        record = self.ledger.begin_reply(target=target(), visible_segments=("发送内容",))
        segment_id = record.segments[0].segment_id
        self.ledger.mark_attempted(segment_id)

        reopened = InternalSendReceiptLedger(
            id_factory=lambda: "fresh-reply",
            now_fn=lambda: 200.0,
        )
        with self.assertRaises(KeyError):
            reopened.mark_succeeded(segment_id)
        self.assertIsNone(reopened.get_reply(record.internal_reply_id))

    def test_accepts_platform_id_only_with_an_explicit_typed_receipt_source(self):
        record = self.ledger.begin_reply(target=target(), visible_segments=("发送内容",))
        segment_id = record.segments[0].segment_id
        self.ledger.mark_attempted(segment_id)

        succeeded = self.ledger.mark_succeeded(
            segment_id,
            platform_message_id="platform-message-9",
            receipt_source=PlatformReceiptSource.ADAPTER_EXPLICIT_RESULT,
        )

        self.assertEqual(succeeded.segments[0].platform_message_id, "platform-message-9")
        self.assertEqual(
            succeeded.segments[0].platform_receipt_source,
            PlatformReceiptSource.ADAPTER_EXPLICIT_RESULT,
        )

    def test_rejects_unproven_platform_id_and_invalid_state_transitions(self):
        record = self.ledger.begin_reply(target=target(), visible_segments=("发送内容",))
        segment_id = record.segments[0].segment_id

        with self.assertRaises(ValueError):
            self.ledger.mark_succeeded(segment_id, platform_message_id="guessed-id")
        with self.assertRaises(ValueError):
            self.ledger.mark_succeeded(segment_id)
        self.ledger.mark_attempted(segment_id)
        with self.assertRaises(ValueError):
            self.ledger.mark_attempted(segment_id)

    def test_failed_attempt_records_only_failure_kind(self):
        record = self.ledger.begin_reply(target=target(), visible_segments=("隐私文本",))
        segment_id = record.segments[0].segment_id
        self.ledger.mark_attempted(segment_id)

        failed = self.ledger.mark_failed(segment_id, failure_kind="adapter_exception")
        metadata = failed.segments[0].trace_metadata()

        self.assertEqual(failed.segments[0].status, SegmentSendStatus.FAILED)
        self.assertEqual(failed.segments[0].failure_kind, "adapter_exception")
        self.assertNotIn("隐私文本", repr(metadata))
        self.assertFalse(failed.all_succeeded)

    def test_requires_exact_target_and_non_empty_segments(self):
        with self.assertRaises(ValueError):
            self.ledger.begin_reply(
                target=target(sender_key=""),
                visible_segments=("内容",),
            )
        with self.assertRaises(ValueError):
            self.ledger.begin_reply(target=target(), visible_segments=())
        with self.assertRaises(ValueError):
            self.ledger.begin_reply(target=target(), visible_segments=("",))

    def test_lookup_requires_internal_id_not_text_or_time(self):
        record = self.ledger.begin_reply(target=target(), visible_segments=("同样的文本",))

        self.assertIs(self.ledger.get_reply(record.internal_reply_id), record)
        self.assertIsNone(self.ledger.get_reply("同样的文本"))
        self.assertIsNone(self.ledger.get_reply("100.0"))

    def test_can_append_actual_fallback_segment_after_an_attempt_fails(self):
        record = self.ledger.begin_reply(target=target(), visible_segments=("第一句",))
        first_id = record.segments[0].segment_id
        self.ledger.mark_attempted(first_id)
        self.ledger.mark_failed(first_id, failure_kind="adapter_exception")

        fallback = self.ledger.append_segment(
            record.internal_reply_id,
            visible_text="第一句\n第二句",
        )

        self.assertEqual(fallback.segment_index, 1)
        self.assertEqual(fallback.status, SegmentSendStatus.PLANNED)
        updated = self.ledger.get_reply(record.internal_reply_id)
        self.assertEqual(len(updated.segments), 2)

    def test_sent_reply_record_contains_exact_target_and_real_successful_segments(self):
        record = self.ledger.begin_reply(
            target=target(referenced_message_id="quoted-message"),
            visible_segments=("第一句",),
            expression_ids=("expression-a",),
        )
        first_id = record.segments[0].segment_id
        self.ledger.mark_attempted(first_id)
        self.ledger.mark_succeeded(first_id)
        second = self.ledger.append_segment(
            record.internal_reply_id,
            visible_text="第二句",
        )
        self.ledger.mark_attempted(second.segment_id)
        self.ledger.mark_failed(second.segment_id, failure_kind="adapter_exception")

        sent = self.ledger.sent_reply_record(record.internal_reply_id)

        self.assertEqual(sent.reply_id, "shio-reply-a")
        self.assertEqual(sent.target_message_id, "incoming-message-1")
        self.assertEqual(sent.target_sender_key, "platform:p|user:u1")
        self.assertEqual(sent.target_referenced_message_id, "quoted-message")
        self.assertEqual(sent.successful_visible_text, "第一句")
        self.assertEqual([segment.visible_text for segment in sent.segments], [
            "第一句",
            "第二句",
        ])
        self.assertTrue(sent.has_failed_segments)
        self.assertTrue(sent.is_terminal)
        self.assertEqual(sent.expression_ids, ("expression-a",))
        self.assertGreater(sent.sent_at, 0)

    def test_sent_reply_trace_does_not_include_text_or_identity_values(self):
        record = self.ledger.begin_reply(
            target=target(),
            visible_segments=("不应进入 trace 的正文",),
        )
        segment_id = record.segments[0].segment_id
        self.ledger.mark_attempted(segment_id)
        self.ledger.mark_succeeded(segment_id)

        metadata = self.ledger.sent_reply_record(record.internal_reply_id).trace_metadata()
        rendered = repr(metadata)

        self.assertNotIn("不应进入 trace 的正文", rendered)
        self.assertNotIn("incoming-message-1", rendered)
        self.assertNotIn("platform:p|user:u1", rendered)
        self.assertEqual(metadata["succeeded_count"], 1)

    def test_ledger_is_bounded_and_eviction_removes_segment_indexes(self):
        ids = iter(f"reply-{index}" for index in range(20))
        ledger = InternalSendReceiptLedger(
            id_factory=lambda: next(ids),
            now_fn=lambda: 100.0,
            max_replies=16,
        )
        first = None
        for index in range(17):
            record = ledger.begin_reply(
                target=target(message_id=f"message-{index}"),
                visible_segments=(f"segment-{index}",),
            )
            if first is None:
                first = record
            segment_id = record.segments[0].segment_id
            ledger.mark_attempted(segment_id)
            ledger.mark_succeeded(segment_id)

        self.assertIsNone(ledger.sent_reply_record(first.internal_reply_id))
        with self.assertRaises(KeyError):
            ledger.mark_attempted(first.segments[0].segment_id)


if __name__ == "__main__":
    unittest.main()
