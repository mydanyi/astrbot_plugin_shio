from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.recovery_queue import PendingReplyStore


class FakeLogger:
    def warning(self, *args, **kwargs):
        pass


class RecoveryQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.store = PendingReplyStore(
            Path(self.temp.name),
            FakeLogger(),
            now_fn=lambda: self.now,
        )

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self):
        return self.store.enqueue(
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            platform_id="adapter-a",
            bot_id="bot-a",
            chat_type="group",
            group_id="123",
            sender_id="guest-a",
            sender_name="群友甲",
            message_id="8899",
            current_message="为什么没有回答？",
            contexts=[{"role": "user", "content": "前文"}],
            reply_shape="chat_bubbles",
            initial_delay_seconds=30,
            ttl_seconds=3600,
            failure_reason="offline",
        )

    def test_queue_is_persistent_and_deduplicated(self):
        first, created = self.enqueue()
        duplicate, created_again = self.enqueue()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, duplicate.id)
        loaded = PendingReplyStore(
            Path(self.temp.name), FakeLogger(), now_fn=lambda: self.now
        )
        self.assertEqual(list(loaded.items), [first.id])
        raw = json.loads(
            (Path(self.temp.name) / "pending_replies.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("api_key", json.dumps(raw))

    def test_due_backoff_expedite_and_complete(self):
        item, _ = self.enqueue()
        self.assertEqual(self.store.due(now=1029), [])
        self.assertEqual([entry.id for entry in self.store.due(now=1030)], [item.id])
        self.now = 1030
        self.store.mark_failed(
            item.id,
            reason="still offline",
            delays_seconds=[120, 300],
            max_attempts=4,
        )
        self.assertEqual(self.store.due(now=1149), [])
        self.store.expedite(now=1040)
        self.assertEqual([entry.id for entry in self.store.due(now=1040)], [item.id])
        self.store.complete(item.id)
        self.assertEqual(self.store.items, {})

    def test_expired_item_is_silently_pruned(self):
        item, _ = self.enqueue()
        self.now = item.expires_at + 1
        self.assertEqual(self.store.due(), [])
        self.assertEqual(self.store.items, {})


if __name__ == "__main__":
    unittest.main()
