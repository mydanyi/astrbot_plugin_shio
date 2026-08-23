import unittest

from astrbot_plugin_shio.core.generation_epoch import (
    EpochBindingError,
    GenerationEpochRegistry,
    ensure_event_generation_epoch,
    event_epoch_validation,
)
from astrbot_plugin_shio.core.identity import TurnEnvelope


def envelope(scope="scope-a", session="session-a", message="message-a"):
    return TurnEnvelope(
        session_id=session,
        message_id=message,
        scope_key=scope,
        sender_key=f"{scope}|user:a",
        sender_id="user-a",
        platform_id="platform-a",
        bot_id="bot-a",
        chat_type="group",
        group_id=session,
        reply_to_message_id="",
        reply_to_sender_id="",
        timestamp=1000.0,
        timestamp_source="message",
        source_kind="inbound",
        degradation_reasons=(),
    )


class Event:
    def __init__(self):
        self.extras = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class GenerationEpochRegistryTests(unittest.TestCase):
    def test_new_inbound_in_same_scope_invalidates_previous_snapshot(self):
        registry = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        first = registry.advance(envelope(message="message-a"))
        second = registry.advance(envelope(message="message-b"))

        self.assertFalse(registry.validate(first).is_current)
        self.assertEqual(registry.validate(first).reason_code, "stale_generation_epoch")
        self.assertTrue(registry.validate(second).is_current)
        self.assertEqual(second.epoch, first.epoch + 1)

    def test_different_sessions_do_not_invalidate_each_other(self):
        registry = GenerationEpochRegistry()
        first = registry.advance(envelope(scope="scope-a", session="session-a"))
        second = registry.advance(envelope(scope="scope-b", session="session-b"))

        self.assertTrue(registry.validate(first).is_current)
        self.assertTrue(registry.validate(second).is_current)

    def test_same_event_is_advanced_only_once(self):
        registry = GenerationEpochRegistry()
        event = Event()

        first = ensure_event_generation_epoch(event, registry, envelope())
        second = ensure_event_generation_epoch(event, registry, envelope())

        self.assertIs(first, second)
        self.assertEqual(first.epoch, 1)
        self.assertTrue(event_epoch_validation(event, registry).is_current)

    def test_next_epoch_is_read_only_and_expected_advance_is_exact(self):
        registry = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        turn = envelope()

        self.assertEqual(registry.current(turn.scope_key), 0)
        self.assertEqual(registry.next_epoch(turn), 1)
        self.assertEqual(registry.next_epoch(turn), 1)
        self.assertEqual(registry.current(turn.scope_key), 0)

        snapshot = registry.advance(turn, expected_epoch=1)
        self.assertEqual(snapshot.epoch, 1)
        self.assertEqual(registry.current(turn.scope_key), 1)
        with self.assertRaises(EpochBindingError):
            registry.advance(turn, expected_epoch=3)
        self.assertEqual(registry.current(turn.scope_key), 1)

    def test_passive_snapshot_binds_wait_without_superseding_active_reply(self):
        registry = GenerationEpochRegistry(now_fn=lambda: 1000.0)
        active = registry.advance(envelope(message="active"), expected_epoch=1)
        passive = registry.issue_passive(
            envelope(message="ambient"),
            expected_epoch=2,
        )

        self.assertEqual(passive.epoch, 2)
        self.assertEqual(registry.current(active.scope_key), 1)
        self.assertTrue(registry.validate(active).is_current)
        passive_validation = registry.validate(passive)
        self.assertFalse(passive_validation.is_current)
        self.assertEqual(
            passive_validation.reason_code,
            "passive_generation_epoch",
        )

        next_active = registry.advance(
            envelope(message="next-active"),
            expected_epoch=2,
        )
        self.assertTrue(registry.validate(next_active).is_current)
        self.assertFalse(registry.validate(active).is_current)
        self.assertEqual(registry.current(active.scope_key), 2)

    def test_missing_session_or_scope_fails_closed(self):
        registry = GenerationEpochRegistry()

        with self.assertRaises(EpochBindingError):
            registry.advance(envelope(session=""))
        with self.assertRaises(EpochBindingError):
            registry.advance(envelope(scope=""))

    def test_trace_does_not_include_scope_or_session_values(self):
        snapshot = GenerationEpochRegistry().advance(envelope())
        rendered = repr(snapshot.trace_metadata())

        self.assertNotIn("scope-a", rendered)
        self.assertNotIn("session-a", rendered)


if __name__ == "__main__":
    unittest.main()
