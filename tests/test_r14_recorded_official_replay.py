"""R14 limited fixed-source hook replay; not production/network acceptance.

The methods executed here are parsed directly from the two frozen upstream
archives.  Shims are deliberately restricted to decorator registration,
logging, event/message records and sleep/send/image I/O recording.

Historical limitation: its Meme wrapper test injects a selected ID before
decorating.  It therefore is not evidence that fixed Meme selection was driven
by Shio's reviewed final text; R15's dedicated replay supplies that proof.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import math
from pathlib import Path
import random
import re
from types import SimpleNamespace
import unittest
import zipfile

try:
    from test_name_semantic_provider_routing import (
        MAIN,
        SYS001,
        FakeEvent,
        _official_registry_order,
    )
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        MAIN,
        SYS001,
        FakeEvent,
        _official_registry_order,
    )


ASTR_ARCHIVE = Path(r"C:\Users\45928\AppData\Local\Temp\shio-r14-official-c12e8d08d495465e8c10d21f96f80b76\astrbot-fixed.zip")
MEME_ARCHIVE = Path(r"C:\Temp\sys001-r14-meme-official-8ecd36e03fb545b5bcb7024f4ae34b23\meme-fixed.zip")
ASTR_ARCHIVE_SHA = "c284bdc18cd8c862631125db1ec052921f94350b57f34fb3a746b43fd45c4149"
MEME_ARCHIVE_SHA = "7ce485ba7aadf5b516c60b86c64457384b71c67b21c4905b37c01bd8568bd2f4"
SOURCES = {
    "astrbot/core/pipeline/result_decorate/stage.py": "c7d09174f080be1fd4c6b1698ad99aa559eb0dcbf3e09c71c5bc33546c257a1c",
    "astrbot/core/pipeline/respond/stage.py": "b9abc2097538cfcb7fa87e0ac844541c9b3c64bc6ce36fa36d5beab2d8d2fe2b",
    "main.py": "70c22358b05ea4e90cdb43060536fd8d401dc5a838299a3e60771ab52b117373",
    "mixins/event_handlers.py": "ea552e7dd471faa2e1d538bbd08e6470bb01c8c93e6273ae7f96b739775c74a7",
    "metadata.yaml": "2b4658fe65e7e666830dd214ed7755918a135ba7c758977ade2206afc0abff52",
}


class Plain:
    type = "plain"

    def __init__(self, text: str):
        self.text = text


class Image:
    type = "image"

    @classmethod
    def fromFileSystem(cls, _path):
        return cls()


class At:
    type = "at"


class Reply:
    type = "reply"


class Record:
    type = "record"


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Chain:
    def __init__(self, chain):
        self.chain = list(chain)


class _Result:
    def __init__(self, chain):
        self.chain = list(chain)
        self.result_content_type = "final"
        self.use_t2i_ = False

    def is_llm_result(self):
        return True

    def is_model_result(self):
        return True

    def get_plain_text(self):
        return "".join(str(item.text) for item in self.chain if hasattr(item, "text"))

    def derive(self, chain):
        return _Chain(chain)


class _ReplayEvent:
    def __init__(self, result):
        self.result = result
        self.extras = {}
        self.plugins_name = []
        self.unified_msg_origin = "recorded:group:1"
        self.sent = []

    def get_result(self):
        return self.result

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def get_platform_name(self):
        return "recorded"

    def get_platform_id(self):
        return "recorded"

    def get_sender_name(self):
        return "recorded"

    def get_sender_id(self):
        return "recorded"

    def get_message_type(self):
        return "group"

    def get_self_id(self):
        return "bot"

    def _outline_chain(self, _chain):
        return "recorded"

    def is_stopped(self):
        return False

    async def send(self, chain):
        self.sent.append(chain)

    def clear_result(self):
        self.extras["cleared"] = True


def _read_archives():
    assert hashlib.sha256(ASTR_ARCHIVE.read_bytes()).hexdigest() == ASTR_ARCHIVE_SHA
    assert hashlib.sha256(MEME_ARCHIVE.read_bytes()).hexdigest() == MEME_ARCHIVE_SHA
    sources = {}
    with zipfile.ZipFile(ASTR_ARCHIVE) as archive:
        for path in SOURCES:
            if path.startswith("astrbot/"):
                sources[path] = archive.read(path)
    with zipfile.ZipFile(MEME_ARCHIVE) as archive:
        for path in SOURCES:
            if not path.startswith("astrbot/"):
                sources[path] = archive.read(path)
    for path, expected in SOURCES.items():
        assert hashlib.sha256(sources[path]).hexdigest() == expected, path
    return {path: value.decode("utf-8") for path, value in sources.items()}


def _method(source, class_name, method_name, namespace):
    tree = ast.parse(source)
    for item in tree.body:
        if isinstance(item, ast.ClassDef) and item.name == class_name:
            for member in item.body:
                if (
                    isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name == method_name
                ):
                    member.decorator_list = []  # decorator registration shim only
                    ast.fix_missing_locations(member)
                    exec(
                        compile(ast.Module([member], []), "<fixed-recorded-source>", "exec"),
                        namespace,
                    )
                    return namespace[method_name]
    raise LookupError(f"missing {class_name}.{method_name}")


async def _drain(value):
    if value is not None:
        if inspect.isasyncgen(value):
            async for _ in value:
                pass
        else:
            await value


class RecordedOfficialReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sources = _read_archives()

    def test_fixed_sources_and_real_hook_priorities(self):
        """Request projection is last; final review/layout precede Meme hooks."""
        handlers = _official_registry_order(
            "attach_turn_and_project_capabilities",
            "observe_final_agent_response",
            "layout_text_components",
        )
        self.assertEqual(
            [
                "astrbot_plugin_shio.main_observe_final_agent_response",
                "astrbot_plugin_shio.main_layout_text_components",
                "astrbot_plugin_shio.main_attach_turn_and_project_capabilities",
            ],
            [handler.handler_full_name for handler in handlers],
        )
        self.assertEqual(
            [100000, 100000, -1000000],
            [handler.extras_configs["priority"] for handler in handlers],
        )
        tree = ast.parse(self.sources["main.py"])
        decorators = []
        for item in tree.body:
            if isinstance(item, ast.ClassDef) and item.name == "MemeSender":
                for member in item.body:
                    if isinstance(member, ast.AsyncFunctionDef) and member.name in {
                        "inject_meme_prompt", "resp", "on_decorating_result"
                    }:
                        decorators.extend(ast.unparse(value) for value in member.decorator_list)
        self.assertEqual(3, len(decorators))
        self.assertTrue(all("99999" in value for value in decorators))
        self.assertGreater(99999, handlers[-1].extras_configs["priority"])
        self.assertGreater(handlers[0].extras_configs["priority"], 99999)
        self.assertGreater(handlers[1].extras_configs["priority"], 99999)

    async def test_meme_limited_wrapper_replay_is_not_selection_evidence(self):
        """Historical wrapper smoke test; R15 owns selection-path evidence."""
        source = self.sources["mixins/event_handlers.py"]
        namespace = {
            "asyncio": asyncio,
            "re": re,
            "json": __import__("json"),
            "traceback": __import__("traceback"),
            "os": __import__("os"),
            "random": random,
            "Plain": MAIN.Plain,
            "Image": Image,
            "MessageChain": _Chain,
            "ResultContentType": SimpleNamespace(STREAMING_FINISH="finish"),
            "logger": _Log(),
            "strip_internal_image_ref_lines": lambda text: text.replace("[internal-image]\n", ""),
            "runtime_category_mapping": lambda value: value,
            "probability_hit": lambda _value: False,
            "validate_selected_id": lambda _event, _selected, _pack: Path("recorded-image"),
            "REVIEW_CATEGORY": "review",
            "IMAGE_EXTENSIONS": (),
        }
        inject = _method(source, "EventHandlerMixin", "_inject_meme_prompt_impl", namespace)
        respond = _method(source, "EventHandlerMixin", "_resp_impl", namespace)
        decorate = _method(source, "EventHandlerMixin", "_on_decorating_result_impl", namespace)
        after = _method(source, "EventHandlerMixin", "_after_message_sent_impl", namespace)
        trace = []

        class Receiver:
            emotion_llm_enabled = False
            emotion_llm_provider_id = ""
            category_mapping = {}
            emotions_probability = 0
            max_emotions_per_message = 1
            remove_invalid_alternative_markup = True
            streaming_compatibility = False
            enable_mixed_message = False
            send_image_as_base64 = False

            def _scope_allows_llm_origin(self, _event): return True
            def _apply_request_prompt(self, req, _event): trace.append(("request", req.system_prompt))
            def _semantic_mode_active(self, _event): return True
            def _resolve_runtime_pack_context(self, **_kwargs): return {"category_mapping": {}}
            def _read_config_value(self, *_args, **_kwargs): return False
            def _filter_emotion_selection(self, values): return values
            def _should_attach_for_result(self, _event, _result): return True
            def _clean_outgoing_plain_text(self, text): return text
            def _get_runtime_memes_dir_for_event(self, _event): return Path("recorded")
            def _get_runtime_pack_context(self, **_kwargs): return {"pack_dir": Path("recorded")}
            def _convert_to_gif(self, path): return path
            async def _send_meme_image(self, _event, image): trace.append(("image", image))

        receiver = Receiver()
        event = _ReplayEvent(_Result([MAIN.Plain("reviewed text"), Image()]))
        req = SimpleNamespace(system_prompt="Shio reviewed prompt", model="")
        await inject(receiver, event, req)
        shio = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        shio.config = {"sys001": {"final_review": {"mode": "off"}}}
        event.set_extra(MAIN.SYS001_TURN_EXTRA, MAIN.create_snapshot(FakeEvent(), origin="direct"))
        event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, MAIN.TurnLifecycle())
        event.set_extra("r14.initial_candidate", "initial candidate")
        response = SimpleNamespace(
            role="assistant",
            completion_text="[internal-image]\nreviewed text &&unknown&&",
            reasoning_content="",
        )
        await shio.observe_final_agent_response(event, response)
        self.assertEqual("[internal-image]\nreviewed text &&unknown&&", response.completion_text)
        await respond(receiver, event, response)
        self.assertEqual("reviewed text", response.completion_text)
        self.assertEqual("Shio reviewed prompt", trace[0][1])
        event.set_extra("meme_manager_semantic_selected_ids", ["recorded"])
        await decorate(receiver, event)
        self.assertEqual(["reviewed text"], [part.text for part in event.result.chain if isinstance(part, MAIN.Plain)])
        self.assertIsInstance(event.result.chain[1], Image)
        image = event.get_extra("meme_manager_pending_images")[0]
        self.assertEqual([image], event.get_extra("meme_manager_pending_images"))
        await after(receiver, event)
        self.assertEqual([("image", image)], trace[1:])
        self.assertIsNone(event.get_extra("meme_manager_pending_images"))

    async def test_fixed_result_decorate_and_respond_process_own_segmentation_and_sends(self):
        """Run original AstrBot methods; sleep/send traces belong to their bodies."""
        decorate_source = self.sources["astrbot/core/pipeline/result_decorate/stage.py"]
        respond_source = self.sources["astrbot/core/pipeline/respond/stage.py"]
        component_type = SimpleNamespace(Plain="plain", Reply="reply", At="at", Record="record")
        content_type = SimpleNamespace(STREAMING_RESULT="streaming", STREAMING_FINISH="finish")
        decorate_namespace = {
            "asyncio": asyncio, "re": re, "traceback": __import__("traceback"),
            "Plain": MAIN.Plain, "Image": Image, "At": At, "Reply": Reply, "Record": Record,
            "Node": object, "Json": object, "ResultContentType": content_type,
            "logger": _Log(), "star_handlers_registry": SimpleNamespace(get_handlers_by_event_type=lambda *args, **kwargs: []),
            "EventType": SimpleNamespace(OnDecoratingResultEvent="decorating"), "star_map": {},
            "SessionServiceManager": SimpleNamespace(should_process_tts_request=lambda _event: asyncio.sleep(0, result=False)),
        }
        decorate_process = _method(decorate_source, "ResultDecorateStage", "process", decorate_namespace)
        MAIN.Plain.type = "plain"
        result = _Result([MAIN.Plain("alpha"), MAIN.Plain("beta")])
        event = _ReplayEvent(result)
        layout = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        layout.config = {"sys001": {"presentation": {"text_component_mode": "plugin", "text_component_min_segments": 1, "text_component_max_segments": 3}}}
        event.set_extra(MAIN.SYS001_TURN_EXTRA, MAIN.create_snapshot(FakeEvent(), origin="direct"))
        await layout.layout_text_components(event)
        decorate_stage = SimpleNamespace(
            content_safe_check_reply=False, content_safe_check_stage=None,
            reply_prefix="", enable_segmented_reply=True, only_llm_result=True,
            words_count_threshold=0, split_mode="regex", regex="(?s).+", content_cleanup_rule="",
            _split_text_by_words=lambda text: [text], show_reasoning=False,
            tts_trigger_probability=0.0, use_t2i_=False, forward_threshold=9999,
            reply_with_mention=False, reply_with_quote=False, t2i_word_threshold=9999,
            ctx=SimpleNamespace(
                astrbot_config={"provider_tts_settings": {"enable": False}, "t2i": False},
                plugin_manager=SimpleNamespace(
                    context=SimpleNamespace(
                        get_using_tts_provider_async=lambda _umo: asyncio.sleep(0, result=None)
                    )
                ),
            ),
        )
        await _drain(decorate_process(decorate_stage, event))
        self.assertEqual(["alpha", "beta"], [part.text for part in result.chain])

        sleeps = []
        class AsyncioIO:
            async def sleep(self, value): sleeps.append(value)
        respond_namespace = {
            "asyncio": AsyncioIO(), "math": math, "random": random, "logger": _Log(),
            "Comp": SimpleNamespace(Plain=MAIN.Plain, Reply=Reply, At=At, Record=Record, File=object),
            "ComponentType": component_type, "ResultContentType": content_type,
            "MessageChain": _Chain, "call_event_hook": lambda _event, _kind: asyncio.sleep(0, result=False),
            "EventType": SimpleNamespace(OnAfterMessageSentEvent="after"), "path_Mapping": lambda _mappings, path: path,
        }
        calculate = _method(respond_source, "RespondStage", "_calc_comp_interval", respond_namespace)
        process = _method(respond_source, "RespondStage", "process", respond_namespace)
        async def empty(_chain): return False
        segmented = SimpleNamespace(
            config={}, platform_settings={}, interval_method="random", interval=(0, 0),
            _calc_comp_interval=lambda comp: calculate(segmented, comp),
            _is_empty_message_chain=empty, is_seg_reply_required=lambda _event: True,
            _extract_comp=lambda chain, _types, modify_raw_chain: [],
        )
        await _drain(process(segmented, event))
        self.assertEqual(2, len(event.sent))
        self.assertEqual(2, len(sleeps))
        self.assertEqual(["alpha", "beta"], [chain.chain[0].text for chain in event.sent])

        disabled_event = _ReplayEvent(_Result([MAIN.Plain("alpha"), MAIN.Plain("beta")]))
        disabled = SimpleNamespace(**segmented.__dict__)
        disabled.is_seg_reply_required = lambda _event: False
        disabled._extract_comp = lambda chain, _types, modify_raw_chain: []
        await _drain(process(disabled, disabled_event))
        self.assertEqual(1, len(disabled_event.sent))
        self.assertEqual(["alpha", "beta"], [part.text for part in disabled_event.sent[0].chain])
