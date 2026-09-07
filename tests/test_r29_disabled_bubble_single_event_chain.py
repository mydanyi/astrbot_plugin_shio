"""R29 recorded one-event oracle: fixed decorator, respond, dispatcher, Shio and Meme."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import random
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace
import unittest

try:
    import test_r14_recorded_official_replay as R14
    import test_r15_meme_official_recorded_replay as R15
    import test_r28_disabled_bubble_official_chain as R28
    from test_name_semantic_provider_routing import MAIN
except ModuleNotFoundError:  # pragma: no cover - package discovery path
    from astrbot_plugin_shio.tests import test_r14_recorded_official_replay as R14
    from astrbot_plugin_shio.tests import test_r15_meme_official_recorded_replay as R15
    from astrbot_plugin_shio.tests import test_r28_disabled_bubble_official_chain as R28
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import MAIN


class _Log:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Event(R28._Event):
    def __init__(self, chain):
        super().__init__(chain, use_t2i=False)
        self.session_id = "r29-single-event"
        self.delivery_trace = []

    async def send(self, chain):
        self.sent.append(chain)
        self.delivery_trace.append(("text", chain))


class R29DisabledBubbleSingleEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.astr_sources = R14._read_archives()
        self.meme_sources = R15._archive_sources()
        self.official_root = Path(os.environ["SYS001_OFFICIAL_ASTRBOT_REPO"].split("::")[-1])
        dispatcher_blob = (
            self.official_root / "astrbot/core/pipeline/context_utils.py"
        ).read_bytes()
        self.dispatcher_source = dispatcher_blob.decode("utf-8")
        # The fixed v4.27.4 dispatcher is a text source.  Pin its canonical
        # LF representation so the same official body is accepted from the
        # Windows CRLF archive and the clean Linux Git checkout, but reject
        # any non-text newline form or different source logic.
        canonical_dispatcher = dispatcher_blob.replace(b"\r\n", b"\n")
        self.assertNotIn(b"\r", canonical_dispatcher)
        self.dispatcher_sha = hashlib.sha256(canonical_dispatcher).hexdigest()
        self.assertTrue((self.official_root / "astrbot/__init__.py").is_file())
        self.assertEqual(
            "9700cd79ea963411701936bf0ab1dc87436bd074671bfd261ad0b02793ec2b7c",
            self.dispatcher_sha,
        )

    def _plugin(self):
        plugin = R28.R28DisabledBubbleOfficialChainTests()._plugin({
            "provider_tts_settings": {"enable": False}, "t2i": False,
            "t2i_word_threshold": 50,
            "platform_settings": {"forward_threshold": 9999},
        })
        return plugin

    async def _decorate_then_respond(self, event, plugin, receiver, *, mode="success"):
        """Execute unchanged recorded fixed method bodies around one event object."""
        fixed = self.astr_sources["astrbot/core/pipeline/result_decorate/stage.py"]
        meme_seen = []

        async def meme_decorate(received):
            # The fixed 99999 Meme body must see the layout-100000 full chain,
            # before the fixed -100000 Shio stage takes its remainder.
            meme_seen.append([getattr(part, "text", None) for part in received.result.chain])
            await receiver._on_decorating_result_impl(received)

        handlers = [
            SimpleNamespace(handler=plugin.layout_text_components, handler_module_path="shio", handler_name="layout"),
            SimpleNamespace(handler=meme_decorate, handler_module_path="meme", handler_name="decorate"),
            SimpleNamespace(handler=plugin.stage_remaining_bubbles, handler_module_path="shio", handler_name="stage"),
        ]
        namespace = {
            "asyncio": asyncio, "re": __import__("re"), "traceback": traceback,
            "random": random, "time": __import__("time"), "Plain": MAIN.Plain,
            "Image": R28._Image, "At": object, "Reply": object, "Record": R28._Record,
            "Node": R28._Node, "Json": object, "ResultContentType": SimpleNamespace(STREAMING_RESULT="streaming", STREAMING_FINISH="finish"),
            "logger": _Log(), "star_map": {"shio": SimpleNamespace(name="Shio"), "meme": SimpleNamespace(name="Meme")},
            "star_handlers_registry": SimpleNamespace(get_handlers_by_event_type=lambda *_a, **_k: handlers),
            "EventType": SimpleNamespace(OnDecoratingResultEvent="decorating"),
            "SessionServiceManager": SimpleNamespace(should_process_tts_request=lambda _event: asyncio.sleep(0, result=False)),
            "html_renderer": SimpleNamespace(render_t2i=lambda *_a, **_k: asyncio.sleep(0, result="unused")),
            "file_token_service": SimpleNamespace(register_file=lambda *_a, **_k: asyncio.sleep(0, result="unused")),
        }
        process = R14._method(fixed, "ResultDecorateStage", "process", namespace)
        config = plugin.context.get_config(umo=event.unified_msg_origin)
        decorate_stage = SimpleNamespace(
            content_safe_check_reply=False, content_safe_check_stage=None, reply_prefix="",
            enable_segmented_reply=False, only_llm_result=True, words_count_threshold=0,
            split_mode="regex", regex="(?s).+", content_cleanup_rule="", _split_text_by_words=lambda text: [text],
            show_reasoning=False, tts_trigger_probability=0.0, t2i_word_threshold=50,
            t2i_use_network=False, t2i_active_template="recorded", forward_threshold=9999,
            reply_with_mention=False, reply_with_quote=False,
            ctx=SimpleNamespace(astrbot_config={**config, "callback_api_base": "", "t2i_use_file_service": False}, plugin_manager=SimpleNamespace(context=SimpleNamespace(get_using_tts_provider_async=lambda _umo: asyncio.sleep(0, result=None)))),
        )
        value = process(decorate_stage, event)
        if inspect.isasyncgen(value):
            async for _ in value:
                pass
        else:
            await value

        if mode == "cancel":
            async def cancelled(_lower, _upper):
                raise asyncio.CancelledError
            plugin._wait_between_bubbles = cancelled
        elif mode == "failure":
            async def failed(_chain):
                raise RuntimeError("recorded send failure")
            event.send = failed
        elif mode == "reload":
            plugin = self._plugin()

        async def shio_after(received):
            await plugin.send_remaining_bubbles(received)

        async def meme_after(received):
            await receiver._after_message_sent_impl(received)

        after_handlers = [
            SimpleNamespace(handler=shio_after, handler_module_path="shio", handler_name="after"),
            SimpleNamespace(handler=meme_after, handler_module_path="meme", handler_name="after"),
        ]
        dispatcher = R28._function(self.dispatcher_source, "call_event_hook", {
            "inspect": inspect, "traceback": traceback, "logger": _Log(),
            "star_handlers_registry": SimpleNamespace(get_handlers_by_event_type=lambda *_a, **_k: after_handlers),
            "star_map": {"shio": SimpleNamespace(name="Shio"), "meme": SimpleNamespace(name="Meme")},
            "AstrMessageEvent": object, "EventType": object,
        })
        respond_source = self.astr_sources["astrbot/core/pipeline/respond/stage.py"]
        component_type = SimpleNamespace(Plain="plain", Reply="reply", At="at", Record="record")
        MAIN.Plain.type = "plain"
        respond_namespace = {
            "asyncio": SimpleNamespace(sleep=lambda _value: asyncio.sleep(0)), "math": __import__("math"), "random": random, "logger": _Log(),
            "Comp": SimpleNamespace(Plain=MAIN.Plain, Reply=object, At=object, Record=object, File=object),
            "ComponentType": component_type, "ResultContentType": SimpleNamespace(STREAMING_RESULT="streaming", STREAMING_FINISH="finish"),
            "MessageChain": R15._MessageChain, "call_event_hook": dispatcher,
            "EventType": SimpleNamespace(OnAfterMessageSentEvent=SimpleNamespace(name="after")), "path_Mapping": lambda _mappings, path: path,
        }
        calculate = R14._method(respond_source, "RespondStage", "_calc_comp_interval", respond_namespace)
        respond = R14._method(respond_source, "RespondStage", "process", respond_namespace)
        async def empty(_chain): return False
        respond_stage = SimpleNamespace(
            config={}, platform_settings={}, interval_method="random", interval=(0, 0),
            _calc_comp_interval=lambda component: calculate(respond_stage, component),
            _is_empty_message_chain=empty, is_seg_reply_required=lambda _event: False,
            _extract_comp=lambda _chain, _types, modify_raw_chain: [],
        )
        try:
            await R14._drain(respond(respond_stage, event))
        except asyncio.CancelledError:
            # Fixed dispatcher honestly propagates cancellation; the event was
            # stopped before it reached the fixed Meme after hook.
            pass
        return meme_seen

    async def test_one_event_full_fixed_chain_handles_incident_success_cancel_failure_and_reload(self):
        """This is deliberately one object through every recorded fixed hook."""
        for mode in ("success", "cancel", "failure", "reload"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="sys001-r29-chain-") as root:
                pack_dir = Path(root) / "pack"
                image_dir = pack_dir / "memes" / "happy"
                image_dir.mkdir(parents=True)
                (image_dir / "recorded.png").write_bytes(b"recorded image")
                plugin, event = self._plugin(), _Event([
                    MAIN.Plain("甲。"), MAIN.Plain("乙。"), MAIN.Plain("丙。"), MAIN.Plain("丁。"), R28._Image(), R28._Record(),
                ])
                receiver = R15._official_receiver(
                    self.meme_sources, memes_dir=pack_dir / "memes", pack_dir=pack_dir, trace=event.delivery_trace
                )
                event.set_extra(MAIN.SYS001_TURN_EXTRA, MAIN.create_snapshot(R28.FakeEvent(), origin="direct"))
                event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, MAIN.TurnLifecycle())
                event.set_extra("shio.sys001.segmented_reply_status", R28.SYS001.SegmentedReplyCompatibility(False, "disabled"))
                response = SimpleNamespace(completion_text="甲乙丙丁 &&happy&&")
                await receiver._mark_llm_request_origin_impl(event)
                await receiver._resp_impl(event, response)
                self.assertEqual(["happy"], event.get_extra("found_emotions"))
                meme_seen = await self._decorate_then_respond(event, plugin, receiver, mode=mode)
                self.assertEqual(3, len([part for part in meme_seen[0] if part is not None]))
                self.assertEqual("甲。乙。丙。丁。", "".join(part for part in meme_seen[0] if part is not None))
                self.assertIsNone(event.get_extra("shio.sys001.pending_bubble_delivery"))
                if mode == "success":
                    self.assertFalse(event.is_stopped())
                    # RespondStage sends the staged first unit, then Shio's
                    # public after hook preserves two text, Image and Record.
                    self.assertEqual(5, len(event.sent))
                    self.assertIsInstance(event.sent[-2].chain[0], R28._Image)
                    self.assertIsInstance(event.sent[-1].chain[0], R28._Record)
                    self.assertEqual(["text"] * 5 + ["meme_image"], [kind for kind, _value in event.delivery_trace])
                    self.assertIsNone(event.get_extra("meme_manager_pending_images"))
                else:
                    self.assertTrue(event.is_stopped())
                    self.assertNotIn("meme_image", [kind for kind, _value in event.delivery_trace])
                    self.assertIn(event.get_extra("shio.sys001.bubble_delivery_status"), {"cancelled", "send_failed", "stale"})
