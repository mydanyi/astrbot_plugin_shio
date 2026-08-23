from __future__ import annotations

import dataclasses
import json
import sys
import types
import unittest
from enum import Enum
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_shio.core.contracts import PluginEvidenceStatus
from astrbot_plugin_shio.core.plugin_adapters.reneban import (
    EXPECTED_HANDLER_FULL_NAME,
    EXPECTED_HANDLER_MODULE,
    EXPECTED_HANDLER_NAME,
    EXPECTED_PLUGIN_NAME,
    ReNeBanEvidenceReason,
    ReNeBanHookEvidence,
    inspect_reneban_hook,
)


class AdapterMessage:
    name = "AdapterMessageEvent"


@dataclasses.dataclass
class FakeMetadata:
    name: str = EXPECTED_PLUGIN_NAME
    activated: bool = True
    star_handler_full_names: list[str] = dataclasses.field(
        default_factory=lambda: [EXPECTED_HANDLER_FULL_NAME]
    )

    @property
    def star_cls(self):
        raise AssertionError("adapter must not read ReNeBan private instance")


@dataclasses.dataclass
class FakeHandler:
    event_type: object = dataclasses.field(default_factory=AdapterMessage)
    handler_module_path: str = EXPECTED_HANDLER_MODULE
    handler_name: str = EXPECTED_HANDLER_NAME
    handler_full_name: str = EXPECTED_HANDLER_FULL_NAME
    enabled: bool = True
    extras_configs: dict = dataclasses.field(default_factory=lambda: {"priority": 114})

    @property
    def handler(self):
        raise AssertionError("adapter must not call or inspect ban handler internals")


class FakeContext:
    def __init__(self, metadata):
        self._metadata = metadata
        self.raw_message = "synthetic private message"
        self.sender_id = "synthetic-private-id"
        self.requested_names = []

    def get_registered_star(self, name):
        self.requested_names.append(name)
        return self._metadata

    @property
    def ban_data(self):
        raise AssertionError("adapter must not read private ban data")


