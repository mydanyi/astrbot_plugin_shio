"""R30 independent regressions for compat observation and fixed priority/T2I init."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

try:
    import test_r14_recorded_official_replay as R14
    import test_r29_bubble_instance_and_t2i_oracle as R29
    from test_name_semantic_provider_routing import FakeEvent, MAIN, SYS001
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests import test_r14_recorded_official_replay as R14
    from astrbot_plugin_shio.tests import test_r29_bubble_instance_and_t2i_oracle as R29
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import FakeEvent, MAIN, SYS001


class R30OfficialConfigAndPriorityTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, config):
        return R29.R29IndependentBubbleOracleTests()._plugin(config)

    def _event(self):
        event = R29._Event([MAIN.Plain("甲"), MAIN.Plain("乙"), MAIN.Plain("丙")], False)
        event.platform_meta = SimpleNamespace(supports_segmented_reply=True)
        event.set_extra(MAIN.SYS001_TURN_EXTRA, MAIN.create_snapshot(FakeEvent(), origin="direct"))
        event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, MAIN.TurnLifecycle())
        return event

    async def _fixed_t2i_threshold(self, raw_threshold: object) -> int:
        """Return the actual fixed ResultDecorate initialization result."""
        sources = R14._read_archives()
        namespace = {"re": __import__("re"), "registered_stages": []}
        initialize = R14._method(
            sources["astrbot/core/pipeline/result_decorate/stage.py"],
            "ResultDecorateStage", "initialize", namespace,
        )
        config = {
            "platform_settings": {
                "reply_prefix": "", "reply_with_mention": False,
                "reply_with_quote": False, "forward_threshold": 9,
                "segmented_reply": {
                    "words_count_threshold": 1, "enable": False,
                    "only_llm_result": True, "split_mode": "regex",
                    "regex": "", "content_cleanup_rule": "",
                },
            },
            "t2i_word_threshold": raw_threshold, "t2i_strategy": "local",
            "t2i_active_template": "x", "provider_tts_settings": {"enable": False},
            "content_safety": {"also_use_in_response": False},
        }
        fixed = SimpleNamespace()
        await initialize(fixed, SimpleNamespace(astrbot_config=config))
        return fixed.t2i_word_threshold

    async def _run_fixed_decorate_with_staging(
        self, *, chain, prefix: str, t2i: bool, forward_threshold: int,
        use_t2i=None, platform="recorded",
    ):
        """Run fixed ResultDecorate after its real decorating-hook boundary."""
        sources = R14._read_archives()
        rendered, hook_trace = [], []

        class Node:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        async def render_t2i(text, **_kwargs):
            rendered.append(text)
            return ""

        handler = SimpleNamespace(
            handler_module_path="shio", handler_name="stage_remaining_bubbles",
        )
        namespace = {
            "asyncio": asyncio, "re": re, "time": __import__("time"),
            "traceback": __import__("traceback"), "Plain": MAIN.Plain,
            "Image": R14.Image, "At": R14.At, "Reply": R14.Reply,
            "Record": R14.Record, "Node": Node, "Json": object,
            "ResultContentType": SimpleNamespace(STREAMING_RESULT="streaming", STREAMING_FINISH="finish"),
            "logger": R14._Log(), "registered_stages": [],
            "star_map": {"shio": SimpleNamespace(name="Shio")},
            "EventType": SimpleNamespace(OnDecoratingResultEvent="decorating"),
            "SessionServiceManager": SimpleNamespace(
                should_process_tts_request=lambda _event: asyncio.sleep(0, result=False)
            ),
            "html_renderer": SimpleNamespace(render_t2i=render_t2i),
            "star_handlers_registry": SimpleNamespace(
                get_handlers_by_event_type=lambda *_args, **_kwargs: [handler]
            ),
        }
        initialize = R14._method(
            sources["astrbot/core/pipeline/result_decorate/stage.py"],
            "ResultDecorateStage", "initialize", namespace,
        )
        process = R14._method(
            sources["astrbot/core/pipeline/result_decorate/stage.py"],
            "ResultDecorateStage", "process", namespace,
        )
        config = {
            "provider_tts_settings": {"enable": False}, "t2i": t2i,
            "t2i_word_threshold": 1, "t2i_strategy": "local",
            "t2i_active_template": "fixed", "content_safety": {"also_use_in_response": False},
            "provider_settings": {},
            "platform_settings": {
                "reply_prefix": prefix, "reply_with_mention": False,
                "reply_with_quote": False, "forward_threshold": forward_threshold,
                "segmented_reply": {
                    "words_count_threshold": 1, "enable": False, "only_llm_result": True,
                    "split_mode": "regex", "regex": "", "content_cleanup_rule": "",
                },
            },
        }
        helper = R29.R29IndependentBubbleOracleTests()
        plugin, event = helper._plugin(config), helper._event(chain, use_t2i)
        event.plugins_name = []
        event.result.result_content_type = "final"
        event.get_platform_name = lambda: platform

        async def ordered_staging(event):
            hook_trace.append([getattr(part, "text", None) for part in event.result.chain])
            await plugin.stage_remaining_bubbles(event)

        handler.handler = ordered_staging
        stage = SimpleNamespace()
        fixed_ctx = SimpleNamespace(
            astrbot_config=config,
            plugin_manager=SimpleNamespace(
                context=SimpleNamespace(
                    get_using_tts_provider_async=lambda _umo: asyncio.sleep(0, result=None)
                )
            ),
        )
        await initialize(stage, fixed_ctx)
        self.assertEqual(50, stage.t2i_word_threshold)
        await R14._drain(process(stage, event))
        return plugin, event, rendered, hook_trace

    async def test_bad_compat_config_is_readable_fail_closed_and_keeps_chain(self):
        """R29 failed: observer left status None and staging silently returned."""
        for config in (None, RuntimeError("recorded")):
            with self.subTest(config=type(config).__name__):
                plugin = self._plugin(R29.R29IndependentBubbleOracleTests()._config())
                if isinstance(config, Exception):
                    plugin.context = SimpleNamespace(get_config=lambda **_kwargs: (_ for _ in ()).throw(config))
                else:
                    plugin.context = SimpleNamespace(get_config=lambda **_kwargs: config)
                event = self._event()
                plugin._observe_segmented_reply_compatibility(event)
                await plugin.stage_remaining_bubbles(event)
                self.assertEqual(["甲", "乙", "丙"], [part.text for part in event.result.chain])
                self.assertIsNone(event.get_extra("shio.sys001.pending_bubble_delivery"))
                self.assertTrue(event.get_extra("shio.sys001.bubble_delivery_status").startswith("fail_closed:official_config_"))

    def test_fixed_initialize_clamps_and_actual_hook_priorities_are_ordered(self):
        import asyncio
        self.assertEqual(50, asyncio.run(self._fixed_t2i_threshold(1)))
        tree = ast.parse(Path(MAIN.__file__).read_text(encoding="utf-8"))
        priorities = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "ShioPlugin":
                for item in node.body:
                    if isinstance(item, ast.AsyncFunctionDef) and item.name in {"layout_text_components", "stage_remaining_bubbles"}:
                        priorities[item.name] = ast.unparse(item.decorator_list[0])
        self.assertIn("100000", priorities["layout_text_components"])
        self.assertIn("-100000", priorities["stage_remaining_bubbles"])

    async def test_fresh_t2i_projection_shape_matrix_keeps_or_stages_exactly(self):
        helper = R29.R29IndependentBubbleOracleTests()
        short = [MAIN.Plain("甲"), MAIN.Plain("乙")]
        raw_threshold = 1
        effective_threshold = await self._fixed_t2i_threshold(raw_threshold)
        self.assertEqual(50, effective_threshold)
        # The candidate must consume raw=1, not a hand-set 50.  The cases
        # below are derived from fixed initialize's effective 50: a regression
        # to the raw value makes the below/equal pending cases fail closed.
        cases = (
            (True, False, short, "fail_closed:official_t2i"),
            (False, True, short, "pending"),
            (False, True, [MAIN.Plain("a" * 24), MAIN.Plain("b" * 23)], "pending"),
            (None, True, short, "pending"),
            (None, True, [MAIN.Plain("a" * 23), MAIN.Plain("b" * 23)], "pending"),
            (None, True, [MAIN.Plain("a" * 24), MAIN.Plain("b" * 23)], "fail_closed:official_t2i"),
            (None, True, [R29._Image(), MAIN.Plain("a" * 80), MAIN.Plain("b")], "pending"),
        )
        for use_t2i, global_t2i, chain, expected in cases:
            with self.subTest(use_t2i=use_t2i, global_t2i=global_t2i, expected=expected):
                plugin, event = await helper._stage(
                    helper._config(t2i=global_t2i, t2i_word_threshold=raw_threshold),
                    chain, use_t2i,
                )
                self.assertEqual(expected, event.get_extra("shio.sys001.bubble_delivery_status"))
                self.assertEqual(expected == "pending", event.get_extra("shio.sys001.pending_bubble_delivery") is not None)

        for invalid in ("missing", 7):
            with self.subTest(invalid=invalid):
                plugin, event = helper._plugin(helper._config()), helper._event(short, False)
                if invalid == "missing":
                    del event.result.use_t2i_
                else:
                    event.result.use_t2i_ = invalid
                await plugin.stage_remaining_bubbles(event)
                self.assertEqual("fail_closed:official_result_shape_unknown", event.get_extra("shio.sys001.bubble_delivery_status"))
                self.assertIsNone(event.get_extra("shio.sys001.pending_bubble_delivery"))
                self.assertEqual(short, event.result.chain)

    async def test_fixed_post_hook_reply_prefix_projects_t2i_and_forward_boundaries(self):
        """R34 fails above-threshold cases: it stages before fixed prefix insertion."""
        cases = (
            ("t2i-prefix-below", [MAIN.Plain("a" * 22), MAIN.Plain("b" * 22)], True, 999, "recorded", True),
            ("t2i-prefix-equal", [MAIN.Plain("a" * 23), MAIN.Plain("b" * 22)], True, 999, "recorded", True),
            # The fixed stage sees 51 (not the hook's pre-prefix 50) and must render.
            ("t2i-prefix-above", [MAIN.Plain("a" * 23), MAIN.Plain("b" * 23)], True, 999, "recorded", False),
            ("forward-prefix-below", [MAIN.Plain("a" * 22), MAIN.Plain("b" * 22)], False, 46, "aiocqhttp", True),
            ("forward-prefix-equal", [MAIN.Plain("a" * 22), MAIN.Plain("b" * 23)], False, 46, "aiocqhttp", True),
            # The same actual post-hook prefix crosses aiocqhttp forward's strict boundary.
            ("forward-prefix-above", [MAIN.Plain("a" * 23), MAIN.Plain("b" * 23)], False, 46, "aiocqhttp", False),
        )
        for name, chain, t2i, threshold, platform, pending in cases:
            with self.subTest(name=name):
                hook_input = [part.text for part in chain]
                _plugin, event, rendered, hook_trace = await self._run_fixed_decorate_with_staging(
                    chain=chain, prefix="前", t2i=t2i, forward_threshold=threshold,
                    use_t2i=None, platform=platform,
                )
                self.assertEqual([hook_input], hook_trace)
                self.assertEqual(pending, event.get_extra("shio.sys001.pending_bubble_delivery") is not None)
                if name == "t2i-prefix-above":
                    self.assertEqual([51], [len(value) for value in rendered])
                if name == "forward-prefix-above":
                    self.assertEqual("Node", type(event.result.chain[0]).__name__)

        _plugin, event, rendered, _trace = await self._run_fixed_decorate_with_staging(
            chain=[MAIN.Plain("a" * 23), MAIN.Plain("b" * 23)], prefix="",
            t2i=True, forward_threshold=999, use_t2i=None,
        )
        self.assertIsNotNone(event.get_extra("shio.sys001.pending_bubble_delivery"))
        self.assertEqual([], rendered)

        _plugin, event, rendered, _trace = await self._run_fixed_decorate_with_staging(
            chain=[R29._Image(), MAIN.Plain("a" * 80), MAIN.Plain("b")], prefix="前",
            t2i=True, forward_threshold=999, use_t2i=None,
        )
        self.assertIsNotNone(event.get_extra("shio.sys001.pending_bubble_delivery"))
        self.assertEqual([], rendered)

        helper = R29.R29IndependentBubbleOracleTests()
        invalid = helper._config(t2i=True, t2i_word_threshold=1)
        invalid["platform_settings"]["reply_prefix"] = object()
        _plugin, event = await helper._stage(invalid, [MAIN.Plain("a"), MAIN.Plain("b")], None)
        self.assertIsNone(event.get_extra("shio.sys001.pending_bubble_delivery"))
        self.assertTrue(event.get_extra("shio.sys001.bubble_delivery_status").startswith("fail_closed:"))
