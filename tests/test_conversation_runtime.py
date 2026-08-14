from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from astrbot_plugin_shio.core.conversation_runtime import ConversationRuntime


class FakeLogger:
    def warning(self, *args, **kwargs):
        pass


class ConversationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = ConversationRuntime(
            Path(self.temp.name),
            FakeLogger(),
            random_fn=lambda: 0.0,
            now_fn=lambda: 1000.0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def ingest(
        self,
        text: str,
        *,
        group: str = "group-a",
        sender: str = "guest-a",
        created_at: float = 1000.0,
        direct: bool = False,
    ):
        return self.runtime.ingest(
            platform_id="adapter-a",
            bot_id="bot-a",
            group_id=group,
            unified_msg_origin=f"adapter-a:GroupMessage:{group}",
            sender_id=sender,
            sender_name=sender,
            text=text,
            is_owner=False,
            is_direct_wake=direct,
            created_at=created_at,
        )

    def test_scope_separates_platform_bot_group_and_user(self):
        group_a = self.runtime.group_scope("adapter-a", "bot-a", "group-a")
        group_b = self.runtime.group_scope("adapter-a", "bot-a", "group-b")
        bot_b = self.runtime.group_scope("adapter-a", "bot-b", "group-a")
        self.assertNotEqual(group_a, group_b)
        self.assertNotEqual(group_a, bot_b)
        self.assertNotEqual(
            self.runtime.identity_key(group_a, "guest-a"),
            self.runtime.identity_key(group_a, "guest-b"),
        )

    def test_stale_debounce_event_waits_and_latest_question_can_reply(self):
        first = self.ingest("我先说一半")
        latest = self.ingest("你们觉得这个模型怎么样？", sender="guest-b", created_at=1001)
        self.assertIsNotNone(first)
        self.assertIsNotNone(latest)
        stale = self.runtime.decide_participation(
            first.scope_key,
            first.sequence,
            threshold=4.2,
            cooldown_seconds=45,
            max_replies_per_hour=8,
            recent_window_seconds=180,
            recent_window_messages=12,
            persona_names=["亚托莉"],
            now=1005,
        )
        self.assertEqual(stale.action, "wait")

        decision = self.runtime.decide_participation(
            latest.scope_key,
            latest.sequence,
            threshold=4.2,
            cooldown_seconds=45,
            max_replies_per_hour=8,
            recent_window_seconds=180,
            recent_window_messages=12,
            persona_names=["亚托莉"],
            now=1005,
        )
        self.assertEqual(decision.action, "reply")
        self.assertEqual(decision.target.sender_id, "guest-b")

    def test_participation_never_selects_an_older_high_score_sender(self):
        self.ingest("亚托莉，你觉得这个问题应该怎么解决？", sender="guest-a")
        latest = self.ingest("什么片？", sender="guest-b", created_at=1001)

        decision = self.runtime.decide_participation(
            latest.scope_key,
            latest.sequence,
            threshold=2.5,
            cooldown_seconds=45,
            max_replies_per_hour=8,
            recent_window_seconds=180,
            recent_window_messages=12,
            persona_names=["亚托莉"],
            now=1005,
        )

        self.assertEqual(decision.action, "reply")
        self.assertEqual(decision.target.sequence, latest.sequence)
        self.assertEqual(decision.target.sender_id, "guest-b")

    def test_ambient_target_becomes_stale_when_new_message_arrives(self):
        target = self.ingest("你们觉得这部电影怎么样？", sender="guest-a")
        self.assertTrue(self.runtime.is_current_target(target.scope_key, target.sequence))

        self.ingest("什么片？", sender="guest-b", created_at=1001)

        self.assertFalse(self.runtime.is_current_target(target.scope_key, target.sequence))

    def test_ambient_target_remains_relevant_during_short_same_topic_exchange(self):
        target = self.ingest("你们觉得这部电影怎么样？", sender="guest-a")
        self.ingest("这部电影我也看了", sender="guest-b", created_at=1001)
        self.ingest("结尾还不错", sender="guest-c", created_at=1002)

        self.assertTrue(
            self.runtime.is_target_relevant(
                target.scope_key,
                target.sequence,
                max_new_messages=4,
                max_age_seconds=45,
                now=1005,
            )
        )

    def test_ambient_target_expires_on_direct_wake_or_too_many_messages(self):
        target = self.ingest("你们觉得这部电影怎么样？", sender="guest-a")
        self.ingest("亚托莉你先回答我", sender="guest-b", created_at=1001, direct=True)
        self.assertFalse(
            self.runtime.is_target_relevant(
                target.scope_key,
                target.sequence,
                max_new_messages=4,
                max_age_seconds=45,
                now=1005,
            )
        )

    def test_reply_cooldown_prevents_duplicate_participation(self):
        target = self.ingest("亚托莉，你觉得这个怎么样？")
        self.runtime.record_bot_reply(
            scope_key=target.scope_key,
            target_sender_id=target.sender_id,
            reply_text="让我看看。",
            target_sequence=target.sequence,
            now=1002,
        )
        latest = self.ingest("那这个呢？", sender="guest-b", created_at=1010)
        decision = self.runtime.decide_participation(
            latest.scope_key,
            latest.sequence,
            threshold=2.0,
            cooldown_seconds=45,
            max_replies_per_hour=8,
            recent_window_seconds=180,
            recent_window_messages=12,
            persona_names=["亚托莉"],
            now=1012,
        )
        self.assertEqual(decision.action, "wait")
        self.assertIn("刚刚说过话", decision.reasons[0])

    def test_feedback_is_aggregated_without_persisting_chat_text(self):
        target = self.ingest("来接一句话吧")
        self.runtime.record_bot_reply(
            scope_key=target.scope_key,
            target_sender_id=target.sender_id,
            reply_text="这是不会被持久化的机器人原文",
            expression_ids=["proud-cute"],
            now=1001,
        )
        self.ingest("哈哈，太可爱了", sender="guest-b", created_at=1005)
        self.runtime.flush()

        payload_text = (Path(self.temp.name) / "social_state.json").read_text(
            encoding="utf-8"
        )
        payload = json.loads(payload_text)
        profile = payload["profiles"][
            self.runtime.identity_key(target.scope_key, target.sender_id)
        ]
        self.assertEqual(profile["positive_feedback"], 1)
        self.assertGreater(payload["expression_feedback"]["proud-cute"], 0)
        self.assertNotIn("来接一句话吧", payload_text)
        self.assertNotIn("不会被持久化", payload_text)

    def test_quiet_topic_requires_explicit_group_whitelist_and_active_hours(self):
        now = datetime(2026, 8, 10, 12, 0).timestamp()
        target = self.ingest("今天大家都在聊模型", created_at=now - 7200)
        none = self.runtime.quiet_topic_candidates(
            group_whitelist=set(),
            idle_seconds=3600,
            cooldown_seconds=7200,
            max_per_day=2,
            active_start="09:00",
            active_end="23:30",
            now=now,
        )
        self.assertEqual(none, [])
        candidates = self.runtime.quiet_topic_candidates(
            group_whitelist={target.group_id},
            idle_seconds=3600,
            cooldown_seconds=7200,
            max_per_day=2,
            active_start="09:00",
            active_end="23:30",
            now=now,
        )
        self.assertEqual([item.group_id for item in candidates], [target.group_id])

    def test_quiet_topic_failure_uses_short_backoff_not_full_success_cooldown(self):
        now = datetime(2026, 8, 10, 12, 0).timestamp()
        target = self.ingest("今天大家都在聊模型", created_at=now - 7200)
        self.runtime.mark_quiet_topic_attempt(target.scope_key, now=now)
        blocked = self.runtime.quiet_topic_candidates(
            group_whitelist={target.group_id},
            idle_seconds=3600,
            cooldown_seconds=14400,
            failure_backoff_seconds=600,
            max_per_day=2,
            active_start="09:00",
            active_end="23:30",
            now=now + 300,
        )
        ready = self.runtime.quiet_topic_candidates(
            group_whitelist={target.group_id},
            idle_seconds=3600,
            cooldown_seconds=14400,
            failure_backoff_seconds=600,
            max_per_day=2,
            active_start="09:00",
            active_end="23:30",
            now=now + 601,
        )
        self.assertEqual(blocked, [])
        self.assertEqual([item.group_id for item in ready], [target.group_id])

    def test_active_topic_uses_natural_lull_without_probability_or_long_idle(self):
        now = datetime(2026, 8, 10, 12, 0).timestamp()
        target = self.ingest("刚才大家还在聊本地模型", created_at=now - 900)
        candidates = self.runtime.active_topic_candidates(
            group_whitelist={target.group_id},
            minimum_lull_seconds=180,
            quiet_idle_seconds=5400,
            minimum_observation_seconds=600,
            cooldown_seconds=14400,
            bot_reply_guard_seconds=600,
            failure_backoff_seconds=600,
            max_per_day=2,
            active_start="09:00",
            active_end="23:30",
            now=now,
        )
        self.assertEqual([item.group_id for item in candidates], [target.group_id])

    def test_normal_bot_reply_only_uses_short_initiative_guard(self):
        now = datetime(2026, 8, 10, 12, 0).timestamp()
        target = self.ingest("刚才大家还在聊本地模型", created_at=now - 3600)
        self.runtime.record_bot_reply(
            scope_key=target.scope_key,
            target_sender_id=target.sender_id,
            reply_text="这是一次普通点名回复",
            now=now - 1200,
        )
        candidates = self.runtime.active_topic_candidates(
            group_whitelist={target.group_id},
            minimum_lull_seconds=180,
            quiet_idle_seconds=5400,
            minimum_observation_seconds=600,
            cooldown_seconds=14400,
            bot_reply_guard_seconds=600,
            failure_backoff_seconds=600,
            max_per_day=2,
            active_start="09:00",
            active_end="23:30",
            now=now,
        )
        self.assertEqual([item.group_id for item in candidates], [target.group_id])


if __name__ == "__main__":
    unittest.main()
