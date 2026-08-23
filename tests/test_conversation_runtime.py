from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.conversation_runtime import ConversationRuntime


class FakeLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class ConversationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1_765_000_000.0
        self.runtime = ConversationRuntime(
            Path(self.temp.name),
            FakeLogger(),
            max_messages_per_group=20,
            now_fn=lambda: self.now,
        )
        self.scope = self.runtime.group_scope("platform", "bot", "group")

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, sender="guest-a", text="当前消息", **kwargs):
        return self.runtime.ingest(
            platform_id="platform",
            bot_id="bot",
            group_id="group",
            unified_msg_origin="test",
            sender_id=sender,
            sender_name=sender,
            text=text,
            is_owner=False,
            is_direct_wake=False,
            created_at=self.now,
            **kwargs,
        )

    def test_ingest_is_identity_scoped_and_keeps_only_bounded_memory(self):
        first = self.ingest(message_id="message-a")
        self.now += 1
        second = self.ingest(sender="guest-b", message_id="message-b")

        self.assertEqual(first.scope_key, self.scope)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(self.runtime.groups[self.scope].messages[-1].sender_id, "guest-b")

    def test_same_reply_segments_update_one_observation_without_double_counting(self):
        self.ingest()
        kwargs = dict(
            scope_key=self.scope,
            target_sender_id="guest-a",
            expression_ids=["expression-a"],
            internal_reply_id="reply-a",
            feedback_window_seconds=600,
        )
        self.runtime.record_bot_reply(reply_text="第一段", **kwargs)
        self.runtime.record_bot_reply(reply_text="第一段\n第二段", **kwargs)

        profile = self.runtime.profiles[self.runtime.identity_key(self.scope, "guest-a")]
        self.assertEqual(profile.interaction_count, 1)
        self.assertEqual(self.runtime.observations[self.scope].reply_text, "第一段\n第二段")

    def test_explicit_target_feedback_updates_only_that_users_profile(self):
        self.ingest()
        self.runtime.record_bot_reply(
            scope_key=self.scope,
            target_sender_id="guest-a",
            reply_text="回复",
            expression_ids=["expression-a"],
            internal_reply_id="reply-a",
            sent_platform_message_ids=["bot-message-a"],
        )
        self.now += 2
        self.ingest(
            text="好可爱，说得对",
            reply_to_message_id="bot-message-a",
        )

        target = self.runtime.profiles[self.runtime.identity_key(self.scope, "guest-a")]
        self.assertEqual(target.positive_feedback, 1)
        self.assertGreater(self.runtime.expression_feedback["expression-a"], 0)

    def test_other_users_adjacent_reaction_does_not_change_target_affinity(self):
        self.ingest()
        self.runtime.record_bot_reply(
            scope_key=self.scope,
            target_sender_id="guest-a",
            reply_text="回复",
            expression_ids=["expression-a"],
            internal_reply_id="reply-a",
        )
        self.now += 2
        self.ingest(sender="guest-b", text="哈哈，好可爱")

        target = self.runtime.profiles[self.runtime.identity_key(self.scope, "guest-a")]
        self.assertEqual(target.positive_feedback, 0)
        self.assertIn("expression-a", self.runtime.group_expression_feedback[self.scope])

    def test_expired_feedback_is_ignored(self):
        self.ingest()
        self.runtime.record_bot_reply(
            scope_key=self.scope,
            target_sender_id="guest-a",
            reply_text="回复",
            expression_ids=["expression-a"],
            internal_reply_id="reply-a",
            feedback_window_seconds=60,
        )
        self.now += 61
        self.ingest(text="说得对")

        target = self.runtime.profiles[self.runtime.identity_key(self.scope, "guest-a")]
        self.assertEqual(target.positive_feedback, 0)
        self.assertNotIn(self.scope, self.runtime.observations)

    def test_persistence_contains_aggregates_not_chat_text(self):
        self.ingest(text="这是真实群聊正文")
        self.runtime.record_bot_reply(
            scope_key=self.scope,
            target_sender_id="guest-a",
            reply_text="这是真实机器人回复",
            expression_ids=["expression-a"],
            internal_reply_id="reply-a",
        )
        self.runtime.flush()

        raw = (Path(self.temp.name) / "social_state.json").read_text(encoding="utf-8")
        payload = json.loads(raw)
        self.assertEqual(payload["version"], 3)
        self.assertNotIn("这是真实群聊正文", raw)
        self.assertNotIn("这是真实机器人回复", raw)
        self.assertNotIn("quiet_topic", raw)

    def test_runtime_source_has_no_reply_or_participation_generator(self):
        source = (Path(__file__).parents[1] / "core" / "conversation_runtime.py").read_text(
            encoding="utf-8"
        )
        for token in (
            "decide_participation",
            "quiet_topic_candidates",
            "active_topic_candidates",
            "ParticipationDecision",
        ):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
