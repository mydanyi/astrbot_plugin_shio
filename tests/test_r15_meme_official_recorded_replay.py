"""R15 fixed-source Meme Manager 4.15.4 recorded replay.

This test executes the original archived hook/helper method bodies.  Its only
shims are import/decorator plumbing, logger records, temporary image-path I/O,
and recorded message delivery; selection, filtering, text cleaning, attach,
pending, and after-send behavior remain the fixed official methods.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
import random
import re
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import unquote, urlsplit
import unittest
import zipfile

try:
    from test_name_semantic_provider_routing import (
        FakeContext,
        FakeEvent,
        MAIN,
        _official_registry_order,
    )
    import test_r14_reviewer_regressions as R14
    import test_r14_recorded_official_replay as R14Replay
except ModuleNotFoundError:  # pragma: no cover - package discovery path
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        FakeContext,
        FakeEvent,
        MAIN,
        _official_registry_order,
    )
    from astrbot_plugin_shio.tests import test_r14_reviewer_regressions as R14
    from astrbot_plugin_shio.tests import test_r14_recorded_official_replay as R14Replay


MEME_ARCHIVE = Path(
    r"C:\Temp\sys001-r14-meme-official-8ecd36e03fb545b5bcb7024f4ae34b23\meme-fixed.zip"
)
MEME_ARCHIVE_SHA = "7ce485ba7aadf5b516c60b86c64457384b71c67b21c4905b37c01bd8568bd2f4"
MEME_ENTRY_SHA = {
    "main.py": "70c22358b05ea4e90cdb43060536fd8d401dc5a838299a3e60771ab52b117373",
    "mixins/event_handlers.py": "ea552e7dd471faa2e1d538bbd08e6470bb01c8c93e6273ae7f96b739775c74a7",
    "metadata.yaml": "2b4658fe65e7e666830dd214ed7755918a135ba7c758977ade2206afc0abff52",
}


class _Log:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Image:
    type = "image"

    def __init__(self, path=""):
        self.file = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(str(path))


class _MessageChain:
    def __init__(self, chain):
        self.chain = list(chain)


class _Result:
    def __init__(self, chain):
        self.chain = list(chain)
        self.result_content_type = "final"

    def is_llm_result(self):
        return True

    def get_plain_text(self):
        return "".join(part.text for part in self.chain if hasattr(part, "text"))

    def derive(self, chain):
        return _MessageChain(chain)


class _ReplayEvent:
    def __init__(self):
        self.extras: dict[str, object] = {}
        # The Shio review fixture reuses its recorded public Context route.
        self.unified_msg_origin = "qq:group:202"
        self.session_id = "recorded-session"
        self.result = None
        self.delivery_trace: list[tuple[str, object]] = []
        self.plugins_name = []

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_result(self):
        return self.result

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
        self.delivery_trace.append(("official_text", chain))

    def clear_result(self):
        self.extras["recorded_result_cleared"] = True


class _ToolSet:
    def __init__(self):
        self.removed: list[str] = []

    def remove_tool(self, name):
        self.removed.append(name)


class _Request:
    def __init__(self):
        self.system_prompt = "Persona prefix"
        self.model = "recorded-model"
        self.session_id = "recorded-session"
        self.conversation = None
        self.func_tool = _ToolSet()


class _ReviewProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def text_chat(self, *, prompt, func_tool):
        self.calls.append((prompt, func_tool))
        return SimpleNamespace(completion_text=self.replies.pop(0))


def _archive_sources() -> dict[str, str]:
    assert hashlib.sha256(MEME_ARCHIVE.read_bytes()).hexdigest() == MEME_ARCHIVE_SHA
    with zipfile.ZipFile(MEME_ARCHIVE) as archive:
        source = {
            path: archive.read(path).decode("utf-8") for path in MEME_ENTRY_SHA
        }
        source.update(
            {
                "backend/text_safety.py": archive.read(
                    "backend/text_safety.py"
                ).decode("utf-8"),
                "backend/semantic_models.py": archive.read(
                    "backend/semantic_models.py"
                ).decode("utf-8"),
                "backend/category_manager.py": archive.read(
                    "backend/category_manager.py"
                ).decode("utf-8"),
                "backend/models.py": archive.read("backend/models.py").decode("utf-8"),
                "utils.py": archive.read("utils.py").decode("utf-8"),
            }
        )
    for path, expected in MEME_ENTRY_SHA.items():
        assert hashlib.sha256(source[path].encode("utf-8")).hexdigest() == expected
    return source


def _compile_selected(source, names, namespace):
    """Compile unchanged archived top-level definitions in source order."""
    tree = ast.parse(source)
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            nodes.append(copy.deepcopy(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in names for target in targets):
                nodes.append(copy.deepcopy(node))
    ast.fix_missing_locations(module := ast.Module(nodes, []))
    exec(compile(module, "<meme-fixed-helper>", "exec"), namespace)


def _compile_handler_class(source, namespace):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "EventHandlerMixin":
            compiled = copy.deepcopy(node)
            for member in compiled.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    member.decorator_list = []
            ast.fix_missing_locations(module := ast.Module([compiled], []))
            exec(compile(module, "<meme-fixed-handlers>", "exec"), namespace)
            return namespace["EventHandlerMixin"]
    raise LookupError("fixed EventHandlerMixin missing")


def _compile_meme_sender_method(source, name, namespace):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MemeSender":
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == name:
                    compiled = copy.deepcopy(member)
                    compiled.decorator_list = []
                    ast.fix_missing_locations(module := ast.Module([compiled], []))
                    exec(compile(module, "<meme-fixed-main>", "exec"), namespace)
                    return namespace[name]
    raise LookupError(f"fixed MemeSender.{name} missing")


def _install_replay_import_shim(memes_dir: Path, pack_dir: Path):
    """Only supports archived relative import discovery for the temp pack path."""
    package = ModuleType("meme_fixed_replay")
    package.__path__ = []
    backend = ModuleType("meme_fixed_replay.backend")
    backend.__path__ = []
    resolver = ModuleType("meme_fixed_replay.backend.pack_resolver")

    def resolve_pack_context(**_kwargs):
        return {
            "pack_id": "recorded-pack",
            "pack_dir": pack_dir,
            "memes_dir": memes_dir,
            "metadata_path": pack_dir / "metadata.json",
            "category_mapping": {"happy": "recorded happy"},
        }

    resolver.resolve_pack_context = resolve_pack_context
    sys.modules.update(
        {
            package.__name__: package,
            backend.__name__: backend,
            resolver.__name__: resolver,
        }
    )


def _official_receiver(sources, *, memes_dir: Path, pack_dir: Path, trace):
    """Create a receiver whose relevant methods are exact archived bodies."""
    _install_replay_import_shim(memes_dir, pack_dir)
    namespace = {
        "__name__": "meme_fixed_replay.main",
        "__package__": "meme_fixed_replay",
        "Any": object,
        "asyncio": asyncio,
        "json": json,
        "os": __import__("os"),
        "random": random,
        "re": re,
        "traceback": __import__("traceback"),
        "Path": Path,
        "unquote": unquote,
        "urlsplit": urlsplit,
        "Plain": MAIN.Plain,
        "Image": _Image,
        "MessageChain": _MessageChain,
        "ResultContentType": SimpleNamespace(STREAMING_FINISH="streaming_finish"),
        "AstrMessageEvent": object,
        "ProviderRequest": object,
        "LLMResponse": object,
        "logger": _Log(),
        "REVIEW_CATEGORY": "needs_review",
        "PLUGIN_DATA_DIR": pack_dir,
        "LLM_REQUEST_ORIGIN_EXTRA_KEY": "meme_manager_llm_request_origin",
        "LLM_REQUEST_ORIGIN_CHAT": "chat",
        "LLM_REQUEST_ORIGIN_PLUGIN": "plugin",
        "TRIGGER_SCOPE_CHAT_ONLY": "only_chat_llm",
        "TRIGGER_SCOPE_CHAT_AND_PLUGIN": "chat_and_plugin_llm",
        "MEME_PROMPT_MARKER_START": "<!-- meme_manager_prompt:start -->",
        "MEME_PROMPT_MARKER_END": "<!-- meme_manager_prompt:end -->",
        "SEMANTIC_PROMPT_MARKER_START": "<!-- meme_manager_semantic_prompt:start -->",
        "SEMANTIC_PROMPT_MARKER_END": "<!-- meme_manager_semantic_prompt:end -->",
    }
    _compile_selected(
        sources["backend/text_safety.py"],
        {
            "_IMAGE_SUFFIXES", "_IMAGE_REF_LINE_RE", "_FENCE_RE", "_DATA_IMAGE_RE",
            "_BASE64_IMAGE_RE", "_WINDOWS_ABSOLUTE_PATH_RE", "_UNC_PATH_RE",
            "_MARKDOWN_LINK_RE", "_INLINE_CODE_RE", "_URI_RE", "_REFERENCE_TOKEN_RE",
            "_REFERENCE_SCHEME_RE", "_DOMAIN_RE", "_unwrap_reference",
            "_has_supported_image_suffix", "_is_supported_image_reference",
            "_is_plugin_owned_file_image_reference", "strip_internal_image_ref_lines",
            "_token_is_reference", "_merge_spans", "_protected_reference_spans",
            "find_unprotected_word_spans",
        },
        namespace,
    )
    _compile_selected(
        sources["backend/semantic_models.py"], {"runtime_category_mapping"}, namespace
    )
    _compile_selected(
        sources["backend/category_manager.py"],
        {"is_safe_category_name", "resolve_safe_category_directory"},
        namespace,
    )
    _compile_selected(sources["backend/models.py"], {"IMAGE_EXTENSIONS"}, namespace)
    _compile_selected(
        sources["utils.py"], {"dict_to_string", "normalize_probability", "probability_hit"}, namespace
    )
    _compile_selected(sources["mixins/event_handlers.py"], {"normalize_trigger_scope"}, namespace)
    handler = _compile_handler_class(sources["mixins/event_handlers.py"], namespace)

    class Receiver(handler):
        def __init__(self):
            self.config = {"generation": {"markup": {"enable_alternative": False}}}
            self.context = SimpleNamespace(
                send_message=self._record_image_send,
                provider_manager=SimpleNamespace(personas=[]),
            )
            self.trigger_scope = "only_chat_llm"
            self.semantic_enabled = False
            self.emotion_llm_enabled = False
            self.emotion_llm_provider_id = ""
            self.category_mapping = {"happy": "recorded happy"}
            self.category_mapping_string = "happy - recorded happy\n"
            self.prompt_head = "choose:"
            self.prompt_tail_1 = " max="
            self.prompt_tail_2 = ""
            self.sys_prompt_add = ""
            self.max_emotions_per_message = 1
            self.strict_max_emotions_per_message = True
            self.emotions_probability = 100
            self.remove_invalid_alternative_markup = True
            self.streaming_compatibility = False
            self.enable_mixed_message = False
            self.mixed_message_probability = 0
            self.send_image_as_base64 = False
            self.content_cleanup_rule = ""
            self._trace = trace

        async def _record_image_send(self, _umo, chain):
            self._trace.append(("meme_image", chain))

        def _convert_to_gif(self, path):
            return path

    main_methods = (
        "_read_path", "_read_config_value", "_reply_model_supports_tools",
        "_semantic_pack_ready", "_remove_semantic_tool", "_semantic_mode_active",
        "_semantic_system_prompt", "_wrap_meme_prompt", "_strip_meme_prompt",
        "_resolve_persona_id", "_resolve_runtime_pack_context",
        "_get_runtime_memes_dir_for_event", "_build_meme_prompt", "_apply_request_prompt",
    )
    for name in main_methods:
        method = _compile_meme_sender_method(sources["main.py"], name, namespace)
        setattr(Receiver, name, staticmethod(method) if name == "_remove_semantic_tool" else method)
    return Receiver()


class R15MemeOfficialRecordedReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sources = _archive_sources()
        # Existing R14 helper verifies the fixed AstrBot archive and the two
        # ResultDecorate/Respond entry hashes before returning their source.
        self.astr_sources = R14Replay._read_archives()

    async def _respond_via_fixed_astrbot(self, event):
        """Run the archived RespondStage rather than hand-simulating sends."""
        respond_source = self.astr_sources["astrbot/core/pipeline/respond/stage.py"]
        component_type = SimpleNamespace(
            Plain="plain", Reply="reply", At="at", Record="record"
        )
        MAIN.Plain.type = "plain"
        content_type = SimpleNamespace(
            STREAMING_RESULT="streaming", STREAMING_FINISH="finish"
        )
        sleeps = []

        class AsyncioIO:
            async def sleep(self, value):
                sleeps.append(value)

        namespace = {
            "asyncio": AsyncioIO(),
            "math": __import__("math"),
            "random": random,
            "logger": _Log(),
            "Comp": SimpleNamespace(
                Plain=MAIN.Plain, Reply=object, At=object, Record=object, File=object
            ),
            "ComponentType": component_type,
            "ResultContentType": content_type,
            "MessageChain": _MessageChain,
            "call_event_hook": lambda _event, _kind: asyncio.sleep(0, result=False),
            "EventType": SimpleNamespace(OnAfterMessageSentEvent="after"),
            "path_Mapping": lambda _mappings, path: path,
        }
        calculate = R14Replay._method(
            respond_source, "RespondStage", "_calc_comp_interval", namespace
        )
        process = R14Replay._method(respond_source, "RespondStage", "process", namespace)

        async def empty(_chain):
            return False

        stage = SimpleNamespace(
            config={},
            platform_settings={},
            interval_method="random",
            interval=(0, 0),
            _calc_comp_interval=lambda component: calculate(stage, component),
            _is_empty_message_chain=empty,
            is_seg_reply_required=lambda _event: True,
            _extract_comp=lambda _chain, _types, modify_raw_chain: [],
        )
        await R14Replay._drain(process(stage, event))
        return sleeps

    async def _shio_reviewed_response(self, event, initial, replies):
        provider = _ReviewProvider(replies)
        plugin = R14.R14LifecycleAndPresentationTests()._auxiliary_plugin(
            provider,
            review={
                "mode": "core",
                # R16 owns the intentionally tiny deadline cases; this full
                # Meme-chain replay needs ordinary scheduling headroom.
                "timeout_seconds": 1.0,
                "max_repair_attempts": 1,
                "repair_enabled": True,
                "use_repaired_text": True,
                "fallback_provider_ids": [],
            },
        )
        event.set_extra(MAIN.SYS001_TURN_EXTRA, MAIN.create_snapshot(FakeEvent(), origin="direct"))
        event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, MAIN.TurnLifecycle())
        response = SimpleNamespace(role="assistant", completion_text=initial, reasoning_content="")
        await plugin.observe_final_agent_response(event, response)
        self.assertTrue(all(func_tool is None for _prompt, func_tool in provider.calls))
        return plugin, response

    def test_fixed_hook_priorities_keep_request_projection_last(self):
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
        priorities = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "MemeSender":
                for member in node.body:
                    if isinstance(member, ast.AsyncFunctionDef) and member.name in {
                        "inject_meme_prompt", "resp", "on_decorating_result"
                    }:
                        priorities[member.name] = ast.unparse(member.decorator_list[0])
        self.assertEqual({"inject_meme_prompt", "resp", "on_decorating_result"}, set(priorities))
        self.assertTrue(all("99999" in value for value in priorities.values()))

    async def test_fixed_meme_hooks_select_from_shio_reviewed_text_then_separate_send(self):
        """No selected IDs are injected: archived response/decorating own them."""
        with tempfile.TemporaryDirectory(prefix="sys001-r15-meme-") as root:
            pack_dir = Path(root) / "pack"
            memes_dir = pack_dir / "memes"
            image_dir = memes_dir / "happy"
            image_dir.mkdir(parents=True)
            (image_dir / "recorded.png").write_bytes(b"recorded image fixture")
            trace = []
            receiver = _official_receiver(
                self.sources, memes_dir=memes_dir, pack_dir=pack_dir, trace=trace
            )
            event = _ReplayEvent()
            request = _Request()
            await receiver._mark_llm_request_origin_impl(event)
            await receiver._inject_meme_prompt_impl(event, request)
            self.assertIn("<!-- meme_manager_prompt:start -->", request.system_prompt)

            plugin, response = await self._shio_reviewed_response(
                event,
                "draft &&invalid&&",
                [
                    '{"action":"replace","text":"reviewed-happy &&happy&&"}',
                    '{"action":"keep"}',
                ],
            )
            self.assertEqual("reviewed-happy &&happy&&", response.completion_text)
            await receiver._resp_impl(event, response)
            self.assertEqual("reviewed-happy", response.completion_text)
            self.assertEqual(["happy"], event.get_extra("found_emotions"))

            event.result = _Result([MAIN.Plain(response.completion_text)])
            plugin.config["sys001"]["presentation"] = {
                "text_component_mode": "plugin",
                "text_component_min_segments": 2,
                "text_component_max_segments": 2,
            }
            await plugin.layout_text_components(event)
            text_before_meme = [part.text for part in event.result.chain]
            self.assertEqual("reviewed-happy", "".join(text_before_meme))
            self.assertEqual(2, len(text_before_meme))

            await receiver._on_decorating_result_impl(event)
            self.assertEqual(text_before_meme, [part.text for part in event.result.chain])
            pending = event.get_extra("meme_manager_pending_images")
            self.assertEqual(1, len(pending or []))
            self.assertTrue(all(isinstance(part, MAIN.Plain) for part in event.result.chain))

            # The fixed AstrBot RespondStage owns segmented text delivery;
            # only after it completes does Meme's fixed after hook send image.
            sleeps = await self._respond_via_fixed_astrbot(event)
            self.assertEqual(2, len([item for item in event.delivery_trace if item[0] == "official_text"]))
            self.assertEqual(2, len(sleeps))
            await receiver._after_message_sent_impl(event)
            self.assertEqual(["official_text", "official_text", "meme_image"], [kind for kind, _value in event.delivery_trace + trace])
            self.assertIsNone(event.get_extra("meme_manager_pending_images"))
            self.assertEqual(1, len([item for item in trace if item[0] == "meme_image"]))

    async def test_fixed_meme_hooks_do_not_attach_when_shio_final_text_has_no_valid_tag(self):
        with tempfile.TemporaryDirectory(prefix="sys001-r15-meme-") as root:
            pack_dir = Path(root) / "pack"
            memes_dir = pack_dir / "memes"
            (memes_dir / "happy").mkdir(parents=True)
            trace = []
            receiver = _official_receiver(
                self.sources, memes_dir=memes_dir, pack_dir=pack_dir, trace=trace
            )
            event = _ReplayEvent()
            request = _Request()
            await receiver._mark_llm_request_origin_impl(event)
            await receiver._inject_meme_prompt_impl(event, request)
            plugin, response = await self._shio_reviewed_response(
                event, "draft &&invalid&&", ['{"action":"keep"}']
            )
            self.assertEqual("draft &&invalid&&", response.completion_text)
            await receiver._resp_impl(event, response)
            self.assertEqual("draft", response.completion_text)
            self.assertEqual([], event.get_extra("found_emotions"))
            event.result = _Result([MAIN.Plain(response.completion_text)])
            await plugin.layout_text_components(event)
            await receiver._on_decorating_result_impl(event)
            self.assertIsNone(event.get_extra("meme_manager_pending_images"))
            await receiver._after_message_sent_impl(event)
            self.assertEqual([], trace)