class ReNeBanAdapterTests(unittest.TestCase):
    def test_exact_loaded_activated_adapter_message_hook_is_verified(self):
        context = FakeContext(FakeMetadata())
        seen = []

        def loader(value):
            seen.append(value)
            return (FakeHandler(),)

        evidence = inspect_reneban_hook(context, registry_loader=loader)

        self.assertIsInstance(evidence, ReNeBanHookEvidence)
        self.assertIs(evidence.status, PluginEvidenceStatus.VERIFIED)
        self.assertEqual(evidence.reason_codes, ())
        self.assertTrue(evidence.plugin_loaded)
        self.assertTrue(evidence.plugin_activated)
        self.assertTrue(evidence.handler_present)
        self.assertTrue(evidence.handler_enabled)
        self.assertEqual(evidence.handler_priority, 114)
        self.assertEqual(context.requested_names, [EXPECTED_PLUGIN_NAME])
        self.assertEqual(seen, [context])
        self.assertFalse(hasattr(evidence, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            evidence.status = PluginEvidenceStatus.ERROR

    def test_default_loader_uses_adapter_message_registry_api_not_iteration(self):
        calls = []

        class EventType(Enum):
            AdapterMessageEvent = "adapter_message"

        adapter_event_type = EventType.AdapterMessageEvent

        class NonIterableRegistry:
            def get_handlers_by_event_type(self, event_type, only_activated=True):
                calls.append((event_type, only_activated))
                return [FakeHandler()]

            def get_handler_by_full_name(self, full_name):
                self.last_full_name = full_name
                return None

        registry = NonIterableRegistry()
        star_handler_module = types.ModuleType("astrbot.core.star.star_handler")
        star_handler_module.EventType = EventType
        star_handler_module.star_handlers_registry = registry
        modules = {
            "astrbot": types.ModuleType("astrbot"),
            "astrbot.core": types.ModuleType("astrbot.core"),
            "astrbot.core.star": types.ModuleType("astrbot.core.star"),
            "astrbot.core.star.star_handler": star_handler_module,
        }
        with patch.dict(sys.modules, modules):
            evidence = inspect_reneban_hook(FakeContext(FakeMetadata()))

        self.assertIs(evidence.status, PluginEvidenceStatus.VERIFIED)
        self.assertEqual(calls, [(adapter_event_type, False)])
        self.assertEqual(registry.last_full_name, EXPECTED_HANDLER_FULL_NAME)

    def test_unrelated_handlers_from_same_plugin_module_do_not_change_exact_gate(self):
        command_handler = FakeHandler(
            handler_name="ban_help",
            handler_full_name=(
                "data.plugins.astrbot_plugin_reneban.main_ban_help"
            ),
            extras_configs={"priority": 0},
        )

        evidence = inspect_reneban_hook(
            FakeContext(FakeMetadata()),
            registry_loader=lambda _: (command_handler, FakeHandler()),
        )

        self.assertIs(evidence.status, PluginEvidenceStatus.VERIFIED)
        self.assertEqual(evidence.reason_codes, ())
        self.assertTrue(evidence.handler_enabled)
        self.assertEqual(evidence.handler_priority, 114)

    def test_missing_plugin_is_missing_without_loading_registry(self):
        context = FakeContext(None)
        calls = []
        evidence = inspect_reneban_hook(
            context,
            registry_loader=lambda value: calls.append(value),
        )
        self.assertIs(evidence.status, PluginEvidenceStatus.MISSING)
        self.assertEqual(evidence.reason_codes, (ReNeBanEvidenceReason.PLUGIN_MISSING,))
        self.assertEqual(calls, [])

    def test_plugin_or_handler_disabled_is_disabled(self):
        plugin_disabled = inspect_reneban_hook(
            FakeContext(FakeMetadata(activated=False)),
            registry_loader=lambda _: (FakeHandler(),),
        )
        self.assertIs(plugin_disabled.status, PluginEvidenceStatus.DISABLED)
        self.assertIn(ReNeBanEvidenceReason.PLUGIN_DISABLED, plugin_disabled.reason_codes)

        handler_disabled = inspect_reneban_hook(
            FakeContext(FakeMetadata()),
            registry_loader=lambda _: (FakeHandler(enabled=False),),
        )
        self.assertIs(handler_disabled.status, PluginEvidenceStatus.DISABLED)
        self.assertIn(ReNeBanEvidenceReason.HANDLER_DISABLED, handler_disabled.reason_codes)

    def test_context_metadata_and_handler_interface_changes_are_explicit(self):
        cases = (
            (
                object(),
                lambda _: (FakeHandler(),),
                ReNeBanEvidenceReason.CONTEXT_INTERFACE_CHANGED,
            ),
            (
                FakeContext(FakeMetadata(name="renamed_plugin")),
                lambda _: (FakeHandler(),),
                ReNeBanEvidenceReason.METADATA_NAME_CHANGED,
            ),
            (
                FakeContext(FakeMetadata(star_handler_full_names=[])),
                lambda _: (FakeHandler(),),
                ReNeBanEvidenceReason.METADATA_HANDLER_CHANGED,
            ),
            (
                FakeContext(FakeMetadata()),
                lambda _: (),
                ReNeBanEvidenceReason.HANDLER_MISSING,
            ),
            (
                FakeContext(FakeMetadata()),
                lambda _: (FakeHandler(handler_module_path="changed.module"),),
                ReNeBanEvidenceReason.HANDLER_INTERFACE_CHANGED,
            ),
            (
                FakeContext(FakeMetadata()),
                lambda _: (FakeHandler(handler_name="renamed_handler"),),
                ReNeBanEvidenceReason.HANDLER_INTERFACE_CHANGED,
            ),
            (
                FakeContext(FakeMetadata()),
                lambda _: (FakeHandler(handler_full_name="changed_full_name"),),
                ReNeBanEvidenceReason.HANDLER_INTERFACE_CHANGED,
            ),
            (
                FakeContext(FakeMetadata()),
                lambda _: (
                    FakeHandler(event_type=SimpleNamespace(name="OnLLMRequestEvent")),
                ),
                ReNeBanEvidenceReason.HANDLER_INTERFACE_CHANGED,
            ),
        )
        for context, loader, reason in cases:
            with self.subTest(reason=reason):
                evidence = inspect_reneban_hook(context, registry_loader=loader)
                self.assertIs(
                    evidence.status,
                    PluginEvidenceStatus.INTERFACE_CHANGED,
                )
                self.assertIn(reason, evidence.reason_codes)

    def test_wrong_priority_or_relative_order_is_hook_order_invalid(self):
        for priority in (113, 90, 115, None, True):
            with self.subTest(priority=priority):
                handler = FakeHandler(
                    extras_configs=(
                        {} if priority is None else {"priority": priority}
                    )
                )
                evidence = inspect_reneban_hook(
                    FakeContext(FakeMetadata()),
                    registry_loader=lambda _, value=handler: (value,),
                )
                self.assertIs(
                    evidence.status,
                    PluginEvidenceStatus.HOOK_ORDER_INVALID,
                )
                self.assertIn(
                    ReNeBanEvidenceReason.HOOK_ORDER_INVALID,
                    evidence.reason_codes,
                )

    def test_context_and_registry_exceptions_become_error_without_details(self):
        class BrokenContext:
            def get_registered_star(self, name):
                raise RuntimeError("private failure with synthetic id")

        first = inspect_reneban_hook(
            BrokenContext(),
            registry_loader=lambda _: (FakeHandler(),),
        )
        self.assertIs(first.status, PluginEvidenceStatus.ERROR)
        self.assertEqual(first.reason_codes, (ReNeBanEvidenceReason.CONTEXT_ERROR,))

        def broken_loader(_):
            raise RuntimeError("registry details must not escape")

        second = inspect_reneban_hook(
            FakeContext(FakeMetadata()),
            registry_loader=broken_loader,
        )
        self.assertIs(second.status, PluginEvidenceStatus.ERROR)
        self.assertEqual(second.reason_codes, (ReNeBanEvidenceReason.REGISTRY_ERROR,))
        self.assertNotIn("details", str(second.reason_codes))

    def test_adapter_never_reads_plugin_instance_handler_or_ban_private_data(self):
        context = FakeContext(FakeMetadata())
        evidence = inspect_reneban_hook(
            context,
            registry_loader=lambda _: (FakeHandler(),),
        )
        self.assertIs(evidence.status, PluginEvidenceStatus.VERIFIED)

    def test_trace_and_evidence_do_not_retain_raw_context_handler_or_ids(self):
        context = FakeContext(FakeMetadata())
        evidence = inspect_reneban_hook(
            context,
            registry_loader=lambda _: (FakeHandler(),),
        )
        rendered = json.dumps(evidence.trace_metadata(), ensure_ascii=False)
        for forbidden in (
            context.raw_message,
            context.sender_id,
            EXPECTED_HANDLER_MODULE,
            EXPECTED_HANDLER_FULL_NAME,
            EXPECTED_PLUGIN_NAME,
        ):
            self.assertNotIn(forbidden, rendered)
        fields = {field.name for field in dataclasses.fields(evidence)}
        for forbidden_field in (
            "context",
            "metadata",
            "handler",
            "message",
            "sender_id",
            "ban_data",
        ):
            self.assertNotIn(forbidden_field, fields)


if __name__ == "__main__":
    unittest.main()
