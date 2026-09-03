"""Independent R29 oracle for fixed ResultDecorate projection and send fencing."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest

try:
    from test_name_semantic_provider_routing import FakeEvent, MAIN, SYS001
except ModuleNotFoundError:  # pragma: no cover - package discovery path
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        FakeEvent,
        MAIN,
        SYS001,
    )


class _Image:
    type = "image"


class _Result:
    def __init__(self, chain, use_t2i):
        self.chain = list(chain)
        self.use_t2i_ = use_t2i

    def is_llm_result(self):
        return True


class _Event(FakeEvent):
    def __init__(self, chain, use_t2i=False):
        super().__init__()
        self._extras, self.sent, self._stopped = {}, [], False
        self.result = _Result(chain, use_t2i)
        self.unified_msg_origin = "recorded:r29"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_result(self):
        return self.result

    def get_platform_name(self):
        return "recorded"

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped

    async def send(self, chain):
        self.sent.append(chain)


class R29IndependentBubbleOracleTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, **overrides):
        base = {
            "provider_tts_settings": {"enable": False},
            "t2i": False,
            "t2i_word_threshold": 150,
            "platform_settings": {"forward_threshold": 9999},
        }
        base.update(overrides)
        return base

    def _plugin(self, config):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {"presentation": {
            "text_component_mode": "plugin", "text_component_min_segments": 1,
            "text_component_max_segments": 3, "bubble_send_min_wait_seconds": 0,
            "bubble_send_max_wait_seconds": 0,
        }}}
        plugin.context = SimpleNamespace(get_config=lambda **_kwargs: config)
        plugin._bubble_epoch, plugin._bubble_terminated = 0, False
        plugin._bubble_instance_token = object()
        return plugin

    def _event(self, chain, use_t2i=False):
        event = _Event(chain, use_t2i)
        event.set_extra(MAIN.SYS001_TURN_EXTRA, MAIN.create_snapshot(FakeEvent(), origin="direct"))
        event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, MAIN.TurnLifecycle())
        event.set_extra(
            "shio.sys001.segmented_reply_status",
            SYS001.SegmentedReplyCompatibility(False, "disabled"),
        )
        return event

    async def _stage(self, config, chain, use_t2i=False):
        plugin, event = self._plugin(config), self._event(chain, use_t2i)
        await plugin.stage_remaining_bubbles(event)
        return plugin, event

    async def test_fixed_t2i_strict_threshold_and_leading_plain_projection(self):
        """R28 fails this: it treated every True/None t2i flag as a conflict."""
        short = [MAIN.Plain("甲"), MAIN.Plain("乙"), MAIN.Plain("丙")]
        plugin, event = await self._stage(
            self._config(t2i=True, t2i_word_threshold=50), short, None
        )
        self.assertEqual("pending", event.get_extra("shio.sys001.bubble_delivery_status"))
        self.assertIsNotNone(event.get_extra("shio.sys001.pending_bubble_delivery"))
        self.assertIs(plugin, plugin)

        # ResultDecorate prefixes every leading Plain by two newlines and uses
        # a strict greater-than test.  23 + 23 source characters equal 50.
        equal = [MAIN.Plain("a" * 23), MAIN.Plain("b" * 23)]
        _plugin, event = await self._stage(
            self._config(t2i=True, t2i_word_threshold=50), equal, None
        )
        self.assertEqual("pending", event.get_extra("shio.sys001.bubble_delivery_status"))
        above = [MAIN.Plain("a" * 24), MAIN.Plain("b" * 23)]
        _plugin, event = await self._stage(
            self._config(t2i=True, t2i_word_threshold=50), above, None
        )
        self.assertEqual("fail_closed:official_t2i", event.get_extra("shio.sys001.bubble_delivery_status"))

        # An Image terminates the fixed leading-Plain scan; later text does
        # not enter the T2I input and ordinary disabled bubbles remain valid.
        _plugin, event = await self._stage(
            self._config(t2i=True, t2i_word_threshold=50),
            [_Image(), MAIN.Plain("a" * 80), MAIN.Plain("b")], None,
        )
        self.assertEqual("pending", event.get_extra("shio.sys001.bubble_delivery_status"))
        for use_t2i in (False, None):
            _plugin, event = await self._stage(self._config(t2i=False), short, use_t2i)
            self.assertEqual("pending", event.get_extra("shio.sys001.bubble_delivery_status"))

    async def test_incomplete_fixed_resultdecorate_shape_fails_closed_without_dropping_chain(self):
        chain = [MAIN.Plain("甲"), MAIN.Plain("乙"), MAIN.Plain("丙")]
        cases = (
            {},
            {"provider_tts_settings": {}, "t2i": False, "t2i_word_threshold": 50, "platform_settings": {"forward_threshold": 9}},
            {"provider_tts_settings": {"enable": False}, "t2i": False, "platform_settings": {"forward_threshold": 9}},
        )
        for config in cases:
            with self.subTest(config=config):
                _plugin, event = await self._stage(config, chain, False)
                self.assertIsNone(event.get_extra("shio.sys001.pending_bubble_delivery"))
                self.assertTrue(event.get_extra("shio.sys001.bubble_delivery_status").startswith("fail_closed:"))
                self.assertEqual(["甲", "乙", "丙"], [part.text for part in event.result.chain])

        plugin, event = await self._stage(self._config(), chain, False)
        del event.result.use_t2i_
        await plugin.stage_remaining_bubbles(event)
        self.assertEqual("fail_closed:official_result_shape_unknown", event.get_extra("shio.sys001.bubble_delivery_status"))

    async def test_instance_token_unknown_pending_and_last_send_barrier_stop_meme(self):
        plugin, event = await self._stage(self._config(), [MAIN.Plain("甲"), MAIN.Plain("乙")])
        pending = event.get_extra("shio.sys001.pending_bubble_delivery")
        self.assertIsNotNone(pending)
        event.set_extra("shio.sys001.pending_bubble_delivery", replace(pending, instance_token=object()))
        await plugin.send_remaining_bubbles(event)
        self.assertTrue(event.is_stopped())
        self.assertEqual("stale", event.get_extra("shio.sys001.bubble_delivery_status"))
        self.assertIsNone(event.get_extra("shio.sys001.pending_bubble_delivery"))

        plugin, event = await self._stage(self._config(), [MAIN.Plain("甲"), MAIN.Plain("乙")])
        old_module_pending = SimpleNamespace(**event.get_extra("shio.sys001.pending_bubble_delivery").__dict__) if hasattr(event.get_extra("shio.sys001.pending_bubble_delivery"), "__dict__") else object()
        event.set_extra("shio.sys001.pending_bubble_delivery", old_module_pending)
        await plugin.send_remaining_bubbles(event)
        self.assertTrue(event.is_stopped())
        self.assertEqual("stale", event.get_extra("shio.sys001.bubble_delivery_status"))

        plugin, event = await self._stage(self._config(), [MAIN.Plain("甲"), MAIN.Plain("乙")])
        async def drift_after_send(chain):
            event.sent.append(chain)
            plugin._bubble_epoch += 1
        event.send = drift_after_send
        await plugin.send_remaining_bubbles(event)
        self.assertEqual(1, len(event.sent))
        self.assertTrue(event.is_stopped())
        self.assertEqual("stale", event.get_extra("shio.sys001.bubble_delivery_status"))
