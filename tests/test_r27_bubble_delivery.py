"""R27 recorded multi-bubble regressions at the public AstrBot hook boundary."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

try:
    from test_name_semantic_provider_routing import FakeEvent, MAIN, SYS001
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        FakeEvent,
        MAIN,
        SYS001,
    )


class _Result:
    def __init__(self, chain):
        self.chain = list(chain)
        self.use_t2i_ = False

    def is_llm_result(self):
        return True


class _Event(FakeEvent):
    def __init__(self, chain):
        super().__init__()
        self._extras = {}
        self.result = _Result(chain)
        self.sent = []
        self.stopped = False

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_result(self):
        return self.result

    def get_platform_name(self):
        return "recorded"

    async def send(self, chain):
        self.sent.append(chain)

    def stop_event(self):
        self.stopped = True


class _Reply:
    type = "reply"


class _At:
    type = "at"


class _Record:
    type = "record"


class R27BubbleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, *, minimum=1, maximum=3):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {"presentation": {
            "text_component_mode": "plugin",
            "text_component_min_segments": minimum,
            "text_component_max_segments": maximum,
            "bubble_send_min_wait_seconds": 0,
            "bubble_send_max_wait_seconds": 0,
        }}}
        plugin._bubble_epoch = 0
        plugin._bubble_terminated = False
        plugin.context = SimpleNamespace(
            get_config=lambda **_kwargs: {
                "provider_tts_settings": {"enable": False},
                "t2i": False,
                "t2i_word_threshold": 50,
                "platform_settings": {"forward_threshold": 9999},
            }
        )
        return plugin

    def _prepared_event(self, chain, *, status="disabled", origin="direct"):
        event = _Event(chain)
        event.set_extra(MAIN.SYS001_TURN_EXTRA, MAIN.create_snapshot(FakeEvent(), origin=origin))
        event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, MAIN.TurnLifecycle())
        event.set_extra(
            "shio.sys001.segmented_reply_status",
            SYS001.SegmentedReplyCompatibility(status == "compatible", status),
        )
        return event

    async def test_disabled_recorded_r26_one_send_regresses_to_r27_three_text_bubbles(self):
        """The old disabled RespondStage would emit this three-Plain chain once."""
        plugin = self._plugin(minimum=1, maximum=3)
        event = self._prepared_event([MAIN.Plain("甲。"), MAIN.Plain("乙。"), MAIN.Plain("丙。")])
        sleeps = []

        async def sleep(value):
            sleeps.append(value)

        plugin._bubble_sleep = sleep
        await plugin.stage_remaining_bubbles(event)
        # This is the exact first unit left for the fixed public RespondStage.
        self.assertEqual(["甲。"], [part.text for part in event.result.chain])
        await plugin.send_remaining_bubbles(event)
        self.assertEqual([["乙。"], ["丙。"]], [[part.text for part in chain.chain] for chain in event.sent])
        self.assertEqual([0.0, 0.0], sleeps)
        self.assertEqual("sent", event.get_extra("shio.sys001.bubble_delivery_status"))

    async def test_compatible_official_path_never_stages_or_duplicates(self):
        plugin = self._plugin()
        event = self._prepared_event([MAIN.Plain("甲。"), MAIN.Plain("乙。")], status="compatible")
        await plugin.stage_remaining_bubbles(event)
        await plugin.send_remaining_bubbles(event)
        self.assertEqual(["甲。", "乙。"], [part.text for part in event.result.chain])
        self.assertEqual([], event.sent)
        self.assertEqual("official_compatible", event.get_extra("shio.sys001.bubble_delivery_status"))

    async def test_words_conflict_keeps_full_chain_and_reports_conflict(self):
        plugin = self._plugin()
        event = self._prepared_event([MAIN.Plain("一。"), MAIN.Plain("二。"), MAIN.Plain("三。")], status="split_mode_not_regex")
        await plugin.stage_remaining_bubbles(event)
        self.assertEqual(["一。", "二。", "三。"], [part.text for part in event.result.chain])
        self.assertEqual("conflict:split_mode_not_regex", event.get_extra("shio.sys001.bubble_delivery_status"))

    async def test_reply_at_first_and_record_order_are_preserved(self):
        plugin = self._plugin()
        reply, at, record = _Reply(), _At(), _Record()
        event = self._prepared_event([reply, at, MAIN.Plain("一。"), MAIN.Plain("二。"), record])
        await plugin.stage_remaining_bubbles(event)
        self.assertEqual([reply, at, "一。"], [*event.result.chain[:2], event.result.chain[2].text])
        await plugin.send_remaining_bubbles(event)
        self.assertEqual([["二。"], [record]], [[part.text if hasattr(part, "text") else part for part in chain.chain] for chain in event.sent])

    async def test_reentry_failure_cancellation_and_stale_generation_stop_without_retry(self):
        plugin = self._plugin()
        event = self._prepared_event([MAIN.Plain("一。"), MAIN.Plain("二。"), MAIN.Plain("三。")])
        await plugin.stage_remaining_bubbles(event)
        original = event.get_extra("shio.sys001.pending_bubble_delivery")
        await plugin.send_remaining_bubbles(event)
        await plugin.send_remaining_bubbles(event)
        self.assertEqual(2, len(event.sent))
        self.assertIsNone(event.get_extra("shio.sys001.pending_bubble_delivery"))

        event = self._prepared_event([MAIN.Plain("一。"), MAIN.Plain("二。")])
        await plugin.stage_remaining_bubbles(event)
        async def fail(_chain):
            raise RuntimeError("recorded send failure")
        event.send = fail
        await plugin.send_remaining_bubbles(event)
        self.assertEqual("send_failed", event.get_extra("shio.sys001.bubble_delivery_status"))

        event = self._prepared_event([MAIN.Plain("一。"), MAIN.Plain("二。")])
        await plugin.stage_remaining_bubbles(event)
        event.set_extra("shio.sys001.generation", 7)
        await plugin.send_remaining_bubbles(event)
        self.assertEqual("stale", event.get_extra("shio.sys001.bubble_delivery_status"))

        event = self._prepared_event([MAIN.Plain("一。"), MAIN.Plain("二。")])
        await plugin.stage_remaining_bubbles(event)
        async def cancelled(_value):
            raise asyncio.CancelledError
        plugin._bubble_sleep = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await plugin.send_remaining_bubbles(event)
        self.assertEqual("cancelled", event.get_extra("shio.sys001.bubble_delivery_status"))

    def test_wait_bounds_are_finite_nonnegative_and_ordered(self):
        self.assertEqual((0.0, 1.5), SYS001.bubble_send_wait_bounds(0, "1.5"))
        for values in ((-1, 0), (2, 1), (float("nan"), 1), (0, float("inf")), ("x", 0)):
            with self.subTest(values=values):
                self.assertIsNone(SYS001.bubble_send_wait_bounds(*values))
