"""R28 recorded regression: disabled Shio bubbles remain inside official safety gates."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import os
import random
import time
from pathlib import Path
from types import SimpleNamespace
import traceback
import unittest
import zipfile

try:
    from test_name_semantic_provider_routing import FakeEvent, MAIN, SYS001
    import test_r14_recorded_official_replay as R14
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import FakeEvent, MAIN, SYS001
    from astrbot_plugin_shio.tests import test_r14_recorded_official_replay as R14


class _Log:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Image:
    type = "image"

    @classmethod
    def fromFileSystem(cls, path):
        value = cls()
        value.path = path
        return value


class _Record:
    type = "record"

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Node:
    type = "node"

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Chain:
    def __init__(self, chain):
        self.chain = list(chain)


class _Result:
    def __init__(self, chain, *, use_t2i=False):
        self.chain = list(chain)
        self.result_content_type = "final"
        self.use_t2i_ = use_t2i

    def is_llm_result(self): return True
    def is_model_result(self): return True
    def get_plain_text(self): return "".join(part.text for part in self.chain if hasattr(part, "text"))
    def derive(self, chain): return _Chain(chain)


class _Event(FakeEvent):
    def __init__(self, chain, *, platform="recorded", use_t2i=False):
        super().__init__()
        self._extras, self.sent = {}, []
        self.result = _Result(chain, use_t2i=use_t2i)
        self.plugins_name, self.unified_msg_origin = [], "recorded:group:28"
        self._platform, self._stopped = platform, False

    def get_extra(self, key, default=None): return self._extras.get(key, default)
    def set_extra(self, key, value): self._extras[key] = value
    def get_result(self): return self.result
    def get_platform_name(self): return self._platform
    def get_platform_id(self): return self._platform
    def get_sender_name(self): return "recorded"
    def get_sender_id(self): return "recorded"
    def get_message_type(self): return "group"
    def get_self_id(self): return "bot"
    def _outline_chain(self, _chain): return "recorded"
    def stop_event(self): self._stopped = True
    def is_stopped(self): return self._stopped
    async def send(self, chain): self.sent.append(chain)
    def clear_result(self): self._extras["cleared"] = True


def _function(source, name, namespace):
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            node.decorator_list = []
            ast.fix_missing_locations(node)
            exec(compile(ast.Module([node], []), "<fixed-recorded-source>", "exec"), namespace)
            return namespace[name]
    raise LookupError(name)


class R28DisabledBubbleOfficialChainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sources = R14._read_archives()

    def _plugin(self, config):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {"presentation": {
            "text_component_mode": "plugin", "text_component_min_segments": 1,
            "text_component_max_segments": 3, "bubble_send_min_wait_seconds": 0,
            "bubble_send_max_wait_seconds": 0,
        }}}
        plugin.context = SimpleNamespace(get_config=lambda **_kwargs: config)
        plugin._bubble_epoch, plugin._bubble_terminated = 0, False
        return plugin

    def _event(self, chain, **kwargs):
        event = _Event(chain, **kwargs)
        event.set_extra(MAIN.SYS001_TURN_EXTRA, MAIN.create_snapshot(FakeEvent(), origin="direct"))
        event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, MAIN.TurnLifecycle())
        event.set_extra("shio.sys001.segmented_reply_status", SYS001.SegmentedReplyCompatibility(False, "disabled"))
        return event

    async def test_fixed_post_decorator_shapes_fail_closed_but_plain_disabled_path_stages(self):
        """R27 fails: it removes units before all three fixed post-hook owners run."""
        fixed = self.sources["astrbot/core/pipeline/result_decorate/stage.py"]
        self.assertLess(fixed.index("OnDecoratingResultEvent"), fixed.index("# TTS"))
        self.assertLess(fixed.index("# TTS"), fixed.index("# 文本转图片"))
        self.assertLess(fixed.index("# 文本转图片"), fixed.index("# 触发转发消息"))
        base = {"provider_tts_settings": {"enable": False, "use_file_service": False, "dual_output": False}, "t2i": False, "t2i_word_threshold": 50, "platform_settings": {"forward_threshold": 9999}}
        cases = (
            ("official_tts", {**base, "provider_tts_settings": {"enable": True, "use_file_service": False, "dual_output": False}}, "recorded", False),
            ("official_t2i", {**base, "t2i": True}, "recorded", None),
            ("official_forward", {**base, "platform_settings": {"forward_threshold": 3}}, "aiocqhttp", False),
        )
        # The recorded 4-text incident has already passed Shio's maximum=3
        # layout owner: the first two source texts are one merged Plain here.
        chain = [MAIN.Plain("甲" * 48), MAIN.Plain("丙"), MAIN.Plain("丁"), _Image()]
        self.assertEqual("甲" * 48 + "丙丁", "".join(item.text for item in chain if hasattr(item, "text")))

        async def run_fixed_decorate(plugin, event, config):
            handlers = [SimpleNamespace(handler=plugin.stage_remaining_bubbles, handler_module_path="shio", handler_name="stage")]
            namespace = {
                "asyncio": asyncio, "re": __import__("re"), "traceback": traceback,
                "random": random, "time": time,
                "Plain": MAIN.Plain, "Image": _Image, "At": object, "Reply": object,
                "Record": _Record, "Node": _Node, "Json": object,
                "ResultContentType": SimpleNamespace(STREAMING_RESULT="streaming", STREAMING_FINISH="finish"),
                "logger": _Log(), "star_map": {"shio": SimpleNamespace(name="Shio")},
                "star_handlers_registry": SimpleNamespace(get_handlers_by_event_type=lambda *_a, **_k: handlers),
                "EventType": SimpleNamespace(OnDecoratingResultEvent="decorating"),
                "SessionServiceManager": SimpleNamespace(should_process_tts_request=lambda _event: asyncio.sleep(0, result=True)),
                "html_renderer": SimpleNamespace(render_t2i=lambda *_a, **_k: asyncio.sleep(0, result="recorded.png")),
                "file_token_service": SimpleNamespace(register_file=lambda *_a, **_k: asyncio.sleep(0, result="unused")),
            }
            process = R14._method(fixed, "ResultDecorateStage", "process", namespace)
            stage = SimpleNamespace(
                content_safe_check_reply=False, content_safe_check_stage=None, reply_prefix="",
                enable_segmented_reply=False, only_llm_result=True, words_count_threshold=0,
                split_mode="regex", regex="(?s).+", content_cleanup_rule="", _split_text_by_words=lambda text: [text],
                show_reasoning=False, tts_trigger_probability=1.0, t2i_word_threshold=max(int(config["t2i_word_threshold"]), 50),
                t2i_use_network=False, t2i_active_template="recorded", forward_threshold=config["platform_settings"]["forward_threshold"],
                reply_with_mention=False, reply_with_quote=False,
                ctx=SimpleNamespace(
                    astrbot_config={**config, "callback_api_base": "", "t2i_use_file_service": False},
                    plugin_manager=SimpleNamespace(
                        context=SimpleNamespace(
                            get_using_tts_provider_async=lambda _umo: asyncio.sleep(
                                0,
                                result=SimpleNamespace(
                                    get_audio=lambda _text: asyncio.sleep(0, result="recorded.mp3")
                                ),
                            )
                        )
                    ),
                ),
            )
            value = process(stage, event)
            if inspect.isasyncgen(value):
                async for _ in value:
                    pass
            else:
                await value

        for expected, config, platform, use_t2i in cases:
            with self.subTest(expected=expected):
                plugin, event = self._plugin(config), self._event(chain, platform=platform, use_t2i=use_t2i)
                await run_fixed_decorate(plugin, event, config)
                self.assertIsNone(event.get_extra("shio.sys001.pending_bubble_delivery"))
                self.assertEqual(f"fail_closed:{expected}", event.get_extra("shio.sys001.bubble_delivery_status"))
                if expected == "official_tts":
                    self.assertEqual(["甲" * 48, "丙", "丁"], [record.text for record in event.result.chain[:3]])
                elif expected == "official_t2i":
                    self.assertIsInstance(event.result.chain[0], _Image)
                else:
                    self.assertEqual("甲" * 48 + "丙丁", "".join(item.text for item in event.result.chain[0].content if hasattr(item, "text")))
        plugin, event = self._plugin(base), self._event(chain)
        await plugin.stage_remaining_bubbles(event)
        self.assertEqual(["甲" * 48], [part.text for part in event.result.chain])
        self.assertIsNotNone(event.get_extra("shio.sys001.pending_bubble_delivery"))

    async def test_fixed_dispatcher_stops_fixed_meme_after_on_success_cancel_and_send_failure(self):
        """Run fixed call_event_hook and fixed Meme after body on one disabled event."""
        official_root = Path(os.environ["SYS001_OFFICIAL_ASTRBOT_REPO"].split("::")[-1])
        dispatcher_path = official_root / "astrbot/core/pipeline/context_utils.py"
        dispatcher_source = dispatcher_path.read_text(encoding="utf-8")
        self.assertTrue((official_root / "astrbot/__init__.py").is_file())
        self.assertEqual(R14.ASTR_ARCHIVE_SHA, hashlib.sha256(R14.ASTR_ARCHIVE.read_bytes()).hexdigest())
        meme_after = R14._method(self.sources["mixins/event_handlers.py"], "EventHandlerMixin", "_after_message_sent_impl", {"logger": _Log(), "os": __import__("os")})
        trace = []
        class MemeReceiver:
            async def _send_meme_image(self, _event, image): trace.append(("meme", image))

        async def run_case(mode):
            event = self._event([MAIN.Plain("甲"), MAIN.Plain("乙"), MAIN.Plain("丙"), _Record()])
            plugin = self._plugin({"provider_tts_settings": {"enable": False}, "t2i": False, "t2i_word_threshold": 50, "platform_settings": {"forward_threshold": 9999}})
            await plugin.stage_remaining_bubbles(event)
            event.set_extra("meme_manager_pending_images", ["recorded-image"])
            if mode == "cancel":
                async def cancelled(_value): raise asyncio.CancelledError
                plugin._bubble_sleep = cancelled
            elif mode == "failure":
                async def failed(_chain): raise RuntimeError("recorded send failure")
                event.send = failed
            async def shio_after(received): await plugin.send_remaining_bubbles(received)
            async def meme_after_handler(received): await meme_after(MemeReceiver(), received)
            handlers = [SimpleNamespace(handler=shio_after, handler_module_path="shio", handler_name="after"), SimpleNamespace(handler=meme_after_handler, handler_module_path="meme", handler_name="after")]
            registry = SimpleNamespace(get_handlers_by_event_type=lambda *_args, **_kwargs: handlers)
            dispatcher = _function(dispatcher_source, "call_event_hook", {"inspect": inspect, "traceback": traceback, "logger": _Log(), "star_handlers_registry": registry, "star_map": {"shio": SimpleNamespace(name="Shio"), "meme": SimpleNamespace(name="Meme")}, "AstrMessageEvent": object, "EventType": object})
            return event, await dispatcher(event, SimpleNamespace(name="OnAfterMessageSentEvent"))

        event, stopped = await run_case("success")
        self.assertFalse(stopped)
        self.assertEqual(["乙", "丙"], [item.chain[0].text for item in event.sent[:2]])
        self.assertEqual(["meme"], [item[0] for item in trace])
        trace.clear()
        for mode in ("cancel", "failure"):
            with self.subTest(mode=mode):
                event, stopped = await run_case(mode)
                self.assertTrue(stopped)
                self.assertTrue(event.is_stopped())
                self.assertEqual([], trace)
                self.assertEqual("cancelled" if mode == "cancel" else "send_failed", event.get_extra("shio.sys001.bubble_delivery_status"))

    def test_user_docs_no_longer_state_the_replaced_d081_owner_rule(self):
        root = Path(__file__).resolve().parents[1]
        for path, replaced in ((root / "README.md", "D-081 已确定组件布局边界"), (root / "docs" / "COMPATIBILITY.md", "D-081 把多消息／间隔")):
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(replaced, text)
                self.assertIn("关闭 AstrBot 内置分段", text)
