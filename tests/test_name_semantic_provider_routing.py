"""Behavior tests for SYS-001 name-semantic Provider routing.

The real AstrBot package is deliberately not imported here.  A minimal public
API stub loads the plugin as a plugin would see it, while fake Context and
Provider objects exercise the concrete routing behavior.
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import json
import sys
import time
import types
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from pathlib import Path

from astrbot.api.event import filter as _FIXED_FILTER
from astrbot.builtin_stars.astrbot import group_chat_context as _FIXED_GROUP_CONTEXT
import astrbot.core.star as _FIXED_STAR_PACKAGE
from astrbot.core.star import base as _FIXED_STAR_BASE
from astrbot.core.star import star as _FIXED_STAR
from astrbot.core.star import star_handler as _FIXED_STAR_HANDLER
from astrbot.core.star.register import star_handler as _FIXED_STAR_REGISTER


_FIXED_ASTRBOT_MODULES = {
    name: module
    for name, module in sys.modules.items()
    if name == "astrbot" or name.startswith("astrbot.")
}
_PUBLIC_API_MODULES = (
    "astrbot",
    "astrbot.api",
    "astrbot.api.event",
    "astrbot.api.message_components",
    "astrbot.api.provider",
    "astrbot.api.star",
)
_MISSING = object()


def _install_astrbot_public_api_stub() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    provider = types.ModuleType("astrbot.api.provider")
    message_components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")

    def decorator_factory(event_type):
        def factory(*_args, **kwargs):
            def decorate(function):
                return function

            return decorate

        return factory

    class Filter:
        PlatformAdapterType = SimpleNamespace(ALL="ALL")
        platform_adapter_type = staticmethod(decorator_factory("adapter"))
        on_llm_request = staticmethod(decorator_factory("llm_request"))
        on_llm_response = staticmethod(decorator_factory("llm_response"))
        on_agent_begin = staticmethod(decorator_factory("agent_begin"))
        on_agent_done = staticmethod(decorator_factory("agent_done"))
        on_decorating_result = staticmethod(decorator_factory("decorating"))
        after_message_sent = staticmethod(decorator_factory("after_send"))
        on_llm_tool_respond = staticmethod(decorator_factory("tool_response"))

    class Star:
        def __init__(self, context) -> None:
            self.context = context

    class ToolSet:
        def __init__(self, tools=None) -> None:
            self.tools = list(tools or [])

        def add_tool(self, _tool) -> None:
            self.tools.append(_tool)

    class FunctionTool:
        """Minimal fixed-4.27.4-shaped tool for the isolated public API shim."""

        def __init__(self, *, name, description, parameters, handler=None) -> None:
            self.name = name
            self.description = description
            self.parameters = parameters
            self.handler = handler
            self.active = True

    class Plain:
        def __init__(self, text):
            self.text = text

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

    class ResultContentType(enum.Enum):
        LLM_RESULT = enum.auto()
        AGENT_RUNNER_ERROR = enum.auto()
        GENERAL_RESULT = enum.auto()
        STREAMING_RESULT = enum.auto()
        STREAMING_FINISH = enum.auto()

    api.AstrBotConfig = dict
    api.FunctionTool = FunctionTool
    api.ToolSet = ToolSet
    api.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None, info=lambda *_args, **_kwargs: None)
    event.AstrMessageEvent = object
    event.MessageChain = MessageChain
    event.ResultContentType = ResultContentType
    event.filter = Filter
    message_components.Plain = Plain
    provider.LLMResponse = object
    provider.ProviderRequest = object
    star.Context = object
    star.Star = Star

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.message_components": message_components,
            "astrbot.api.provider": provider,
            "astrbot.api.star": star,
        }
    )


def _function_tool(name: str):
    """Use a real FunctionTool when another replay already loaded fixed core."""
    tool_module = sys.modules.get("astrbot.core.agent.tool")
    tool_type = getattr(tool_module, "FunctionTool", None)
    if tool_type is None:
        tool_type = sys.modules["astrbot.api"].FunctionTool
    return tool_type(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}},
        handler=None,
    )


def _snapshot_fields(objects):
    """Capture object fields, preserving container identities and contents."""
    snapshots = []
    seen = set()
    for obj in objects:
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        fields = []
        for name, value in vars(obj).items():
            if isinstance(value, list):
                fields.append((name, "list", value, tuple(value)))
            elif isinstance(value, dict):
                fields.append((name, "dict", value, tuple(value.items())))
            elif isinstance(value, set):
                fields.append((name, "set", value, frozenset(value)))
            else:
                fields.append((name, "value", value, None))
        snapshots.append((obj, tuple(fields)))
    return tuple(snapshots)


def _restore_fields(snapshots) -> None:
    for obj, fields in snapshots:
        saved_names = {name for name, *_ in fields}
        for name in tuple(vars(obj)):
            if name not in saved_names:
                delattr(obj, name)
        for name, kind, value, contents in fields:
            if kind == "list":
                value[:] = contents
            elif kind == "dict":
                value.clear()
                value.update(contents)
            elif kind == "set":
                value.clear()
                value.update(contents)
            setattr(obj, name, value)


def _assert_fields_restored(snapshots) -> None:
    for obj, fields in snapshots:
        assert tuple(vars(obj)) == tuple(name for name, *_ in fields)
        for name, kind, value, contents in fields:
            current = getattr(obj, name)
            assert current is value
            if kind == "list":
                assert tuple(current) == contents
            elif kind == "dict":
                assert tuple(current.items()) == contents
            elif kind == "set":
                assert frozenset(current) == contents


def _snapshot_handler_registries(*registries):
    snapshots = []
    seen = set()
    for registry in registries:
        if id(registry) in seen:
            continue
        seen.add(id(registry))
        handlers_map = registry.star_handlers_map
        handlers = registry._handlers
        map_entries = tuple(handlers_map.items())
        handler_entries = tuple(handlers)
        snapshots.append(
            (
                registry,
                handlers_map,
                handlers,
                map_entries,
                handler_entries,
                _snapshot_fields([*handlers_map.values(), *handlers]),
            )
        )
    return tuple(snapshots)


def _restore_handler_registries(snapshots) -> None:
    for registry, handlers_map, handlers, map_entries, handler_entries, fields in snapshots:
        registry.star_handlers_map = handlers_map
        registry._handlers = handlers
        handlers_map.clear()
        handlers_map.update(map_entries)
        handlers[:] = handler_entries
        _restore_fields(fields)


def _assert_handler_registries_restored(snapshots) -> None:
    for registry, handlers_map, handlers, map_entries, handler_entries, fields in snapshots:
        assert registry.star_handlers_map is handlers_map
        assert registry._handlers is handlers
        assert tuple(handlers_map.items()) == map_entries
        assert all(
            current is saved
            for (_key, current), (_saved_key, saved) in zip(
                handlers_map.items(), map_entries, strict=True
            )
        )
        assert len(handlers) == len(handler_entries)
        assert all(current is saved for current, saved in zip(handlers, handler_entries, strict=True))
        _assert_fields_restored(fields)


def _snapshot_star_registration_state():
    star_map = _FIXED_STAR.star_map
    star_registry = _FIXED_STAR.star_registry
    map_refs = (
        (_FIXED_STAR_PACKAGE, "star_map"),
        (_FIXED_STAR, "star_map"),
        (_FIXED_STAR_BASE, "star_map"),
        (_FIXED_STAR_HANDLER, "star_map"),
    )
    registry_refs = (
        (_FIXED_STAR_PACKAGE, "star_registry"),
        (_FIXED_STAR, "star_registry"),
        (_FIXED_STAR_BASE, "star_registry"),
    )
    assert all(getattr(module, name) is star_map for module, name in map_refs)
    assert all(getattr(module, name) is star_registry for module, name in registry_refs)
    map_entries = tuple(star_map.items())
    registry_entries = tuple(star_registry)
    return (
        star_map,
        star_registry,
        map_refs,
        registry_refs,
        map_entries,
        registry_entries,
        _snapshot_fields([*star_map.values(), *star_registry]),
    )


def _restore_star_registration_state(snapshot) -> None:
    (
        star_map,
        star_registry,
        map_refs,
        registry_refs,
        map_entries,
        registry_entries,
        fields,
    ) = snapshot
    for module, name in map_refs:
        setattr(module, name, star_map)
    for module, name in registry_refs:
        setattr(module, name, star_registry)
    star_map.clear()
    star_map.update(map_entries)
    star_registry[:] = registry_entries
    _restore_fields(fields)


def _assert_star_registration_state_restored(snapshot) -> None:
    (
        star_map,
        star_registry,
        map_refs,
        registry_refs,
        map_entries,
        registry_entries,
        fields,
    ) = snapshot
    assert all(getattr(module, name) is star_map for module, name in map_refs)
    assert all(getattr(module, name) is star_registry for module, name in registry_refs)
    assert tuple(star_map.items()) == map_entries
    assert all(
        current is saved
        for (_key, current), (_saved_key, saved) in zip(
            star_map.items(), map_entries, strict=True
        )
    )
    assert len(star_registry) == len(registry_entries)
    assert all(
        current is saved
        for current, saved in zip(star_registry, registry_entries, strict=True)
    )
    _assert_fields_restored(fields)


def _official_registry_order(*handler_names: str):
    """Read fixed-4.27.4 metadata and leave every test-global unchanged."""
    registry = _FIXED_STAR_HANDLER.StarHandlerRegistry()
    saved_astrbot_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "astrbot" or name.startswith("astrbot.")
    }
    saved_parent_package = sys.modules.get("astrbot_plugin_shio", _MISSING)
    saved_parent_main = (
        getattr(saved_parent_package, "main", _MISSING)
        if saved_parent_package is not _MISSING
        else _MISSING
    )
    saved_main = sys.modules.pop("astrbot_plugin_shio.main", _MISSING)
    saved_core = sys.modules.get("astrbot_plugin_shio.core", _MISSING)
    saved_sys001 = sys.modules.get("astrbot_plugin_shio.core.sys001", _MISSING)
    saved_parent_core = (
        getattr(saved_parent_package, "core", _MISSING)
        if saved_parent_package is not _MISSING
        else _MISSING
    )
    core_package = saved_core if saved_core is not _MISSING else None
    saved_core_sys001 = (
        getattr(core_package, "sys001", _MISSING)
        if core_package is not None
        else _MISSING
    )
    saved_registry = _FIXED_STAR_HANDLER.star_handlers_registry
    saved_register_registry = _FIXED_STAR_REGISTER.star_handlers_registry
    handler_snapshot = _snapshot_handler_registries(
        saved_registry, saved_register_registry
    )
    star_snapshot = _snapshot_star_registration_state()
    try:
        sys.modules.update(_FIXED_ASTRBOT_MODULES)
        _FIXED_STAR_HANDLER.star_handlers_registry = registry
        _FIXED_STAR_REGISTER.star_handlers_registry = registry
        probe = importlib.import_module("astrbot_plugin_shio.main")
        module_path = probe.ShioPlugin.__module__
        expected_full_names = {
            f"{module_path}_{handler_name}" for handler_name in handler_names
        }
        ordered = [
            metadata
            for metadata in registry
            if metadata.handler_full_name in expected_full_names
        ]
        assert len(ordered) == len(handler_names)
        return ordered
    finally:
        _FIXED_STAR_HANDLER.star_handlers_registry = saved_registry
        _FIXED_STAR_REGISTER.star_handlers_registry = saved_register_registry
        _restore_handler_registries(handler_snapshot)
        _restore_star_registration_state(star_snapshot)

        for name in tuple(sys.modules):
            if (name == "astrbot" or name.startswith("astrbot.")) and name not in saved_astrbot_modules:
                sys.modules.pop(name)
        sys.modules.update(saved_astrbot_modules)
        _restore_module_entry("astrbot_plugin_shio.main", saved_main)
        _restore_module_entry("astrbot_plugin_shio.core", saved_core)
        _restore_module_entry("astrbot_plugin_shio.core.sys001", saved_sys001)
        if saved_parent_package is _MISSING:
            sys.modules.pop("astrbot_plugin_shio", None)
        else:
            sys.modules["astrbot_plugin_shio"] = saved_parent_package
            _restore_parent_attribute(
                saved_parent_package, "main", saved_parent_main
            )
            _restore_parent_attribute(
                saved_parent_package, "core", saved_parent_core
            )
        if core_package is not None:
            _restore_parent_attribute(core_package, "sys001", saved_core_sys001)

        assert _FIXED_STAR_HANDLER.star_handlers_registry is saved_registry
        assert _FIXED_STAR_REGISTER.star_handlers_registry is saved_register_registry
        _assert_handler_registries_restored(handler_snapshot)
        _assert_star_registration_state_restored(star_snapshot)
        assert sys.modules.get("astrbot_plugin_shio.main", _MISSING) is saved_main
        assert sys.modules.get("astrbot_plugin_shio.core", _MISSING) is saved_core
        assert (
            sys.modules.get("astrbot_plugin_shio.core.sys001", _MISSING)
            is saved_sys001
        )
        assert (
            sys.modules.get("astrbot_plugin_shio", _MISSING) is saved_parent_package
        )
        if saved_parent_package is not _MISSING:
            assert getattr(saved_parent_package, "main", _MISSING) is saved_parent_main
            assert getattr(saved_parent_package, "core", _MISSING) is saved_parent_core
        if core_package is not None:
            assert getattr(core_package, "sys001", _MISSING) is saved_core_sys001
        for name, module in saved_astrbot_modules.items():
            assert sys.modules.get(name) is module


def _restore_module_entry(name: str, module) -> None:
    if module is _MISSING:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module


def _restore_parent_attribute(parent, name: str, value) -> None:
    if value is _MISSING:
        if hasattr(parent, name):
            delattr(parent, name)
    else:
        setattr(parent, name, value)


def _load_module_isolated_stubbed_plugin():
    """Capture fake plugin bindings without leaking them past module collection."""
    saved_astrbot_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "astrbot" or name.startswith("astrbot.")
    }
    saved_parent_package = sys.modules.get("astrbot_plugin_shio", _MISSING)
    saved_main = sys.modules.get("astrbot_plugin_shio.main", _MISSING)
    saved_core = sys.modules.get("astrbot_plugin_shio.core", _MISSING)
    saved_sys001 = sys.modules.get("astrbot_plugin_shio.core.sys001", _MISSING)
    saved_parent_main = (
        getattr(saved_parent_package, "main", _MISSING)
        if saved_parent_package is not _MISSING
        else _MISSING
    )
    saved_parent_core = (
        getattr(saved_parent_package, "core", _MISSING)
        if saved_parent_package is not _MISSING
        else _MISSING
    )
    core_package = saved_core if saved_core is not _MISSING else None
    saved_core_sys001 = (
        getattr(core_package, "sys001", _MISSING)
        if core_package is not None
        else _MISSING
    )
    saved_registry = _FIXED_STAR_HANDLER.star_handlers_registry
    saved_register_registry = _FIXED_STAR_REGISTER.star_handlers_registry
    handler_snapshot = _snapshot_handler_registries(
        saved_registry, saved_register_registry
    )
    star_snapshot = _snapshot_star_registration_state()
    try:
        _install_astrbot_public_api_stub()
        sys.modules.pop("astrbot_plugin_shio.main", None)
        main = importlib.import_module("astrbot_plugin_shio.main")
        sys001 = importlib.import_module("astrbot_plugin_shio.core.sys001")
        return main, sys001
    finally:
        _FIXED_STAR_HANDLER.star_handlers_registry = saved_registry
        _FIXED_STAR_REGISTER.star_handlers_registry = saved_register_registry
        _restore_handler_registries(handler_snapshot)
        _restore_star_registration_state(star_snapshot)

        for name in tuple(sys.modules):
            if (name == "astrbot" or name.startswith("astrbot.")) and name not in saved_astrbot_modules:
                sys.modules.pop(name)
        sys.modules.update(saved_astrbot_modules)
        _restore_module_entry("astrbot_plugin_shio.main", saved_main)
        _restore_module_entry("astrbot_plugin_shio.core", saved_core)
        _restore_module_entry("astrbot_plugin_shio.core.sys001", saved_sys001)
        if saved_parent_package is _MISSING:
            sys.modules.pop("astrbot_plugin_shio", None)
        else:
            sys.modules["astrbot_plugin_shio"] = saved_parent_package
            _restore_parent_attribute(
                saved_parent_package, "main", saved_parent_main
            )
            _restore_parent_attribute(
                saved_parent_package, "core", saved_parent_core
            )
        if core_package is not None:
            _restore_parent_attribute(core_package, "sys001", saved_core_sys001)

        assert _FIXED_STAR_HANDLER.star_handlers_registry is saved_registry
        assert _FIXED_STAR_REGISTER.star_handlers_registry is saved_register_registry
        _assert_handler_registries_restored(handler_snapshot)
        _assert_star_registration_state_restored(star_snapshot)
        for name, module in saved_astrbot_modules.items():
            assert sys.modules.get(name) is module
        assert sys.modules.get("astrbot_plugin_shio.main", _MISSING) is saved_main
        assert sys.modules.get("astrbot_plugin_shio.core", _MISSING) is saved_core
        assert sys.modules.get("astrbot_plugin_shio.core.sys001", _MISSING) is saved_sys001
        assert (
            sys.modules.get("astrbot_plugin_shio", _MISSING) is saved_parent_package
        )
        if saved_parent_package is not _MISSING:
            assert getattr(saved_parent_package, "main", _MISSING) is saved_parent_main
            assert getattr(saved_parent_package, "core", _MISSING) is saved_parent_core
        if core_package is not None:
            assert getattr(core_package, "sys001", _MISSING) is saved_core_sys001


MAIN, SYS001 = _load_module_isolated_stubbed_plugin()


class FakeEvent:
    unified_msg_origin = "qq:group:202"
    created_at = 1_704_067_200.0
    message_obj = SimpleNamespace(message_id="message-1")

    def __init__(self, text: str = "Shio, hello") -> None:
        self._text = text

    def get_messages(self):
        return []

    def get_self_id(self):
        return "999"

    def get_message_str(self):
        return self._text

    def get_platform_id(self):
        return "qq"

    def get_sender_id(self):
        return "202"

    def get_sender_name(self):
        return "synthetic member"

    def is_private_chat(self):
        return False

    def is_admin(self):
        return False


class FakeProvider:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def text_chat(self, *, prompt, func_tool):
        self.calls.append({"prompt": prompt, "func_tool": func_tool})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, tuple) and outcome[0] == "sleep":
            await asyncio.sleep(outcome[1])
            outcome = outcome[2]
        return SimpleNamespace(completion_text=outcome)


class FakeContext:
    def __init__(self, current=None, by_id=None, lookup_errors=None) -> None:
        self.current = current
        self.by_id = by_id or {}
        self.lookup_errors = lookup_errors or set()
        self.current_calls = 0
        self.lookup_ids: list[str] = []

    def get_config(self, *, umo):
        assert umo == "qq:group:202"
        return {"wake_prefix": ["Shio"]}

    async def get_using_provider_async(self, umo):
        assert umo == "qq:group:202"
        self.current_calls += 1
        return self.current

    def get_provider_by_id(self, provider_id):
        self.lookup_ids.append(provider_id)
        if provider_id in self.lookup_errors:
            raise RuntimeError("lookup failed")
        return self.by_id.get(provider_id)


class NameSemanticProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, context, **group):
        instance = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        instance.context = context
        instance.config = {"sys001": {"group": {"name_wake_mode": "semantic", **group}}}
        return instance

    async def test_direct_mode_never_calls_semantic_provider(self) -> None:
        provider = FakeProvider('{"decision":"DIRECT"}')
        plugin = self.plugin(FakeContext(current=provider), name_wake_mode="direct")

        self.assertTrue(await plugin._visible_name_wake(FakeEvent()))
        self.assertEqual([], provider.calls)

    def test_schema_sets_the_documented_per_route_timeout_default(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        timeout = schema["sys001"]["items"]["group"]["items"][
            "name_semantic_timeout_seconds"
        ]
        self.assertEqual(("int", 8), (timeout["type"], timeout["default"]))

    async def test_direct_decision_accepts_current_message_without_tool(self) -> None:
        provider = FakeProvider('{"decision":"DIRECT"}')
        plugin = self.plugin(FakeContext(current=provider), name_semantic_prompt="classify")

        self.assertTrue(await plugin._visible_name_wake(FakeEvent("Shio, current text")))
        self.assertEqual(1, len(provider.calls))
        self.assertIsNone(provider.calls[0]["func_tool"])
        self.assertIn("current_message=Shio, current text", provider.calls[0]["prompt"])
        self.assertIn("sender_id=202", provider.calls[0]["prompt"])
        self.assertIn("scope=qq:group:202", provider.calls[0]["prompt"])

    async def test_legal_mention_or_uncertain_stops_without_fallback(self) -> None:
        for decision in ("MENTION", "UNCERTAIN"):
            with self.subTest(decision=decision):
                current = FakeProvider(f'{{"decision":"{decision}"}}')
                fallback = FakeProvider('{"decision":"DIRECT"}')
                context = FakeContext(current=current, by_id={"fallback": fallback})
                plugin = self.plugin(
                    context,
                    name_semantic_fallback_provider_ids=["fallback"],
                )

                self.assertFalse(await plugin._visible_name_wake(FakeEvent()))
                self.assertEqual(1, len(current.calls))
                self.assertEqual([], fallback.calls)

    async def test_explicit_provider_precedes_fallback_without_current_provider(self) -> None:
        explicit = FakeProvider("not-json")
        fallback = FakeProvider('{"decision":"DIRECT"}')
        context = FakeContext(by_id={"explicit": explicit, "fallback": fallback})
        plugin = self.plugin(
            context,
            name_semantic_provider_id="explicit",
            name_semantic_fallback_provider_ids=["fallback"],
        )

        self.assertTrue(await plugin._visible_name_wake(FakeEvent()))
        self.assertEqual(0, context.current_calls)
        self.assertEqual(["explicit", "fallback"], context.lookup_ids)
        self.assertEqual(1, len(explicit.calls))
        self.assertEqual(1, len(fallback.calls))

    async def test_provider_lookup_error_is_skipped_for_the_next_route(self) -> None:
        fallback = FakeProvider('{"decision":"DIRECT"}')
        context = FakeContext(
            by_id={"fallback": fallback},
            lookup_errors={"missing"},
        )
        plugin = self.plugin(
            context,
            name_semantic_provider_id="missing",
            name_semantic_fallback_provider_ids=["fallback"],
        )

        self.assertTrue(await plugin._visible_name_wake(FakeEvent()))
        self.assertEqual(["missing", "fallback"], context.lookup_ids)
        self.assertEqual(1, len(fallback.calls))

    async def test_current_provider_precedes_fallback_and_duplicates_are_identity_deduped(self) -> None:
        current = FakeProvider("not-json")
        fallback = FakeProvider('{"decision":"DIRECT"}')
        context = FakeContext(current=current, by_id={"same": current, "fallback": fallback})
        plugin = self.plugin(
            context,
            name_semantic_fallback_provider_ids=["same", "fallback"],
        )

        self.assertTrue(await plugin._visible_name_wake(FakeEvent()))
        self.assertEqual(1, context.current_calls)
        self.assertEqual(["same", "fallback"], context.lookup_ids)
        self.assertEqual(1, len(current.calls))
        self.assertEqual(1, len(fallback.calls))

    async def test_fast_exception_empty_and_invalid_continue_within_total_budget(self) -> None:
        providers = [
            FakeProvider(RuntimeError("provider failed")),
            FakeProvider(""),
            FakeProvider("not-json"),
            FakeProvider('{"decision":"DIRECT"}'),
        ]
        ids = [f"p{index}" for index in range(len(providers))]
        context = FakeContext(by_id=dict(zip(ids, providers)))
        plugin = self.plugin(
            context,
            name_semantic_provider_id="p0",
            name_semantic_fallback_provider_ids=ids[1:],
            name_semantic_timeout_seconds=0.5,
        )

        self.assertTrue(await plugin._visible_name_wake(FakeEvent()))
        self.assertEqual([1, 1, 1, 1], [len(provider.calls) for provider in providers])

    async def test_blocked_route_exhausts_total_budget_before_later_fallbacks(self) -> None:
        class _CancellationResistantProvider:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.cancel_seen = asyncio.Event()

            async def text_chat(self, *, prompt, func_tool):
                self.calls.append({"prompt": prompt, "func_tool": func_tool})
                self.started.set()
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        self.cancel_seen.set()
                return SimpleNamespace(completion_text='{"decision":"DIRECT"}')

        blocked = _CancellationResistantProvider()
        later = [FakeProvider(""), FakeProvider("not-json"), FakeProvider('{"decision":"DIRECT"}')]
        providers = [FakeProvider(RuntimeError("provider failed")), blocked, *later]
        ids = [f"p{index}" for index in range(len(providers))]
        context = FakeContext(by_id=dict(zip(ids, providers)))
        plugin = self.plugin(
            context,
            name_semantic_provider_id="p0",
            name_semantic_fallback_provider_ids=ids[1:],
            name_semantic_timeout_seconds=0.05,
        )
        try:
            entry = asyncio.create_task(plugin._visible_name_wake(FakeEvent()))
            await asyncio.wait_for(blocked.started.wait(), timeout=2.0)
            self.assertFalse(await asyncio.wait_for(entry, timeout=2.0))
            self.assertTrue(blocked.cancel_seen.is_set())
            self.assertEqual([1, 1, 0, 0, 0], [len(provider.calls) for provider in providers])
            self.assertTrue(plugin._auxiliary_tasks)
        finally:
            blocked.release.set()
            tasks = tuple(getattr(plugin, "_auxiliary_tasks", set()))
            if tasks:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=2.0
                )
            await asyncio.sleep(0)
        self.assertEqual(set(), getattr(plugin, "_auxiliary_tasks", set()))

    async def test_cancelled_error_propagates_without_fallback(self) -> None:
        current = FakeProvider(asyncio.CancelledError())
        fallback = FakeProvider('{"decision":"DIRECT"}')
        context = FakeContext(current=current, by_id={"fallback": fallback})
        plugin = self.plugin(
            context,
            name_semantic_fallback_provider_ids=["fallback"],
        )

        with self.assertRaises(asyncio.CancelledError):
            await plugin._visible_name_wake(FakeEvent())
        self.assertEqual([], fallback.calls)


class IngressAdmissionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def snapshot(**changes):
        values = dict(
            platform_id="qq",
            scope="qq:group:202",
            account_id="999",
            sender_id="202",
            sender_name="member",
            self_id="999",
            message_id="message",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            message_text="hello",
            is_private=False,
            is_master=False,
            at_self=True,
            reply_to_self=False,
            origin="pending",
        )
        values.update(changes)
        return MAIN.TurnSnapshot(**values)

    def decide(self, snapshot, **lists):
        return MAIN.admit_ingress(
            snapshot,
            private_allowed_sender_ids=frozenset(lists.get("private", ())),
            blocked_sender_ids=frozenset(lists.get("blocked", ())),
            group_allowed_scopes=frozenset(lists.get("group", ())),
            group_blocked_scopes=frozenset(lists.get("group_blocked", ())),
        )

    def test_truth_table_for_private_group_blocks_and_master_bypass(self) -> None:
        private = self.snapshot(is_private=True)
        group = self.snapshot()
        cases = [
            (private, {"private": ["202"]}, True, "private_sender_allowed"),
            (private, {}, False, "private_sender_not_allowed"),
            (group, {"group": ["qq:group:202"]}, True, "group_scope_allowed"),
            (group, {}, False, "group_scope_not_allowed"),
            (private, {"private": ["202"], "blocked": ["202"]}, False, "blocked_sender"),
            (group, {"group": ["qq:group:202"], "group_blocked": ["qq:group:202"]}, False, "group_scope_blocked"),
            (self.snapshot(is_master=True), {"private": ["other"], "blocked": ["202"], "group": ["other:scope"], "group_blocked": ["qq:group:202"]}, True, "master_bypass"),
            (self.snapshot(is_private=True, is_master=True), {"private": ["other"], "blocked": ["202"], "group": ["other:scope"], "group_blocked": ["qq:group:202"]}, True, "master_bypass"),
        ]
        for snapshot, lists, allowed, reason in cases:
            with self.subTest(reason=reason):
                decision = self.decide(snapshot, **lists)
                self.assertEqual((allowed, reason), (decision.allowed, decision.reason))

    def test_missing_identity_and_friendly_like_sender_fail_closed(self) -> None:
        self.assertFalse(self.decide(self.snapshot(sender_id=""), group=["qq:group:202"]).allowed)
        self.assertFalse(self.decide(self.snapshot(self_id=""), group=["qq:group:202"]).allowed)
        self.assertFalse(self.decide(self.snapshot(sender_id="999"), group=["qq:group:202"]).allowed)
        self.assertFalse(self.decide(self.snapshot(), group=[]).allowed)

    async def test_rejected_event_does_not_create_live_turn_or_call_conversation_provider_or_send(self) -> None:
        class RejectedEvent(FakeEvent):
            def __init__(self):
                super().__init__()
                self.extras = {}
                self.conversation_calls = 0
                self.send_calls = 0

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

            def should_call_llm(self, *_args):
                raise AssertionError("rejected event must not request an LLM")

            async def request_llm(self, *_args, **_kwargs):
                raise AssertionError("rejected event must not request a Provider")

            def send(self, *_args, **_kwargs):
                self.send_calls += 1
                raise AssertionError("rejected event must not send")

        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {"ingress": {"group_allowed_scopes": []}}}
        plugin.context = SimpleNamespace(
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("rejected event must not create a conversation")
                )
            )
        )
        event = RejectedEvent()

        yielded = [request async for request in plugin.request_event_bound_reply(event)]

        self.assertEqual([], yielded)
        self.assertNotIn(MAIN.SYS001_TURN_EXTRA, event.extras)
        self.assertNotIn(MAIN.SYS001_LIFECYCLE_EXTRA, event.extras)
        self.assertEqual(0, event.send_calls)


class CapabilityVisibilityTests(unittest.TestCase):
    def plugin(self, **visibility):
        instance = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        instance.config = {"sys001": {"capability_visibility": visibility}}
        return instance

    @staticmethod
    def snapshot(*, is_private=False, is_master=False, sender_id="202"):
        return SimpleNamespace(
            is_private=is_private,
            is_master=is_master,
            sender_id=sender_id,
        )

    @staticmethod
    def request(tools, *, system_prompt="existing", extra_parts=None):
        return SimpleNamespace(
            func_tool=MAIN.ToolSet(tools=tools) if tools is not None else None,
            system_prompt=system_prompt,
            extra_user_content_parts=list(extra_parts or []),
        )

    def test_schema_replaces_ambiguous_group_capabilities(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        items = json.loads(schema_path.read_text(encoding="utf-8"))["sys001"]["items"]
        self.assertNotIn("group_visible_capabilities", items)
        visibility = items["capability_visibility"]["items"]
        self.assertEqual(
            {"friendly_sender_ids", "ordinary_capability_names"},
            set(visibility),
        )

    def test_ordinary_group_user_keeps_current_allowed_tools_and_kb_in_order(self) -> None:
        denied = _function_tool("denied")
        allowed_first = _function_tool("chat")
        knowledge_base = _function_tool("astr_kb_search")
        allowed_second = _function_tool("chat")
        request = self.request([denied, allowed_first, knowledge_base, allowed_second])
        original_toolset = request.func_tool
        plugin = self.plugin(ordinary_capability_names=["chat"])

        plugin._project_group_visible_tools(self.snapshot(), request)

        self.assertIsNot(request.func_tool, original_toolset)
        self.assertEqual(
            [allowed_first, knowledge_base, allowed_second],
            request.func_tool.tools,
        )
        self.assertEqual(
            [id(allowed_first), id(knowledge_base), id(allowed_second)],
            [id(tool) for tool in request.func_tool.tools],
        )

    def test_empty_ordinary_list_still_keeps_agentic_knowledge_base(self) -> None:
        denied = _function_tool("denied")
        knowledge_base = _function_tool("astr_kb_search")
        request = self.request([denied, knowledge_base])

        self.plugin()._project_group_visible_tools(self.snapshot(), request)

        self.assertEqual([knowledge_base], request.func_tool.tools)

    def test_master_and_friendly_users_keep_the_same_current_toolset(self) -> None:
        tools = [_function_tool("admin"), _function_tool("chat")]
        master_request = self.request(tools)
        friendly_request = self.request(tools)
        master_original = master_request.func_tool
        friendly_original = friendly_request.func_tool
        plugin = self.plugin(friendly_sender_ids=["202"])

        plugin._project_group_visible_tools(
            self.snapshot(is_master=True), master_request
        )
        friendly_snapshot = MAIN.create_snapshot(FakeEvent(), origin="direct")
        plugin._project_group_visible_tools(friendly_snapshot, friendly_request)

        self.assertIs(master_original, master_request.func_tool)
        self.assertIs(friendly_original, friendly_request.func_tool)
        self.assertIs(master_request.func_tool.tools[0], tools[0])
        self.assertIs(friendly_request.func_tool.tools[0], tools[0])
        self.assertFalse(friendly_snapshot.is_master)

    def test_missing_sender_fails_closed_and_private_or_empty_toolsets_are_untouched(self) -> None:
        denied = _function_tool("denied")
        knowledge_base = _function_tool("astr_kb_search")
        missing_sender_request = self.request([denied, knowledge_base])
        private_request = self.request([denied])
        empty_request = self.request(None)
        plugin = self.plugin(friendly_sender_ids=["202"])

        plugin._project_group_visible_tools(
            self.snapshot(sender_id=""), missing_sender_request
        )
        private_original = private_request.func_tool
        plugin._project_group_visible_tools(
            self.snapshot(is_private=True), private_request
        )
        plugin._project_group_visible_tools(self.snapshot(), empty_request)

        self.assertEqual([knowledge_base], missing_sender_request.func_tool.tools)
        self.assertIs(private_original, private_request.func_tool)
        self.assertIsNone(empty_request.func_tool)

    def test_projection_leaves_non_tool_request_fields_unchanged(self) -> None:
        request = self.request(
            [_function_tool("denied")],
            system_prompt="Skill-like text must not be parsed",
            extra_parts=["non-agentic knowledge base injection"],
        )
        original_prompt = request.system_prompt
        original_parts = request.extra_user_content_parts

        self.plugin()._project_group_visible_tools(self.snapshot(), request)

        self.assertEqual(original_prompt, request.system_prompt)
        self.assertIs(original_parts, request.extra_user_content_parts)


class ToolObservationTests(unittest.IsolatedAsyncioTestCase):
    class ForbiddenProviderContext:
        def __init__(self) -> None:
            self.calls = 0

        def get_provider_by_id(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("post-tool observation must not call a Provider")

        async def get_using_provider_async(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("post-tool observation must not call a Provider")

    class Event:
        def __init__(self, extras=None) -> None:
            self.extras = dict(extras or {})
            self.send_calls = 0
            self.result_calls = 0

        def get_extra(self, key, default=None):
            return self.extras.get(key, default)

        def set_extra(self, key, value) -> None:
            self.extras[key] = value

        def send(self, *_args, **_kwargs):
            self.send_calls += 1
            raise AssertionError("post-tool observation must not send")

        def set_result(self, *_args, **_kwargs):
            self.result_calls += 1
            raise AssertionError("post-tool observation must not set a result")

    @staticmethod
    def snapshot():
        return MAIN.create_snapshot(FakeEvent(), origin="direct")

    @classmethod
    def plugin(cls):
        return MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)

    def event_with_live_turn(self):
        lifecycle = MAIN.TurnLifecycle()
        event = self.Event(
            {
                MAIN.SYS001_TURN_EXTRA: self.snapshot(),
                MAIN.SYS001_LIFECYCLE_EXTRA: lifecycle,
            }
        )
        return event, lifecycle

    async def test_hook_uses_strict_is_error_observation_categories(self) -> None:
        cases = [
            (None, "unknown"),
            (SimpleNamespace(isError=True), "reported_error"),
            (SimpleNamespace(isError=False), "result_returned"),
            (SimpleNamespace(isError=1), "unknown"),
            (SimpleNamespace(isError=0), "unknown"),
            (SimpleNamespace(isError="false"), "unknown"),
            (SimpleNamespace(), "unknown"),
        ]
        plugin = self.plugin()

        for result, expected_outcome in cases:
            with self.subTest(result=result, expected_outcome=expected_outcome):
                event, lifecycle = self.event_with_live_turn()
                tool = SimpleNamespace(name="tool-a")
                args = {"sensitive": "only kept in the live event"}

                await plugin.observe_official_tool_result(event, tool, args, result)

                observations = event.get_extra(MAIN.SYS001_TOOL_OBSERVATIONS_EXTRA)
                self.assertEqual(1, len(observations))
                observation = observations[0]
                self.assertEqual(expected_outcome, observation.outcome)
                self.assertFalse(observation.authoritative)
                self.assertIs(tool, observation.raw_tool)
                self.assertIs(args, observation.raw_args)
                self.assertIs(result, observation.raw_result)
                self.assertEqual([observation], lifecycle.tool_observations)

    async def test_hook_preserves_multiple_observations_in_order_and_identity(self) -> None:
        event, lifecycle = self.event_with_live_turn()
        plugin = self.plugin()
        plugin.context = self.ForbiddenProviderContext()
        tool_one = SimpleNamespace(name="first")
        tool_two = SimpleNamespace(name="second")
        args_one = {"one": object()}
        args_two = {"two": object()}
        result_one = SimpleNamespace(isError=False)
        result_two = SimpleNamespace(isError=True)

        await plugin.observe_official_tool_result(event, tool_one, args_one, result_one)
        await plugin.observe_official_tool_result(event, tool_two, args_two, result_two)

        observations = event.get_extra(MAIN.SYS001_TOOL_OBSERVATIONS_EXTRA)
        self.assertEqual(
            ["result_returned", "reported_error"],
            [observation.outcome for observation in observations],
        )
        self.assertIs(tool_one, observations[0].raw_tool)
        self.assertIs(args_one, observations[0].raw_args)
        self.assertIs(result_one, observations[0].raw_result)
        self.assertIs(tool_two, observations[1].raw_tool)
        self.assertIs(args_two, observations[1].raw_args)
        self.assertIs(result_two, observations[1].raw_result)
        self.assertEqual(observations, lifecycle.tool_observations)
        self.assertEqual((0, 0), (event.send_calls, event.result_calls))
        self.assertEqual(0, plugin.context.calls)

    async def test_hook_ignores_events_outside_an_active_shio_turn(self) -> None:
        event = self.Event()

        await self.plugin().observe_official_tool_result(
            event,
            SimpleNamespace(name="unrelated"),
            {"arg": "value"},
            SimpleNamespace(isError=False),
        )

        self.assertNotIn(MAIN.SYS001_TOOL_OBSERVATIONS_EXTRA, event.extras)

    async def test_hook_does_not_log_raw_args_or_result(self) -> None:
        event, _lifecycle = self.event_with_live_turn()
        calls = []
        original_logger = MAIN.logger
        MAIN.logger = SimpleNamespace(
            warning=lambda *args, **kwargs: calls.append((args, kwargs)),
            info=lambda *args, **kwargs: calls.append((args, kwargs)),
            error=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        try:
            await self.plugin().observe_official_tool_result(
                event,
                SimpleNamespace(name="tool"),
                {"secret": "arg"},
                SimpleNamespace(isError=False, content="secret result"),
            )
        finally:
            MAIN.logger = original_logger

        self.assertEqual([], calls)


class FinalAgentObservationTests(unittest.IsolatedAsyncioTestCase):
    class ForbiddenProviderContext:
        def __init__(self) -> None:
            self.calls = 0

        def __getattr__(self, _name):
            self.calls += 1
            raise AssertionError("final response observation must not call a Provider")

    @staticmethod
    def event_with_live_turn(origin="direct"):
        lifecycle = MAIN.TurnLifecycle()
        event = ToolObservationTests.Event(
            {
                MAIN.SYS001_TURN_EXTRA: MAIN.create_snapshot(
                    FakeEvent(), origin=origin
                ),
                MAIN.SYS001_LIFECYCLE_EXTRA: lifecycle,
            }
        )
        return event, lifecycle

    @staticmethod
    def response(role="assistant", completion_text="", reasoning_content=""):
        return SimpleNamespace(
            role=role,
            completion_text=completion_text,
            reasoning_content=reasoning_content,
        )

    @staticmethod
    def plugin():
        instance = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        instance.context = FinalAgentObservationTests.ForbiddenProviderContext()
        return instance

    async def test_hook_observes_only_the_final_response_categories(self) -> None:
        cases = [
            (self.response("err", "visible error text"), "final_error"),
            (self.response("assistant", "visible text"), "final_text"),
            (self.response("assistant", "", "reasoning"), "reasoning_only"),
            (self.response("assistant", ""), "empty"),
            (self.response("tool", ""), "unknown"),
        ]

        for response, expected_outcome in cases:
            with self.subTest(expected_outcome=expected_outcome):
                event, lifecycle = self.event_with_live_turn()
                plugin = self.plugin()
                original_text = response.completion_text

                await plugin.observe_final_agent_response(event, response)

                observation = event.get_extra(
                    MAIN.SYS001_FINAL_AGENT_OBSERVATION_EXTRA
                )
                self.assertEqual(expected_outcome, observation.outcome)
                self.assertTrue(observation.from_final_agent_hook)
                self.assertIs(response, observation.raw_response)
                self.assertEqual(original_text, response.completion_text)
                if expected_outcome == "final_text":
                    self.assertEqual("captured", lifecycle.state)
                else:
                    self.assertEqual(("failed", expected_outcome), (lifecycle.state, lifecycle.terminal_reason))
                self.assertEqual((0, 0, 0), (event.send_calls, event.result_calls, plugin.context.calls))

    async def test_main_agent_marker_like_text_stays_visible_for_natural_turns(self) -> None:
        for text in (" WAIT ", "NO_ACTION", "WAIT please", "`WAIT`", "NO_ACTION?", "assistant: hi"):
            with self.subTest(text=text):
                event, lifecycle = self.event_with_live_turn(origin="natural")
                response = self.response(completion_text=text)

                await self.plugin().observe_final_agent_response(event, response)

                self.assertEqual(text, response.completion_text)
                self.assertEqual("captured", lifecycle.state)

    async def test_direct_assistant_prefix_is_not_rewritten_and_duplicate_hook_keeps_first_observation(self) -> None:
        event, lifecycle = self.event_with_live_turn()
        first = self.response(completion_text="assistant: hi")
        duplicate = self.response(role="err", completion_text="later error")
        plugin = self.plugin()

        await plugin.observe_final_agent_response(event, first)
        first_observation = event.get_extra(MAIN.SYS001_FINAL_AGENT_OBSERVATION_EXTRA)
        await plugin.observe_final_agent_response(event, duplicate)

        self.assertEqual("assistant: hi", first.completion_text)
        self.assertIs(first_observation, event.get_extra(MAIN.SYS001_FINAL_AGENT_OBSERVATION_EXTRA))
        self.assertEqual("captured", lifecycle.state)
        self.assertEqual("later error", duplicate.completion_text)

    def test_schema_no_longer_exposes_unimplemented_audit_modes(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        items = json.loads(schema_path.read_text(encoding="utf-8"))["sys001"]["items"]
        self.assertNotIn("audit", items)

    async def test_final_review_uses_official_provider_and_can_replace_only_same_response(self) -> None:
        provider = FakeProvider(
            '{"action":"replace","text":"repaired"}', '{"action":"keep"}'
        )
        event, lifecycle = self.event_with_live_turn()
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.context = FakeContext(current=provider)
        plugin.config = {"sys001": {"final_review": {
            "mode": "core",
            "repair_enabled": True,
            "use_repaired_text": True,
            "timeout_seconds": 1,
            "max_repair_attempts": 1,
        }}}
        event.unified_msg_origin = "qq:group:202"
        response = self.response(completion_text="original")

        await plugin.observe_final_agent_response(event, response)

        self.assertEqual("repaired", response.completion_text)
        self.assertEqual(2, len(provider.calls))
        self.assertIsNone(provider.calls[0]["func_tool"])
        self.assertEqual(
            [{"outcome": "replace"}, {"outcome": "keep"}],
            event.get_extra("shio.sys001.final_review_attempts"),
        )
        self.assertEqual("captured", lifecycle.state)

    async def test_final_review_exhaustion_blocks_default_last_reply_and_is_reachable(self) -> None:
        for provider_text, repair_enabled, use_repaired_text in (
            ("not json", True, True),
            ('{"action":"replace","text":"repaired"}', False, True),
            ('{"action":"replace","text":"repaired"}', True, False),
        ):
            with self.subTest(provider_text=provider_text, repair_enabled=repair_enabled, use_repaired_text=use_repaired_text):
                provider = FakeProvider(provider_text)
                event, lifecycle = self.event_with_live_turn()
                plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
                plugin.context = FakeContext(current=provider)
                plugin.config = {"sys001": {"final_review": {
                    "mode": "core",
                    "repair_enabled": repair_enabled,
                    "use_repaired_text": use_repaired_text,
                    "timeout_seconds": 1,
                    "max_repair_attempts": 1,
                    "send_last_reply_on_review_exhausted": False,
                }}}
                event.unified_msg_origin = "qq:group:202"
                response = self.response(completion_text="original")

                await plugin.observe_final_agent_response(event, response)

                self.assertEqual("", response.completion_text)
                self.assertEqual("review_exhausted", lifecycle.terminal_reason)
                self.assertEqual("review_exhausted", event.get_extra(MAIN.SYS001_FINAL_AGENT_OBSERVATION_EXTRA).outcome)
                self.assertEqual(1, len(provider.calls))

    async def test_review_modes_recheck_repair_limit_and_explicit_last_reply_switch(self) -> None:
        for mode, expected_rule in (
            ("core", "custom core rule"),
            ("additional", "extra rule"),
            ("combined", "custom core rule\n\nextra rule"),
        ):
            with self.subTest(mode=mode):
                provider = FakeProvider('{"action":"keep"}')
                event, lifecycle = self.event_with_live_turn()
                plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
                plugin.context = FakeContext(current=provider)
                plugin.config = {"sys001": {"final_review": {
                    "mode": mode, "core_prompt": "custom core rule", "additional_prompt": "extra rule",
                    "repair_enabled": True, "use_repaired_text": True,
                    "timeout_seconds": 1, "max_repair_attempts": 0,
                }}}
                event.unified_msg_origin = "qq:group:202"
                response = self.response(completion_text="original")
                await plugin.observe_final_agent_response(event, response)
                self.assertEqual("original", response.completion_text)
                self.assertEqual("captured", lifecycle.state)
                self.assertIn(expected_rule, provider.calls[0]["prompt"])

        provider = FakeProvider('{"action":"replace","text":"last"}')
        event, lifecycle = self.event_with_live_turn()
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.context = FakeContext(current=provider)
        plugin.config = {"sys001": {"final_review": {
            "mode": "core", "repair_enabled": True, "use_repaired_text": True,
            "timeout_seconds": 1, "max_repair_attempts": 0,
            "send_last_reply_on_review_exhausted": True,
        }}}
        event.unified_msg_origin = "qq:group:202"
        response = self.response(completion_text="original")
        await plugin.observe_final_agent_response(event, response)
        self.assertEqual("original", response.completion_text)
        self.assertEqual("captured", lifecycle.state)
        self.assertEqual("final_text", event.get_extra(MAIN.SYS001_FINAL_AGENT_OBSERVATION_EXTRA).outcome)

    async def test_review_exhausted_reaches_master_alert_but_explicit_last_reply_does_not(self) -> None:
        class Context(FakeContext):
            def __init__(self, provider):
                super().__init__(current=provider)
                self.kv = {}
                self.sent = []

            async def get_kv_data(self, key, default=None):
                return self.kv.get(key, default)

            async def put_kv_data(self, key, value):
                self.kv[key] = value

            async def send_message(self, umo, chain):
                self.sent.append((umo, chain))
                return True

        for send_last, expected_sends in ((False, 1), (True, 0)):
            with self.subTest(send_last=send_last):
                context = Context(FakeProvider("not json"))
                event, lifecycle = self.event_with_live_turn(origin="direct")
                event.unified_msg_origin = "qq:group:202"
                plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
                plugin.context = context
                plugin.get_kv_data = context.get_kv_data
                plugin.put_kv_data = context.put_kv_data
                plugin.config = {"sys001": {
                    "final_review": {
                        "mode": "core", "timeout_seconds": 1,
                        "max_repair_attempts": 1,
                        "send_last_reply_on_review_exhausted": send_last,
                    },
                    "master_alert": {
                        "master_alert_enabled": True,
                        "review_repair_exhausted_enabled": True,
                        "consecutive_threshold": 1,
                        "window_minutes": 10,
                    },
                }}
                plugin._master_alert_record = SYS001.MasterAlertRecord(
                    master_umo="qq:person:bound"
                )
                plugin._master_alert_binding = {"umo":"qq:person:bound", "sender_id":"202", "platform_id":"qq", "account_id":"999"}
                context.get_config = lambda **kwargs: {"admins_id":["202"]}
                plugin._master_alert_ready = True
                plugin._master_alert_terminated = False
                response = self.response(completion_text="original")

                await plugin.observe_final_agent_response(event, response)

                self.assertEqual(expected_sends, len(context.sent))
                self.assertEqual(
                    "review_exhausted" if not send_last else "captured",
                    lifecycle.terminal_reason if not send_last else lifecycle.state,
                )


class MasterAlertExternalContractTests(unittest.IsolatedAsyncioTestCase):
    class Context:
        def __init__(self):
            self.kv = {}
            self.sent = []
            self.get_calls = 0
            self.put_calls = 0
            self.delete_calls = 0
            self.fail_put_calls = set()

        async def get_kv_data(self, key, default):
            self.get_calls += int(key != "master_alert_destination_v1")
            return self.kv.get(key, default)

        async def put_kv_data(self, key, value):
            self.put_calls += int(key != "master_alert_destination_v1")
            if key != "master_alert_destination_v1" and self.put_calls in self.fail_put_calls:
                raise RuntimeError("KV write unavailable")
            self.kv[key] = value

        async def delete_kv_data(self, key):
            self.delete_calls += 1
            self.kv.pop(key, None)

        async def send_message(self, session, message_chain):
            self.sent.append((session, message_chain))
            return True

        def get_config(self, **kwargs):
            return {"admins_id": ["202"]}

    async def test_only_a_real_private_official_master_event_binds_its_existing_umo(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        context = self.Context()
        plugin.context = context
        plugin.get_kv_data = context.get_kv_data
        plugin.put_kv_data = context.put_kv_data
        plugin.delete_kv_data = context.delete_kv_data
        plugin.config = {"sys001": {"master_alert": {"master_alert_enabled": True}}}
        private_master = FakeEvent("bind")
        private_master.unified_msg_origin = "qq:person:real-admin-session"
        private_master.is_private_chat = lambda: True
        private_master.is_admin = lambda: True
        snapshot = MAIN.create_snapshot(private_master, origin="private")

        self.assertTrue(await plugin.capture_master_alert_binding(private_master, snapshot))
        self.assertEqual("qq:person:real-admin-session", plugin._master_alert_record.master_umo)
        self.assertEqual([], context.sent)

    def alert_plugin(self, context):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.context = context
        plugin.get_kv_data = context.get_kv_data
        plugin.put_kv_data = context.put_kv_data
        plugin.delete_kv_data = context.delete_kv_data
        plugin._master_alert_record = SYS001.MasterAlertRecord(master_umo="qq:person:bound")
        plugin._master_alert_binding = {"umo":"qq:person:bound", "sender_id":"202", "platform_id":"qq", "account_id":"999"}
        plugin._master_alert_ready = True
        plugin._master_alert_terminated = False
        plugin.config = {"sys001": {"master_alert": {
            "master_alert_enabled": True,
            "main_reply_exhausted_enabled": True,
            "consecutive_threshold": 2,
            "window_minutes": 10,
        }}}
        return plugin

    def assert_master_kv_unused(self, context):
        self.assertEqual((0, 0, 0), (
            context.get_calls, context.put_calls, context.delete_calls,
        ))

    async def test_threshold_submits_one_fixed_plain_to_the_bound_umo_and_failure_never_retries(self):
        context = self.Context()
        plugin = self.alert_plugin(context)
        snapshot = MAIN.create_snapshot(FakeEvent(), origin="direct")
        event = ToolObservationTests.Event()

        await plugin._record_master_alert_terminal(event, snapshot, success=False, terminal_reason="final_error")
        self.assertEqual(1, len(context.sent))
        await plugin._record_master_alert_terminal(event, snapshot, success=False, terminal_reason="final_error")
        await plugin._record_master_alert_terminal(event, snapshot, success=False, terminal_reason="final_error")

        self.assertEqual(1, len(context.sent))
        session, chain = context.sent[0]
        self.assertEqual("qq:person:bound", session)
        self.assertIn("主回复模型调用失败", chain.chain[0].text)
        self.assertEqual("submitted", plugin._master_alert_record.report_status)

    async def test_missing_binding_stays_pending_and_send_exception_marks_failed_once(self):
        context = self.Context()
        plugin = self.alert_plugin(context)
        plugin._master_alert_record = SYS001.MasterAlertRecord()
        snapshot = MAIN.create_snapshot(FakeEvent(), origin="direct")
        await plugin._record_master_alert_terminal(ToolObservationTests.Event(), snapshot, success=False, terminal_reason="final_error")
        await plugin._record_master_alert_terminal(ToolObservationTests.Event(), snapshot, success=False, terminal_reason="final_error")
        self.assertEqual(("pending", []), (plugin._master_alert_record.report_status, context.sent))

    async def test_late_timer_submits_only_current_pending_report_and_terminate_cancels_it(self):
        context = self.Context()
        plugin = self.alert_plugin(context)
        plugin._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:bound", report_id="r", report_status="pending",
            quiet_deadline=time.time() - 1,
        )
        await plugin._master_alert_timer_worker("r", plugin._master_alert_record.quiet_deadline)
        self.assertEqual(1, len(context.sent))
        await plugin._master_alert_timer_worker("old", time.time() - 1)
        self.assertEqual(1, len(context.sent))
        plugin._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:bound", report_id="later", report_status="pending",
            quiet_deadline=time.time() + 60,
        )
        plugin._schedule_alert_timer_locked("later", plugin._master_alert_record.quiet_deadline)
        await plugin.terminate()
        self.assertIsNone(plugin._master_alert_timer)

    async def test_official_send_failure_or_unsupported_is_persisted_once_without_retry(self):
        for outcome in (False, RuntimeError("unsupported")):
            with self.subTest(outcome=outcome):
                context = self.Context()
                async def send_message(_session, _chain):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                context.send_message = send_message
                plugin = self.alert_plugin(context)
                plugin._master_alert_record = SYS001.MasterAlertRecord(
                    master_umo="qq:person:bound", report_id="r", report_status="pending"
                )
                await plugin._submit_master_alert_report("r")
                await plugin._submit_master_alert_report("r")
                self.assertEqual("failed", plugin._master_alert_record.report_status)

    async def test_master_kv_faults_do_not_block_current_instance_send(self):
        context = self.Context()
        context.fail_put_calls = {1}
        plugin = self.alert_plugin(context)
        plugin._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:bound", report_id="r", report_status="pending"
        )

        await plugin._submit_master_alert_report("r")

        self.assertEqual(1, len(context.sent))
        self.assertTrue(plugin._master_alert_ready)
        self.assertEqual("submitted", plugin._master_alert_record.report_status)
        self.assert_master_kv_unused(context)

    async def test_new_instance_ignores_legacy_submitting_marker(self):
        shared = self.Context()
        shared.kv["master_alert_state_v1"] = {"report_status": "submitting"}

        second = self.alert_plugin(shared)
        self._prepare_for_initialize(second)
        await second.initialize()

        self.assertEqual([], shared.sent)
        self.assertEqual(SYS001.MasterAlertRecord(), second._master_alert_record)
        self.assertTrue(second._master_alert_ready)
        self.assertIsNone(getattr(second, "_master_alert_timer", None))
        self.assert_master_kv_unused(shared)

    async def test_send_failure_stays_local_and_new_instance_is_clean(self):
        shared = self.Context()
        async def fail_send(_session, _chain):
            shared.sent.append((_session, _chain))
            return False
        shared.send_message = fail_send
        first = self.alert_plugin(shared)
        first._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:bound", report_id="r", report_status="pending"
        )

        await first._submit_master_alert_report("r")

        self.assertEqual(1, len(shared.sent))
        self.assertTrue(first._master_alert_ready)
        self.assertEqual("failed", first._master_alert_record.report_status)

        second = self.alert_plugin(shared)
        self._prepare_for_initialize(second)
        await second.initialize()
        self.assertEqual(1, len(shared.sent))
        self.assertEqual(SYS001.MasterAlertRecord(), second._master_alert_record)
        self.assert_master_kv_unused(shared)

    async def test_cross_instance_pending_is_dropped_and_never_replayed(self):
        shared = self.Context()
        first = self.alert_plugin(shared)
        first._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:bound", report_id="r", report_status="pending",
            quiet_deadline=time.time() - 1, recovered=True,
        )

        second = self.alert_plugin(shared)
        self._prepare_for_initialize(second)
        await second.initialize()
        await asyncio.sleep(0)
        self.assertEqual([], shared.sent)
        self.assertEqual(SYS001.MasterAlertRecord(), second._master_alert_record)
        self.assertIsNone(getattr(second, "_master_alert_timer", None))
        third = self.alert_plugin(shared)
        self._prepare_for_initialize(third)
        await third.initialize()
        self.assertEqual([], shared.sent)
        self.assertEqual(SYS001.MasterAlertRecord(), third._master_alert_record)
        self.assert_master_kv_unused(shared)

    @staticmethod
    def _prepare_for_initialize(plugin):
        plugin._natural_generations = {}
        plugin._natural_cadence = {}
        plugin._natural_bindings = {}
        plugin._natural_ready = {}
        plugin._natural_locks = {}
        plugin._continuous_scopes = {}
        plugin._continuous_locks = {}
        plugin._natural_terminated = False
        plugin._continuous_terminated = False
        plugin._initialized = False
        plugin._initialize_lock = asyncio.Lock()


class MasterAlertStateTests(unittest.TestCase):
    def test_fixed_plus_eight_quiet_handles_cross_midnight_and_boundaries(self):
        at_2259 = datetime(2026, 1, 1, 14, 59, tzinfo=UTC).timestamp()
        at_2300 = datetime(2026, 1, 1, 15, 0, tzinfo=UTC).timestamp()
        at_0800 = datetime(2026, 1, 2, 0, 0, tzinfo=UTC).timestamp()
        self.assertIsNone(SYS001.master_alert_quiet_deadline(now=at_2259, start="23:00", end="08:00"))
        self.assertEqual(at_0800, SYS001.master_alert_quiet_deadline(now=at_2300, start="23:00", end="08:00"))
        self.assertIsNone(SYS001.master_alert_quiet_deadline(now=at_0800, start="23:00", end="08:00"))
        at_0759 = datetime(2026, 1, 1, 23, 59, tzinfo=UTC).timestamp()
        self.assertEqual(at_0800, SYS001.master_alert_quiet_deadline(now=at_0759, start="23:00", end="08:00"))
        daytime = datetime(2026, 1, 1, 3, 0, tzinfo=UTC).timestamp()
        self.assertEqual(
            datetime(2026, 1, 1, 4, 0, tzinfo=UTC).timestamp(),
            SYS001.master_alert_quiet_deadline(now=daytime, start="10:00", end="12:00"),
        )

    def test_strict_record_decode_and_encode_rejects_bad_kv(self):
        record = SYS001.MasterAlertRecord(master_umo="qq:person:bound")
        encoded = SYS001.encode_master_alert_record(record)
        self.assertEqual(record, SYS001.decode_master_alert_record(encoded, now=100.0))
        for bad in (
            {}, {**encoded, "unknown": 1}, {**encoded, "consecutive_count": True},
            {**encoded, "window_started_at": float("nan")},
            {**encoded, "window_started_at": -1}, {**encoded, "version": 2},
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(SYS001.decode_master_alert_record(bad, now=100.0))

    def test_same_type_threshold_once_different_type_and_expired_window_reset(self):
        empty = SYS001.MasterAlertRecord(master_umo="qq:person:bound")
        first = SYS001.master_alert_failure(empty, error_type="final_error", now=10, window_seconds=60, threshold=2)
        reached = SYS001.master_alert_failure(first, error_type="final_error", now=20, window_seconds=60, threshold=2)
        again = SYS001.master_alert_failure(reached, error_type="final_error", now=30, window_seconds=60, threshold=2)
        changed = SYS001.master_alert_failure(again, error_type="review_exhausted", now=31, window_seconds=60, threshold=2)
        expired = SYS001.master_alert_failure(again, error_type="final_error", now=100, window_seconds=60, threshold=2)
        self.assertEqual((1, ""), (first.consecutive_count, first.report_id))
        self.assertTrue(reached.report_id)
        self.assertEqual(reached.report_id, again.report_id)
        self.assertEqual((1, ""), (changed.consecutive_count, changed.report_id))
        self.assertEqual((1, ""), (expired.consecutive_count, expired.report_id))

    def test_success_keeps_pending_summary_and_exclusions_do_not_count(self):
        pending = SYS001.MasterAlertRecord(master_umo="qq:person:bound", error_type="final_error", consecutive_count=3, window_started_at=1, report_id="r", report_status="pending")
        recovered = SYS001.master_alert_success(pending)
        self.assertEqual(("r", "pending", True, 3), (recovered.report_id, recovered.report_status, recovered.recovered, recovered.consecutive_count))
        self.assertFalse(SYS001.master_alert_counts_failure(enabled=False, type_enabled=True, origin="direct", terminal_reason="final_error"))
        self.assertFalse(SYS001.master_alert_counts_failure(enabled=True, type_enabled=False, origin="direct", terminal_reason="final_error"))
        self.assertTrue(SYS001.master_alert_counts_failure(enabled=True, type_enabled=True, origin="natural", terminal_reason="final_error"))
        for origin, reason in (("direct", "wait"), ("direct", "no_action"), ("pending", "final_error")):
            self.assertFalse(SYS001.master_alert_counts_failure(enabled=True, type_enabled=True, origin=origin, terminal_reason=reason))


class TextComponentLayoutTests(unittest.IsolatedAsyncioTestCase):
    class Text(MAIN.Plain):
        pass

    class AlternateText:
        def __init__(self, text):
            self.text = text

    class Image:
        pass

    class Event(ToolObservationTests.Event):
        def __init__(self, result, origin="direct"):
            super().__init__(
                {
                    MAIN.SYS001_TURN_EXTRA: MAIN.create_snapshot(
                        FakeEvent(), origin=origin
                    )
                }
            )
            self._result = result

        def get_result(self):
            return self._result

    @staticmethod
    def plugin(mode="single", maximum=3):
        instance = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        instance.config = {
            "sys001": {
                "presentation": {
                    "text_component_mode": mode,
                    "text_component_max_segments": maximum,
                }
            }
        }
        return instance

    async def test_preserve_and_unknown_modes_keep_the_original_chain(self) -> None:
        for mode in ("single", "not-a-mode"):
            with self.subTest(mode=mode):
                text = self.Text("one。two。")
                image = self.Image()
                chain = [text, image]
                event = self.Event(SimpleNamespace(chain=chain))

                await self.plugin(mode).layout_text_components(event)

                self.assertIs(chain, event.get_result().chain)
                self.assertEqual([text, image], event.get_result().chain)
                self.assertEqual((0, 0), (event.send_calls, event.result_calls))

    async def test_sentence_components_replaces_only_one_text_component(self) -> None:
        text = self.Text("一。二。三。")
        image = self.Image()
        event = self.Event(SimpleNamespace(chain=[image, text]))

        await self.plugin("plugin", maximum=2).layout_text_components(event)

        chain = event.get_result().chain
        self.assertIs(image, chain[0])
        self.assertEqual(["一。", "二。三。"], [part.text for part in chain[1:]])
        self.assertEqual("一。二。三。", "".join(part.text for part in chain[1:]))
        self.assertNotIn(text, chain)
        self.assertEqual((0, 0), (event.send_calls, event.result_calls))

    async def test_sentence_components_maximum_one_and_multiple_text_use_global_minimum(self) -> None:
        single = self.Text("一。二。三。")
        event = self.Event(SimpleNamespace(chain=[single]))
        plugin = self.plugin("plugin", maximum=1)

        await plugin.layout_text_components(event)

        self.assertEqual(["一。二。三。"], [part.text for part in event.get_result().chain])
        self.assertIs(single, event.get_result().chain[0])

        left = self.Text("left。right。")
        right = self.Text("other。text。")
        image = self.Image()
        multi_event = self.Event(SimpleNamespace(chain=[left, image, right]))

        multi_plugin = self.plugin("plugin", maximum=3)
        multi_plugin.config["sys001"]["presentation"]["text_component_min_segments"] = 3
        await multi_plugin.layout_text_components(multi_event)

        chain = multi_event.get_result().chain
        self.assertEqual(3, sum(isinstance(part, MAIN.Plain) for part in chain))
        self.assertIs(image, chain[2])
        self.assertEqual("left。right。", "".join(part.text for part in chain[:2]))
        self.assertEqual("other。text。", chain[3].text)

    async def test_single_text_runs_merge_same_type_without_crossing_media_or_repeat_changes(self) -> None:
        first = self.Text("a")
        second = self.Text("b")
        image = self.Image()
        third = self.Text("c")
        fourth = self.Text("d")
        alternate = self.AlternateText("e")
        event = self.Event(
            SimpleNamespace(chain=[first, second, image, third, fourth, alternate])
        )
        plugin = self.plugin("single")

        await plugin.layout_text_components(event)

        chain = event.get_result().chain
        self.assertEqual(["ab", "cd", "e"], [part.text for part in (chain[0], chain[2], chain[3])])
        self.assertIs(image, chain[1])
        self.assertIs(alternate, chain[3])
        self.assertEqual("abcd", chain[0].text + chain[2].text)
        first_layout_ids = [id(part) for part in chain]

        await plugin.layout_text_components(event)

        self.assertEqual(first_layout_ids, [id(part) for part in event.get_result().chain])
        self.assertEqual((0, 0), (event.send_calls, event.result_calls))

    async def test_persona_prefix_is_appended_for_private_direct_and_natural_turns(self) -> None:
        plugin = self.plugin()
        for origin in ("private", "direct", "natural"):
            with self.subTest(origin=origin):
                event = self.Event(SimpleNamespace(chain=[]), origin=origin)
                request = SimpleNamespace(
                    system_prompt="Persona prefix",
                    prompt="original prompt",
                    func_tool=None,
                    extra_user_content_parts=[],
                )

                await plugin.attach_turn_and_project_capabilities(event, request)

                self.assertTrue(request.system_prompt.startswith("Persona prefix\n\n"))
                self.assertIn(f"origin={origin}", request.system_prompt)
                self.assertEqual([], request.extra_user_content_parts)

    def test_schema_exposes_only_the_approved_layout_and_bubble_wait_settings(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        presentation = json.loads(schema_path.read_text(encoding="utf-8"))["sys001"]["items"][
            "presentation"
        ]["items"]

        self.assertEqual(
            {
                "text_component_mode", "text_component_max_segments",
                "text_component_min_segments", "model_segment_provider_id",
                "model_segment_fallback_provider_ids", "model_segment_timeout_seconds",
                "bubble_send_min_wait_seconds", "bubble_send_max_wait_seconds",
            }, set(presentation)
        )
        self.assertNotIn("bubble_enabled", presentation)
        self.assertNotIn("bubble_max_segments", presentation)
        self.assertNotIn("interval", presentation)
        self.assertEqual("float", presentation["bubble_send_min_wait_seconds"]["type"])
        self.assertEqual(0, presentation["bubble_send_min_wait_seconds"]["default"])
        self.assertEqual("float", presentation["bubble_send_max_wait_seconds"]["type"])
        self.assertEqual(0, presentation["bubble_send_max_wait_seconds"]["default"])


class TimestampAndMasterRelationshipTests(unittest.IsolatedAsyncioTestCase):
    def test_snapshot_prefers_platform_timestamp_and_keeps_structured_identity(self) -> None:
        event = FakeEvent()
        event.created_at = 1_700_000_000
        event.message_obj = SimpleNamespace(message_id="official-message", timestamp="1704067201")

        snapshot = MAIN.create_snapshot(event, origin="direct")

        self.assertEqual(datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC), snapshot.created_at)
        self.assertEqual("202", snapshot.sender_id)
        self.assertEqual("synthetic member", snapshot.sender_name)
        self.assertEqual("999", snapshot.self_id)
        self.assertEqual("official-message", snapshot.message_id)

    def test_timestamp_fallbacks_are_safe_and_utc_aware(self) -> None:
        fallback = datetime(2024, 1, 2, tzinfo=UTC)
        cases = [
            (None, 1_704_153_600, fallback),
            ("bad", 1_704_153_600, fallback),
            (float("nan"), 1_704_153_600, fallback),
            (float("inf"), 1_704_153_600, fallback),
            (-1, 1_704_153_600, fallback),
            (10**20, 1_704_153_600, fallback),
            (None, None, datetime(1970, 1, 1, tzinfo=UTC)),
        ]
        for timestamp, created_at, expected in cases:
            with self.subTest(timestamp=timestamp, created_at=created_at):
                actual = SYS001.event_timestamp(timestamp, created_at)
                self.assertEqual(expected, actual)
                self.assertIs(UTC, actual.tzinfo)

    @staticmethod
    def snapshot(is_master=False, sender_id="202"):
        return MAIN.TurnSnapshot(
            platform_id="qq", scope="qq:group:202", account_id="999",
            sender_id=sender_id, sender_name="member", self_id="999",
            message_id="message", created_at=datetime(2024, 1, 1, tzinfo=UTC),
            message_text="hello", is_private=False, is_master=is_master,
            at_self=True, reply_to_self=False, origin="direct",
        )

    async def test_master_relationship_appends_only_for_official_master_without_changing_tools_or_entry(self) -> None:
        tool = _function_tool("admin-tool")
        toolset = MAIN.ToolSet([tool])
        master = self.snapshot(is_master=True)
        event = ToolObservationTests.Event({MAIN.SYS001_TURN_EXTRA: master})
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {"identity": {
            "master_relationship_enabled": True,
            "master_relationship_prompt": "Use a calm, respectful tone.",
        }}}
        request = SimpleNamespace(
            system_prompt="Persona prefix", prompt="hello", func_tool=toolset,
            extra_user_content_parts=[],
        )

        await plugin.attach_turn_and_project_capabilities(event, request)

        self.assertTrue(request.system_prompt.startswith("Persona prefix\n\n"))
        self.assertIn("[Shio Master 表达附加规则]", request.system_prompt)
        self.assertIn("Use a calm, respectful tone.", request.system_prompt)
        self.assertIs(toolset, request.func_tool)
        self.assertTrue(master.is_master)
        self.assertTrue(
            MAIN.admit_ingress(
                master,
                private_allowed_sender_ids=frozenset(), blocked_sender_ids=frozenset({"202"}),
                group_allowed_scopes=frozenset(), group_blocked_scopes=frozenset({"qq:group:202"}),
            ).allowed
        )

    async def test_relationship_prompt_is_absent_when_disabled_empty_or_not_master(self) -> None:
        for is_master, enabled, prompt in (
            (True, False, "rule"), (True, True, "  "), (False, True, "rule"),
        ):
            with self.subTest(is_master=is_master, enabled=enabled, prompt=prompt):
                event = ToolObservationTests.Event(
                    {MAIN.SYS001_TURN_EXTRA: self.snapshot(is_master=is_master)}
                )
                plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
                plugin.config = {"sys001": {"identity": {
                    "master_relationship_enabled": enabled,
                    "master_relationship_prompt": prompt,
                }}}
                request = SimpleNamespace(
                    system_prompt="Persona", prompt="hello", func_tool=None,
                    extra_user_content_parts=[],
                )

                await plugin.attach_turn_and_project_capabilities(event, request)

                self.assertNotIn("[Shio Master 表达附加规则]", request.system_prompt)

    def test_schema_exposes_only_expression_scoped_master_relationship_settings(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        identity = json.loads(schema_path.read_text(encoding="utf-8"))["sys001"]["items"][
            "identity"
        ]["items"]
        self.assertEqual(
            {"master_relationship_enabled", "master_relationship_prompt"}, set(identity)
        )


class NaturalParticipationPromptTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def snapshot(origin):
        return MAIN.TurnSnapshot(
            platform_id="qq", scope="qq:group:202", account_id="999", sender_id="202",
            sender_name="member", self_id="999", message_id="m",
            created_at=datetime(2024, 1, 1, tzinfo=UTC), message_text="group text",
            is_private=False, is_master=False, at_self=False, reply_to_self=False, origin=origin,
        )

    @staticmethod
    def plugin():
        instance = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        instance.config = {"sys001": {"group": {
            "natural_participation_enabled": True,
            "natural_group_scopes": ["qq:group:202"],
            "natural_participation_prompt": "CUSTOM WAIT OR NO_ACTION",
        }}}
        return instance

    async def test_natural_main_request_preserves_media_fields_without_silence_protocol(self) -> None:
        request = SimpleNamespace(
            prompt="group text", system_prompt="Persona", func_tool=object(),
            image_urls=[object()], audio_urls=[object()], file_urls=[object()], reply=object(),
        )
        event = ToolObservationTests.Event({MAIN.SYS001_TURN_EXTRA: self.snapshot("natural")})

        await self.plugin().attach_turn_and_project_capabilities(event, request)

        self.assertEqual("group text", request.prompt)
        self.assertNotIn("CUSTOM WAIT OR NO_ACTION", request.prompt)
        self.assertEqual("Persona", request.system_prompt.split("\n\n")[0])
        self.assertEqual(1, len(request.image_urls))
        self.assertEqual(1, len(request.audio_urls))
        self.assertEqual(1, len(request.file_urls))
        self.assertIsNotNone(request.reply)

    async def test_direct_and_private_do_not_get_natural_prompt(self) -> None:
        for origin in ("direct", "private"):
            with self.subTest(origin=origin):
                request = SimpleNamespace(prompt="text", system_prompt="Persona", func_tool=None)
                event = ToolObservationTests.Event({MAIN.SYS001_TURN_EXTRA: self.snapshot(origin)})
                await self.plugin().attach_turn_and_project_capabilities(event, request)
                self.assertEqual("text", request.prompt)

    def test_schema_has_no_cooldown_or_deferred_source_settings(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        group = json.loads(schema_path.read_text(encoding="utf-8"))["sys001"]["items"]["group"]["items"]
        self.assertIn("natural_participation_prompt", group)
        for retired in ("natural_cooldown_seconds", "natural_count", "proactive", "private_care"):
            self.assertNotIn(retired, group)
        self.assertFalse(hasattr(self.plugin(), "_natural_last_seen"))


class NaturalCadencePluginTests(unittest.IsolatedAsyncioTestCase):
    """Actual hook/KV wiring tests; they do not model AstrBot delivery success."""

    scope = "qq:group:202"

    @staticmethod
    def snapshot(origin="natural", message_id="natural-m"):
        return MAIN.TurnSnapshot(
            platform_id="qq", scope=NaturalCadencePluginTests.scope,
            account_id="999", sender_id="202", sender_name="member", self_id="999",
            message_id=message_id, created_at=datetime(2024, 1, 1, tzinfo=UTC),
            message_text="group text", is_private=False, is_master=False,
            at_self=False, reply_to_self=False, origin=origin,
        )

    @staticmethod
    def response(text, role="assistant"):
        return SimpleNamespace(role=role, completion_text=text, reasoning_content="")

    def plugin(self, store=None, *, write_error=False):
        store = {} if store is None else store
        instance = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        instance.config = {"sys001": {"group": {
            "natural_participation_enabled": True,
            "natural_group_scopes": [self.scope],
            "natural_reply_cooldown_seconds": 45,
            "natural_frequency_window_minutes": 5,
            "natural_max_replies_per_window": 2,
            "natural_no_action_backoff_base_seconds": 2,
            "natural_no_action_backoff_max_seconds": 30,
        }}}
        instance._natural_generations = {self.scope: 1}
        instance._natural_cadence = {self.scope: MAIN.NaturalCadence.empty()}
        instance._natural_bindings = {}
        instance._natural_ready = {self.scope: True}
        instance._natural_locks = {}

        async def put(key, value):
            if write_error:
                raise RuntimeError("KV unavailable")
            store[key] = value

        async def get(key, default=None):
            return store.get(key, default)

        instance.put_kv_data = put
        instance.get_kv_data = get
        return instance, store

    def event(self, snapshot=None, generation=1):
        snapshot = snapshot or self.snapshot()
        lifecycle = MAIN.TurnLifecycle()
        return ToolObservationTests.Event({
            MAIN.SYS001_TURN_EXTRA: snapshot,
            MAIN.SYS001_LIFECYCLE_EXTRA: lifecycle,
            "shio.sys001.generation": generation,
        }), lifecycle

    async def test_main_agent_marker_text_does_not_write_natural_cadence(self):
        plugin, store = self.plugin()
        wait_event, wait_lifecycle = self.event()
        await plugin.observe_final_agent_response(wait_event, self.response("WAIT"))
        self.assertEqual("", wait_lifecycle.terminal_reason)
        self.assertEqual({}, store)

        no_action_event, no_action_lifecycle = self.event()
        await plugin.observe_final_agent_response(
            no_action_event, self.response("NO_ACTION")
        )
        self.assertEqual("", no_action_lifecycle.terminal_reason)
        self.assertEqual({}, store)

    async def test_reply_becomes_pending_then_after_hook_commits_once(self):
        plugin, store = self.plugin()
        event, lifecycle = self.event()
        await plugin.observe_final_agent_response(event, self.response("a normal reply"))
        pending = event.get_extra("shio.sys001.natural_reply_pending")
        self.assertEqual("a normal reply", pending["final_text"])
        self.assertEqual((1, "natural-m"), (pending["generation"], pending["message_id"]))
        self.assertEqual({}, store)

        await plugin.record_standard_send(event)
        first = dict(store[plugin._natural_kv_key(self.scope)])
        self.assertTrue(lifecycle.respond_stage_completed)
        self.assertTrue(event.get_extra("shio.sys001.natural_stage_committed"))
        self.assertEqual("committed", first["status"])
        self.assertRegex(first["transaction_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(1, len(first["completed_at"]))
        self.assertNotIn("delivery", first)

        await plugin.record_standard_send(event)
        self.assertEqual(first, store[plugin._natural_kv_key(self.scope)])

    async def test_stale_non_natural_and_failed_kv_never_commit_reply_state(self):
        plugin, store = self.plugin()
        stale_event, _ = self.event()
        await plugin.observe_final_agent_response(stale_event, self.response("reply"))
        plugin._natural_generations[self.scope] = 2
        await plugin.record_standard_send(stale_event)
        self.assertEqual({}, store)

        direct_event, _ = self.event(self.snapshot(origin="direct"))
        await plugin.observe_final_agent_response(direct_event, self.response("reply"))
        await plugin.record_standard_send(direct_event)
        self.assertEqual({}, store)

        failing, failed_store = self.plugin(write_error=True)
        failed_event, _ = self.event()
        await failing.observe_final_agent_response(failed_event, self.response("reply"))
        await failing.record_standard_send(failed_event)
        self.assertEqual({}, failed_store)
        self.assertFalse(failing._natural_ready[self.scope])
        self.assertFalse(failed_event.get_extra("shio.sys001.natural_stage_committed", False))

    async def test_direct_snapshot_does_not_invalidate_pending_natural_after_send(self):
        plugin, store = self.plugin()
        natural, _ = self.event()
        await plugin.observe_final_agent_response(natural, self.response("reply"))
        self.assertEqual(1, natural.get_extra("shio.sys001.generation"))

        class DirectEvent(FakeEvent):
            def __init__(self):
                super().__init__("direct")
                self.extras = {}
                self.unified_msg_origin = NaturalCadencePluginTests.scope
                self.message_obj = SimpleNamespace(message_id="direct-m")

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        direct_source = DirectEvent()
        direct = plugin._snapshot(direct_source, "direct")
        self.assertEqual("direct", direct.origin)
        self.assertIsNone(direct_source.get_extra("shio.sys001.generation"))
        self.assertEqual(1, plugin._natural_generations[self.scope])

        await plugin.record_standard_send(natural)
        self.assertTrue(natural.get_extra("shio.sys001.natural_stage_committed"))
        self.assertIn(plugin._natural_kv_key(self.scope), store)

    async def test_terminated_plugin_instance_cannot_commit_a_late_after_hook(self):
        plugin, store = self.plugin()
        event, _ = self.event()
        await plugin.observe_final_agent_response(event, self.response("reply"))
        await plugin.terminate()
        self.assertTrue(plugin._natural_terminated)
        await plugin.record_standard_send(event)
        self.assertEqual({}, store)

    async def test_initialize_restores_a_valid_scope_and_fails_closed_on_damage(self):
        seed, store = self.plugin()
        record = MAIN.encode_natural_cadence_record(MAIN.NaturalCadenceRecord(
            self.scope, MAIN.NaturalCadence(1, 0.0, (), 0, 0.0),
            "0123456789abcdef0123456789abcdef", "committed",
        ))
        store[seed._natural_kv_key(self.scope)] = record
        restored, _ = self.plugin(store)
        restored._natural_ready = {}
        restored._natural_cadence = {}
        self.assertTrue(await restored._ensure_natural_scope_ready(self.scope))
        self.assertTrue(restored._natural_ready[self.scope])
        self.assertEqual(1, restored._natural_generations[self.scope])

        store[seed._natural_kv_key(self.scope)] = {"damaged": True}
        damaged, _ = self.plugin(store)
        damaged._natural_ready = {}
        damaged._natural_cadence = {}
        self.assertFalse(await damaged._ensure_natural_scope_ready(self.scope))
        self.assertFalse(damaged._natural_ready[self.scope])
        self.assertFalse(await damaged._natural_gate_allows(self.scope))

    async def test_initialize_fails_closed_on_durable_pending_but_restores_committed(self):
        seed, store = self.plugin()
        pending = MAIN.encode_natural_cadence_record(MAIN.NaturalCadenceRecord(
            self.scope, MAIN.NaturalCadence(1, 0.0, (), 1, 2.0),
            "0123456789abcdef0123456789abcdef", "pending",
        ))
        store[seed._natural_kv_key(self.scope)] = pending
        reloaded, _ = self.plugin(store)
        reloaded._natural_ready = {}
        self.assertFalse(await reloaded._ensure_natural_scope_ready(self.scope))
        self.assertFalse(reloaded._natural_ready[self.scope])

        store[seed._natural_kv_key(self.scope)] = {
            **pending, "status": "committed",
        }
        reloaded, _ = self.plugin(store)
        reloaded._natural_ready = {}
        self.assertTrue(await reloaded._ensure_natural_scope_ready(self.scope))
        self.assertTrue(reloaded._natural_ready[self.scope])

    async def test_cooling_or_unavailable_scope_stops_before_any_official_request(self):
        class Inbound(FakeEvent):
            def __init__(self):
                super().__init__("ordinary group text")
                self.extras = {}
                self.is_at_or_wake_command = False

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

            def should_call_llm(self, value):
                self.call_llm = value

        plugin, _store = self.plugin()
        plugin.config["sys001"]["ingress"] = {
            "group_allowed_scopes": [self.scope],
        }
        plugin.context = FakeContext(current=FakeProvider("unused"))
        plugin._natural_cadence[self.scope] = MAIN.NaturalCadence(
            1, time.time(), (), 0, 0.0
        )

        event = Inbound()
        yielded = [request async for request in plugin.request_event_bound_reply(event)]

        self.assertEqual([], yielded)
        self.assertNotIn(MAIN.SYS001_TURN_EXTRA, event.extras)
        self.assertEqual(0, plugin.context.current_calls)


class OfficialGroupHistoryProjectionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def record(
        row_id,
        created_at,
        sender_id,
        sender_name,
        role,
        message,
    ):
        return SimpleNamespace(
            id=row_id,
            created_at=created_at,
            sender_id=sender_id,
            sender_name=sender_name,
            content={"type": role, "message": message},
        )

    @staticmethod
    def snapshot():
        return MAIN.TurnSnapshot(
            platform_id="qq", scope="qq:group:202", account_id="999", sender_id="202",
            sender_name="current", self_id="999", message_id="current-m",
            created_at=datetime(2024, 1, 1, tzinfo=UTC), message_text="current text",
            is_private=False, is_master=False, at_self=False, reply_to_self=False,
            origin="direct",
        )

    def test_only_official_rows_with_real_sender_are_projected_and_blocked_rows_vanish(self):
        records = [
            self.record(3, datetime(2024, 1, 1, tzinfo=UTC), "202", "normal", "user", [
                {"type": "plain", "text": "hello"},
                {"type": "at", "user_id": "999", "name": "bot"},
                {"type": "reply", "message_id": "r-1", "sender_name": "must-not-project", "text": "quoted"},
            ]),
            self.record(2, datetime(2024, 1, 1, tzinfo=UTC), "666", "blocked", "user", [
                {"type": "plain", "text": "secret"},
            ]),
            self.record(4, datetime(2024, 1, 2, tzinfo=UTC), "bot", "bot", "bot", [
                {"type": "plain", "text": "answer"},
            ]),
            self.record(None, datetime(2024, 1, 2, tzinfo=UTC), "303", "bad", "user", [
                {"type": "plain", "text": "missing row id"},
            ]),
        ]

        contexts = SYS001.project_official_group_history(
            records, blocked_sender_ids=frozenset({"666"})
        )

        self.assertEqual(["user", "assistant"], [item["role"] for item in contexts])
        user = json.loads(contexts[0]["content"])
        assistant = json.loads(contexts[1]["content"])
        self.assertEqual((3, "202", "normal"), (user["history_row_id"], user["sender_id"], user["sender_name"]))
        self.assertEqual("assistant_self", assistant["actor"])
        self.assertNotIn("platform_message_id", user)
        self.assertNotIn("reply_sender_id", json.dumps(user, ensure_ascii=False))
        self.assertEqual(
            [{"type": "reply", "message_id": "r-1", "text": "quoted"}],
            [part for part in user["parts"] if part["type"] == "reply"],
        )

    def test_current_event_keeps_message_id_time_reply_and_at_facts(self):
        class At:
            qq = "999"

        class Reply:
            id = "reply-m"
            sender_id = "303"
            timestamp = 1_704_067_100.0
            qq = 0
            time = 1_704_067_100

        class Event(FakeEvent):
            message_obj = SimpleNamespace(message_id="current-m", timestamp=1_704_067_200.0)

            def get_messages(self):
                return [At(), Reply()]

        snapshot = MAIN.create_snapshot(Event("current"), origin="direct")
        self.assertEqual(("999",), snapshot.at_targets)
        self.assertEqual(("reply-m", "303"), (snapshot.reply_message_id, snapshot.reply_sender_id))
        self.assertTrue(snapshot.reply_time_utc.endswith("+00:00"))
        context = snapshot.model_context()
        for line in ("platform_message_id=current-m", "at_targets=999", "reply_message_id=reply-m", "reply_sender_id=303"):
            self.assertIn(line, context)

    def test_official_reply_default_qq_zero_is_not_an_at_target_and_keeps_self_distinction(self):
        class ReplyReplay:
            id = "reply-id"
            qq = 0
            time = 1_704_067_100

            def __init__(self, sender_id):
                self.sender_id = sender_id

        self.assertEqual(
            (False, True), SYS001.component_target_ids([ReplyReplay("999")], "999")
        )
        self.assertEqual(
            (False, False), SYS001.component_target_ids([ReplyReplay("303")], "999")
        )
        provenance = SYS001.current_component_provenance([ReplyReplay("999")])
        self.assertEqual((), provenance[0])
        self.assertEqual(("reply-id", "999"), provenance[1:3])
        self.assertTrue(provenance[3].endswith("+00:00"))

    async def test_request_hook_uses_official_history_or_empty_context_without_touching_living_memory_fields(self):
        history = [self.record(1, datetime(2024, 1, 1, tzinfo=UTC), "202", "normal", "user", [
            {"type": "plain", "text": "earlier"},
        ])]

        class Context(FakeContext):
            def __init__(self, enabled=True, manager=True):
                super().__init__()
                self.enabled = enabled
                self.message_history_manager = SimpleNamespace(get=self.get_history) if manager else None
                self.history_calls = 0

            def get_config(self, *, umo):
                assert umo == "qq:group:202"
                return {"provider_ltm_settings": {
                    "group_message_history_enable": self.enabled,
                    "group_icl_enable": False,
                    "group_message_history_max_cnt": 20,
                }}

            async def get_history(self, *, platform_id, user_id, page_size):
                self.history_calls += 1
                assert (platform_id, user_id, page_size) == ("qq", "qq:group:202", 20)
                return history

        snapshot = self.snapshot()
        event = ToolObservationTests.Event({MAIN.SYS001_TURN_EXTRA: snapshot})
        event.unified_msg_origin = snapshot.scope
        event.get_platform_id = lambda: "qq"
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.context = Context()
        plugin.config = {"sys001": {"ingress": {"blocked_sender_ids": ["666"]}}}
        memory = ["LivingMemory-owned object"]
        request = SimpleNamespace(
            prompt="current text", system_prompt="Persona", contexts=[{"role": "user", "content": "unprovenanced conversation"}],
            func_tool=None, extra_user_content_parts=memory,
        )

        await plugin.attach_turn_and_project_capabilities(event, request)

        self.assertEqual(0, plugin.context.history_calls)
        # The official conversation window (req.contexts) is never rewritten;
        # rewriting it is exactly the pollution path Shio must not take.
        self.assertEqual([{"role": "user", "content": "unprovenanced conversation"}], request.contexts)
        # A persistent history setting must not revive Shio's second reader.
        # Other plugins' request parts remain untouched.
        self.assertIs(memory, request.extra_user_content_parts)
        self.assertEqual(["LivingMemory-owned object"], memory)
        self.assertEqual("none", event.get_extra("shio.sys001.group_context_owner"))
        self.assertEqual("Persona", request.system_prompt.split("\n\n")[0])

        plugin.context.enabled = False
        fresh_memory: list = []
        disabled = SimpleNamespace(
            prompt="current text", system_prompt="Persona", contexts=[{"role": "user", "content": "old"}],
            func_tool=None, extra_user_content_parts=fresh_memory,
        )
        await plugin.attach_turn_and_project_capabilities(event, disabled)
        # With history disabled both the conversation window and the parts
        # list stay untouched, and no extra history fetch happens.
        self.assertEqual([{"role": "user", "content": "old"}], disabled.contexts)
        self.assertEqual([], fresh_memory)
        self.assertEqual("none", event.get_extra("shio.sys001.group_context_owner"))
        self.assertEqual(0, plugin.context.history_calls)


class ContinuousWindowCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    scope = "qq:group:202"

    def plugin(self, *, capacity=4):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {"group": {
            "continuous_window_enabled": True,
            "continuous_window_seconds": 1,
            "continuous_window_max_scopes": capacity,
        }}}
        plugin._continuous_scopes = {}
        plugin._continuous_locks = {}
        plugin._continuous_terminated = False
        return plugin

    async def test_first_is_immediate_latest_waiter_wins_and_old_waiter_exits(self):
        plugin = self.plugin()
        self.assertEqual("immediate", await plugin._continuous_begin(self.scope, "first"))
        self.assertEqual("wait", await plugin._continuous_begin(self.scope, "second"))
        old = asyncio.create_task(plugin._continuous_wait_for_turn(self.scope, "second"))
        await asyncio.sleep(0)
        self.assertEqual("wait", await plugin._continuous_begin(self.scope, "third"))
        latest = asyncio.create_task(plugin._continuous_wait_for_turn(self.scope, "third"))
        await plugin._continuous_mark_terminal(self.scope, "first")

        self.assertFalse(await old)
        self.assertTrue(await latest)
        await plugin._continuous_mark_terminal(self.scope, "third")
        self.assertEqual("immediate", await plugin._continuous_begin(self.scope, "after-winner"))

    async def test_nonqual_extends_only_existing_window_and_capacity_fails_closed(self):
        plugin = self.plugin(capacity=1)
        self.assertFalse(await plugin._continuous_extend_nonqual(self.scope))
        self.assertEqual("immediate", await plugin._continuous_begin(self.scope, "first"))
        self.assertEqual("wait", await plugin._continuous_begin(self.scope, "second"))
        state = plugin._continuous_scopes[self.scope]
        before = state.deadline
        self.assertTrue(await plugin._continuous_extend_nonqual(self.scope))
        self.assertGreaterEqual(state.deadline, before)
        self.assertEqual("drop", await plugin._continuous_begin("qq:group:other", "other"))

    async def test_reload_wakes_waiter_without_second_request(self):
        plugin = self.plugin()
        await plugin._continuous_begin(self.scope, "first")
        await plugin._continuous_begin(self.scope, "winner")
        waiting = asyncio.create_task(plugin._continuous_wait_for_turn(self.scope, "winner"))
        await asyncio.sleep(0)
        await plugin.terminate()
        self.assertFalse(await waiting)
        self.assertEqual({}, plugin._continuous_scopes)

    async def test_expired_deadline_waits_for_first_terminal_without_zero_timeout_spin(self):
        plugin = self.plugin()
        await plugin._continuous_begin(self.scope, "first")
        await plugin._continuous_begin(self.scope, "winner")
        state = plugin._continuous_scopes[self.scope]
        state.deadline = asyncio.get_running_loop().time() - 0.01
        waiting = asyncio.create_task(plugin._continuous_wait_for_turn(self.scope, "winner"))
        await asyncio.sleep(0.02)
        self.assertFalse(waiting.done())
        await plugin._continuous_mark_terminal(self.scope, "first")
        self.assertTrue(await asyncio.wait_for(waiting, timeout=2.0))


class ContinuousWindowEventPathTests(unittest.IsolatedAsyncioTestCase):
    class Inbound(FakeEvent):
        def __init__(self, message_id, text="@bot", components=()):
            super().__init__(text)
            self.message_obj = SimpleNamespace(message_id=message_id)
            self.extras = {}
            self.is_at_or_wake_command = True
            self.call_llm = False
            self.components = list(components)

        def get_messages(self):
            return self.components

        def get_extra(self, key, default=None):
            return self.extras.get(key, default)

        def set_extra(self, key, value):
            self.extras[key] = value

        def should_call_llm(self, value):
            self.call_llm = value

        def request_llm(self, **kwargs):
            return SimpleNamespace(**kwargs)

    @staticmethod
    def plugin():
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {
            "ingress": {"group_allowed_scopes": ["qq:group:202"]},
            "group": {
                "continuous_window_enabled": True,
                "continuous_window_seconds": 1,
                "continuous_window_max_scopes": 4,
                "natural_participation_enabled": False,
            },
        }}
        manager = SimpleNamespace(
            get_curr_conversation_id=lambda _scope: asyncio.sleep(0, result="c"),
            get_conversation=lambda _scope, _conversation: asyncio.sleep(0, result=object()),
            new_conversation=lambda *_args, **_kwargs: asyncio.sleep(0, result="new"),
        )
        plugin.context = FakeContext()
        plugin.context.conversation_manager = manager
        plugin._natural_generations = {}
        plugin._continuous_scopes = {}
        plugin._continuous_locks = {}
        plugin._continuous_terminated = False
        return plugin

    async def test_each_direct_event_waits_for_the_r36_quiet_window(self):
        plugin = self.plugin()
        first = self.Inbound("m-1")
        first_request = await anext(plugin.request_event_bound_reply(first))
        self.assertEqual("@bot", first_request.prompt)
        self.assertEqual("m-1", first.get_extra(MAIN.SYS001_TURN_EXTRA).message_id)

        second = self.Inbound("m-2")
        second_task = asyncio.create_task(
            anext(plugin.request_event_bound_reply(second))
        )
        await asyncio.sleep(0)
        self.assertFalse(second_task.done())
        self.assertTrue(second.call_llm)

        await plugin.record_standard_send(first)
        second_request = await second_task
        self.assertEqual("@bot", second_request.prompt)
        self.assertEqual("m-2", second.get_extra(MAIN.SYS001_TURN_EXTRA).message_id)
        await plugin.record_standard_send(second)
        self.assertFalse(plugin._batch_scopes)

    async def test_waiting_media_winner_reopens_only_the_official_media_path(self):
        plugin = self.plugin()
        image = SimpleNamespace(type="image")
        first = self.Inbound("media-1", components=[image])
        self.assertEqual(
            [], [item async for item in plugin.request_event_bound_reply(first)]
        )
        self.assertFalse(first.call_llm)

        second = self.Inbound("media-2", components=[image])
        waiting = asyncio.create_task(
            anext(plugin.request_event_bound_reply(second))
        )
        await asyncio.sleep(0)
        self.assertFalse(second.call_llm)

        await plugin.record_standard_send(first)
        with self.assertRaises(StopAsyncIteration):
            await waiting
        self.assertFalse(second.call_llm)
        self.assertTrue(second.is_at_or_wake_command)


class MemeManager4154NormalHookFixture:
    """Minimal public normal-hook boundary for Meme Manager 4.15.4 / 3a4cac."""

    VERSION = "4.15.4"
    COMMIT = "3a4cac134abf22a8617eda837bd2c9a9c1b90b1f"
    PRIORITY = 99999

    class PendingImage:
        pass

    def __init__(self, fail_at=""):
        self.fail_at = fail_at
        self.response_text = None
        self.pending_image = None

    async def on_response(self, _event, response):
        if self.fail_at == "response":
            raise RuntimeError("Meme response hook failed")
        self.response_text = response.completion_text

    async def on_decorating(self, event):
        if self.fail_at == "decorating":
            raise RuntimeError("Meme decorating hook failed")
        self.pending_image = self.PendingImage()
        event.pending_image = self.pending_image

    async def after_message_sent(self, event):
        if self.fail_at == "after":
            raise RuntimeError("Meme after hook failed")
        if self.pending_image is not None:
            event.sent_images.append(self.pending_image)


class MemeManager4154ActualPathTests(unittest.IsolatedAsyncioTestCase):
    class Image:
        pass

    class Event(ToolObservationTests.Event):
        unified_msg_origin = "qq:group:202"

        def __init__(self, result, extras):
            super().__init__(extras)
            self._result = result
            self.sent_images = []
            self.sent_text = []

        def get_result(self):
            return self._result

    @staticmethod
    def snapshot():
        return MAIN.TurnSnapshot(
            platform_id="qq", scope="qq:group:202", account_id="999", sender_id="202",
            sender_name="member", self_id="999", message_id="meme-m",
            created_at=datetime(2024, 1, 1, tzinfo=UTC), message_text="input",
            is_private=False, is_master=False, at_self=False, reply_to_self=False,
            origin="direct",
        )

    @staticmethod
    def plugin():
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {"presentation": {
            "text_component_mode": "plugin", "text_component_max_segments": 3,
        }}}
        plugin.context = FakeContext()
        return plugin

    async def test_fixed_normal_hook_order_uses_final_text_and_keeps_text_send_owner(self):
        original_image = self.Image()
        result = SimpleNamespace(chain=[MAIN.Plain("甲。乙。"), original_image])
        lifecycle = MAIN.TurnLifecycle()
        event = self.Event(result, {
            MAIN.SYS001_TURN_EXTRA: self.snapshot(),
            MAIN.SYS001_LIFECYCLE_EXTRA: lifecycle,
        })
        plugin = self.plugin()
        meme = MemeManager4154NormalHookFixture()
        response = SimpleNamespace(role="assistant", completion_text="甲。乙。", reasoning_content="")
        request = SimpleNamespace(system_prompt="Persona", prompt="input", func_tool=None)

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
            [100000, -99998, -1000000],
            [handler.extras_configs["priority"] for handler in handlers],
        )
        self.assertGreater(100000, meme.PRIORITY)

        await plugin.attach_turn_and_project_capabilities(event, request)
        await plugin.observe_final_agent_response(event, response)
        await meme.on_response(event, response)
        await meme.on_decorating(event)
        await plugin.layout_text_components(event)

        # AstrBot's normal Respond stage owns all text components.  The fixed
        # Meme normal after-hook only sends its separately pending image.
        event.sent_text.extend(part for part in result.chain if isinstance(part, MAIN.Plain))
        await plugin.record_standard_send(event)
        await meme.after_message_sent(event)

        self.assertEqual("甲。乙。", meme.response_text)
        self.assertEqual("Persona", request.system_prompt.split("\n\n")[0])
        self.assertEqual(["甲。", "乙。"], [part.text for part in result.chain[:2]])
        self.assertIs(original_image, result.chain[2])
        self.assertEqual(["甲。", "乙。"], [part.text for part in event.sent_text])
        self.assertEqual([meme.pending_image], event.sent_images)

    async def test_missing_or_failed_meme_normal_hooks_do_not_block_text(self):
        for fail_at in ("missing", "response", "decorating", "after"):
            with self.subTest(fail_at=fail_at):
                result = SimpleNamespace(chain=[MAIN.Plain("甲。乙。"), self.Image()])
                event = self.Event(result, {
                    MAIN.SYS001_TURN_EXTRA: self.snapshot(),
                    MAIN.SYS001_LIFECYCLE_EXTRA: MAIN.TurnLifecycle(),
                })
                plugin = self.plugin()
                response = SimpleNamespace(
                    role="assistant", completion_text="甲。乙。", reasoning_content=""
                )
                await plugin.observe_final_agent_response(event, response)
                if fail_at != "missing":
                    meme = MemeManager4154NormalHookFixture(fail_at)
                    try:
                        await meme.on_response(event, response)
                        await plugin.layout_text_components(event)
                        await meme.on_decorating(event)
                        await meme.after_message_sent(event)
                    except RuntimeError:
                        pass
                else:
                    await plugin.layout_text_components(event)
                self.assertEqual("甲。乙。", response.completion_text)
                self.assertEqual("甲。乙。", "".join(
                    part.text for part in result.chain if isinstance(part, MAIN.Plain)
                ))


class ModelSegmentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_segments_are_reversible_and_invalid_output_falls_back_to_single_text(self) -> None:
        event = SimpleNamespace(unified_msg_origin="qq:group:202")
        presentation = {"text_component_min_segments": 1, "text_component_max_segments": 3, "model_segment_timeout_seconds": 1}
        for output, expected_count in (
            ('{"segments":["甲。","乙。"]}', 2),
            ('{"segments":["甲。"]}', 1),
            ('{"segments":["甲","乙"]}', 1),
            ('{"other":["甲。乙。"]}', 1),
            ('not json', 1),
        ):
            with self.subTest(output=output):
                plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
                provider = FakeProvider(output)
                plugin.context = FakeContext(current=provider)
                pieces = await plugin._model_text_components(event, "甲。乙。", presentation)
                self.assertEqual(expected_count, len(pieces))
                self.assertEqual("甲。乙。", "".join(pieces))
                self.assertIsNone(provider.calls[0]["func_tool"])


class SegmentedReplyCompatibilityTests(unittest.TestCase):
    def test_read_only_compatibility_requires_passthrough_shape(self) -> None:
        valid = {"enable": True, "only_llm_result": True, "split_mode": "regex", "regex": "(?s).+", "content_cleanup_rule": ""}
        self.assertTrue(SYS001.segmented_reply_compatibility(valid, platform_supported=True).compatible)
        for changed in ({"enable": False}, {"only_llm_result": False}, {"split_mode": "words"}, {"regex": ".+"}, {"content_cleanup_rule": "x"}):
            candidate = {**valid, **changed}
            self.assertFalse(SYS001.segmented_reply_compatibility(candidate, platform_supported=True).compatible)
        self.assertFalse(SYS001.segmented_reply_compatibility(valid, platform_supported=False).compatible)


if __name__ == "__main__":
    unittest.main()
