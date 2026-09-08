"""R23 settings, group-number admission, and lazy natural-state contracts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import MAIN


ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "core" / "sys001.py"
SPEC = importlib.util.spec_from_file_location("r23_sys001", CORE_PATH)
assert SPEC and SPEC.loader
SYS001 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SYS001
SPEC.loader.exec_module(SYS001)


class R23GroupNumberContractTests(unittest.TestCase):
    def snapshot(self, *, group_id="202", sender_id="member", scope="qq:group:202"):
        return SYS001.TurnSnapshot(
            platform_id="qq", scope=scope, account_id="999", sender_id=sender_id,
            sender_name="member", self_id="999", message_id="m-1",
            created_at=datetime(2026, 8, 30, tzinfo=SYS001.UTC), message_text="hello",
            is_private=False, is_master=False, at_self=False, reply_to_self=False,
            origin="pending", group_id=group_id,
        )

    def test_group_number_admits_without_requiring_the_real_umo(self):
        snapshot = self.snapshot()
        decision = SYS001.admit_ingress(
            snapshot,
            private_allowed_sender_ids=frozenset(), blocked_sender_ids=frozenset(),
            group_allowed_scopes=frozenset({"202"}), group_blocked_scopes=frozenset(),
        )
        self.assertTrue(decision.allowed)
        natural = SYS001.decide_entry(
            snapshot, native_wake=False, visible_name_wake=False,
            natural_enabled=True, allowed_group_scopes=frozenset({"202"}),
        )
        self.assertEqual(("request", "natural"), (natural.disposition, natural.origin))

    def test_sender_id_cannot_impersonate_a_group_number(self):
        snapshot = self.snapshot(group_id="303", sender_id="202")
        decision = SYS001.admit_ingress(
            snapshot,
            private_allowed_sender_ids=frozenset(), blocked_sender_ids=frozenset(),
            group_allowed_scopes=frozenset({"202"}), group_blocked_scopes=frozenset(),
        )
        self.assertFalse(decision.allowed)
        natural = SYS001.decide_entry(
            snapshot, native_wake=False, visible_name_wake=False,
            natural_enabled=True, allowed_group_scopes=frozenset({"202"}),
        )
        self.assertEqual("natural_scope_not_allowed", natural.reason)

    def test_existing_real_umo_values_remain_compatible(self):
        snapshot = self.snapshot()
        decision = SYS001.admit_ingress(
            snapshot,
            private_allowed_sender_ids=frozenset(), blocked_sender_ids=frozenset(),
            group_allowed_scopes=frozenset({"qq:group:202"}),
            group_blocked_scopes=frozenset({"qq:group:blocked"}),
        )
        self.assertTrue(decision.allowed)


class R23SettingsSchemaTests(unittest.TestCase):
    def test_stored_keys_types_numeric_defaults_and_option_values_are_unchanged(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        actual = {}

        def visit(node, path="sys001"):
            items = node.get("items") if node.get("type") == "object" else None
            if isinstance(items, dict):
                for key, value in items.items():
                    visit(value, f"{path}.{key}")
                return
            actual[path] = (node["type"], node.get("default"), node.get("options"))

        visit(schema["sys001"])
        expected = {
            "sys001.ingress.private_allowed_sender_ids": ("list", [], None), "sys001.ingress.blocked_sender_ids": ("list", [], None), "sys001.ingress.group_allowed_scopes": ("list", [], None), "sys001.ingress.group_blocked_scopes": ("list", [], None),
            "sys001.identity.master_relationship_enabled": ("bool", False, None), "sys001.identity.master_relationship_prompt": ("text", "", None),
            "sys001.group.name_wake_mode": ("string", "direct", ["direct", "semantic"]), "sys001.group.name_semantic_prompt": ("text", "判断当前消息是直接对机器人说话还是仅提及。只输出 JSON。", None), "sys001.group.name_semantic_provider_id": ("string", "", None), "sys001.group.name_semantic_fallback_provider_ids": ("list", [], None), "sys001.group.name_semantic_timeout_seconds": ("int", 8, None),
            "sys001.group.natural_participation_enabled": ("bool", False, None), "sys001.group.natural_group_scopes": ("list", [], None), "sys001.group.natural_participation_prompt": ("text", "这是群内一批没有直接叫你的真实消息。仅在确实自然、有帮助且不打断对话时回复；只输出严格 JSON：{\"decision\":\"REPLY\"}、{\"decision\":\"WAIT\"} 或 {\"decision\":\"NO_ACTION\"}。", None), "sys001.group.natural_decision_provider_id": ("string", "", None), "sys001.group.natural_decision_fallback_provider_ids": ("list", [], None), "sys001.group.natural_decision_timeout_seconds": ("int", 8, None), "sys001.group.natural_reply_cooldown_seconds": ("int", 45, None), "sys001.group.natural_frequency_window_minutes": ("int", 5, None), "sys001.group.natural_max_replies_per_window": ("int", 2, None), "sys001.group.natural_no_action_backoff_base_seconds": ("int", 2, None), "sys001.group.natural_no_action_backoff_max_seconds": ("int", 30, None), "sys001.group.continuous_window_enabled": ("bool", True, None), "sys001.group.continuous_window_seconds": ("int", 3, None), "sys001.group.continuous_window_max_scopes": ("int", 128, None),
            "sys001.capability_visibility.friendly_sender_ids": ("list", [], None), "sys001.capability_visibility.ordinary_capability_names": ("list", [], None),
            "sys001.final_review.mode": ("string", "off", ["off", "core", "additional", "combined"]), "sys001.final_review.additional_prompt": ("text", "", None), "sys001.final_review.provider_id": ("string", "", None), "sys001.final_review.fallback_provider_ids": ("list", [], None), "sys001.final_review.timeout_seconds": ("int", 8, None), "sys001.final_review.repair_enabled": ("bool", False, None), "sys001.final_review.max_repair_attempts": ("int", 1, None), "sys001.final_review.use_repaired_text": ("bool", False, None), "sys001.final_review.send_last_reply_on_review_exhausted": ("bool", False, None),
            "sys001.master_alert.master_alert_enabled": ("bool", False, None), "sys001.master_alert.main_reply_exhausted_enabled": ("bool", False, None), "sys001.master_alert.review_repair_exhausted_enabled": ("bool", False, None), "sys001.master_alert.consecutive_threshold": ("int", 3, None), "sys001.master_alert.window_minutes": ("int", 10, None), "sys001.master_alert.master_alert_quiet_enabled": ("bool", False, None), "sys001.master_alert.quiet_start": ("string", "23:00", None), "sys001.master_alert.quiet_end": ("string", "08:00", None), "sys001.master_alert.display_timezone": ("string", "+08:00", None),
            "sys001.presentation.text_component_mode": ("string", "single", ["model", "plugin", "single"]), "sys001.presentation.text_component_max_segments": ("int", 3, None), "sys001.presentation.text_component_min_segments": ("int", 1, None), "sys001.presentation.bubble_send_min_wait_seconds": ("float", 0, None), "sys001.presentation.bubble_send_max_wait_seconds": ("float", 0, None), "sys001.presentation.model_segment_provider_id": ("string", "", None), "sys001.presentation.model_segment_fallback_provider_ids": ("list", [], None), "sys001.presentation.model_segment_timeout_seconds": ("int", 8, None),
        }
        # These editable defaults were intentionally rewritten in 0.5.1.1.
        # Their actual Provider payload is covered by test_settings_review;
        # prose wording is not a compatibility contract.
        for path in (
            "sys001.group.name_semantic_prompt",
            "sys001.group.natural_participation_prompt",
            "sys001.final_review.additional_prompt",
            "sys001.final_review.core_prompt",
            "sys001.character_dialogue.situation_prompt",
            "sys001.character_dialogue.expression_prompt",
        ):
            field_type, default, options = actual.pop(path)
            self.assertEqual("text", field_type)
            self.assertIsInstance(default, str)
            self.assertTrue(default.strip())
            self.assertIsNone(options)
            expected.pop(path, None)
        # 0.5.1.2 notifies the first terminal failure; the old threshold is no
        # longer an editable setting. Other stored fields remain unchanged.
        expected.pop("sys001.master_alert.consecutive_threshold")
        # 0.5.1.4 explicitly increases the review-wide default; persisted user
        # values are still preserved by AstrBotConfig (Pipeline regression).
        expected["sys001.final_review.timeout_seconds"] = ("int", 20, None)
        expected["sys001.character_dialogue.enabled"] = ("bool", False, None)
        expected["sys001.character_dialogue.preserve_character_in_review"] = ("bool", True, None)
        self.assertEqual(expected, actual)

    def test_titles_are_short_chinese_and_options_keep_plain_labels(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        forbidden = (
            "SYS-001", "scope", "UMO", "Provider ID", "MessageChain",
            "segmented-reply", "purpose", "current/fallback", "ToolSet",
            "REPLY", "WAIT", "NO_ACTION",
        )

        def visit(node):
            items = node.get("items") if node.get("type") == "object" else None
            if isinstance(items, dict):
                for value in items.values():
                    visit(value)
                return
            description = node.get("description", "")
            self.assertIsInstance(description, str)
            self.assertLessEqual(len(description), 18)
            for term in forbidden:
                self.assertNotIn(term, description)
            if "options" in node:
                self.assertEqual(len(node["options"]), len(node.get("labels", [])))

        visit(schema["sys001"])

    def test_conditions_refer_only_to_sibling_fields(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

        def visit(node):
            items = node.get("items") if node.get("type") == "object" else None
            if not isinstance(items, dict):
                return
            for value in items.values():
                condition = value.get("condition")
                if condition is not None:
                    self.assertTrue(set(condition).issubset(items))
                visit(value)

        visit(schema["sys001"])

    def test_r25_provider_selector_metadata_matches_official_contract(self):
        """R25 must keep stored Provider IDs while reusing AstrBot's selectors."""
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        expected = {
            "group.name_semantic_provider_id": {
                "type": "string", "default": "", "condition": {"name_wake_mode": "semantic"},
                "_special": "select_provider", "items": None,
            },
            "group.name_semantic_fallback_provider_ids": {
                "type": "list", "default": [], "condition": {"name_wake_mode": "semantic"},
                "_special": "select_providers", "items": {"type": "string"},
            },
            "group.natural_decision_provider_id": {
                "type": "string", "default": "", "condition": {"natural_participation_enabled": True},
                "_special": "select_provider", "items": None,
            },
            "group.natural_decision_fallback_provider_ids": {
                "type": "list", "default": [], "condition": {"natural_participation_enabled": True},
                "_special": "select_providers", "items": {"type": "string"},
            },
            "final_review.provider_id": {
                "type": "string", "default": "", "condition": None,
                "_special": "select_provider", "items": None,
            },
            "final_review.fallback_provider_ids": {
                "type": "list", "default": [], "condition": None,
                "_special": "select_providers", "items": {"type": "string"},
            },
            "presentation.model_segment_provider_id": {
                "type": "string", "default": "", "condition": {"text_component_mode": "model"},
                "_special": "select_provider", "items": None,
            },
            "presentation.model_segment_fallback_provider_ids": {
                "type": "list", "default": [], "condition": {"text_component_mode": "model"},
                "_special": "select_providers", "items": {"type": "string"},
            },
        }
        actual = {}
        hints = {}
        for path in expected:
            section, key = path.split(".", 1)
            node = schema["sys001"]["items"][section]["items"][key]
            actual[path] = {
                "type": node["type"], "default": node.get("default"),
                "condition": node.get("condition"), "_special": node.get("_special"),
                "items": node.get("items"),
            }
            hints[path] = node.get("hint", "")
        self.assertEqual(expected, actual)
        for path, hint in hints.items():
            with self.subTest(path=path):
                self.assertNotIn("填写", hint)
                self.assertNotIn("ID", hint)

    def _load_official_v4274_config_service(self, star_registry):
        """Execute the fixed tag blob with import-only stubs at its public seam."""
        repo_text = os.environ.get(
            "SYS001_OFFICIAL_ASTRBOT_REPO", str(ROOT.parent / ".worktrees" / "ASTR-IF-001"),
        )
        repo = Path(repo_text.split("::", 1)[-1])

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
            ).stdout

        self.assertEqual(
            "4fe29759758255a35ad01ea6177a91c2293bfcd3",
            git("rev-parse", "v4.27.4^{commit}").strip(),
        )
        self.assertEqual(
            "2b87573107419ec4d36a59bdaaaf88cbd8c625ca",
            git("rev-parse", "v4.27.4:astrbot/dashboard/services/config_service.py").strip(),
        )
        source = git("show", "v4.27.4:astrbot/dashboard/services/config_service.py")
        self.assertIn("async def save_plugin_configs", source)
        self.assertEqual(
            hashlib.sha1(
                f"blob {len(source.encode('utf-8'))}\0".encode("utf-8") + source.encode("utf-8")
            ).hexdigest(),
            "2b87573107419ec4d36a59bdaaaf88cbd8c625ca",
        )

        stubs = {}

        def stub(name, **values):
            module = types.ModuleType(name)
            module.__dict__.update(values)
            stubs[name] = module
            return module

        logger = SimpleNamespace(
            debug=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None,
        )
        stub("astrbot")
        stub("astrbot.core", file_token_service=SimpleNamespace(), logger=logger)
        stub("astrbot.core.config")
        stub("astrbot.core.config.astrbot_config", AstrBotConfig=object)
        stub(
            "astrbot.core.config.default", CONFIG_METADATA_2={}, CONFIG_METADATA_3={},
            CONFIG_METADATA_3_SYSTEM={}, DEFAULT_CONFIG={},
            DEFAULT_VALUE_MAP={"bool": False, "file": [], "float": 0.0, "int": 0,
                               "list": [], "object": {}, "string": "", "text": ""},
        )
        stub(
            "astrbot.core.config.i18n_utils",
            ConfigMetadataI18n=type("ConfigMetadataI18n", (), {
                "convert_to_i18n_keys": staticmethod(lambda value: value),
            }),
        )
        stub("astrbot.core.core_lifecycle", AstrBotCoreLifecycle=object)
        stub("astrbot.core.db", BaseDatabase=object)
        stub("astrbot.core.platform")
        stub("astrbot.core.platform.register", platform_cls_map={}, platform_registry=[])
        stub("astrbot.core.provider")
        stub("astrbot.core.provider.register", provider_registry=[])
        stub("astrbot.core.star")
        stub("astrbot.core.star.star", star_registry=star_registry)
        stub("astrbot.core.utils")
        stub("astrbot.core.utils.astrbot_path", get_astrbot_plugin_data_path=lambda: Path("."))
        stub(
            "astrbot.core.utils.totp", is_totp_enabled=lambda *args: False,
            revoke_user_trusted_devices=lambda *args: None,
            set_pending_totp_secret=lambda *args: None,
            verify_configured_2fa_code=lambda *args: False,
        )
        stub("astrbot.core.utils.webhook_utils", ensure_platform_webhook_config=lambda *args: None)
        stub("astrbot.dashboard")

        async def run_maybe_async(callable_):
            return callable_()

        stub("astrbot.dashboard.async_utils", run_maybe_async=run_maybe_async)
        stub("astrbot.dashboard.responses", ApiError=RuntimeError)
        for name, module in stubs.items():
            if "." in name:
                parent, child = name.rsplit(".", 1)
                if parent in stubs:
                    setattr(stubs[parent], child, module)

        missing = object()
        original = {name: sys.modules.get(name, missing) for name in stubs}
        sys.modules.update(stubs)
        try:
            module = types.ModuleType("official_astrbot_v4274_config_service")
            exec(compile(source, "v4.27.4:astrbot/dashboard/services/config_service.py", "exec"), module.__dict__)
            return module
        finally:
            for name, previous in original.items():
                if previous is missing:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

    def test_r26_official_plugin_save_preserves_provider_values_and_order(self):
        """Exercise v4.27.4 validation and ConfigFileService save/reload, not JSON alone."""
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        selector_paths = (
            ("group", "name_semantic_provider_id"),
            ("group", "name_semantic_fallback_provider_ids"),
            ("group", "natural_decision_provider_id"),
            ("group", "natural_decision_fallback_provider_ids"),
            ("final_review", "provider_id"),
            ("final_review", "fallback_provider_ids"),
            ("presentation", "model_segment_provider_id"),
            ("presentation", "model_segment_fallback_provider_ids"),
        )

        def payload(*, empty):
            return {"sys001": {
                "group": {
                    "name_semantic_provider_id": "" if empty else "provider-redacted-name",
                    "name_semantic_fallback_provider_ids": [] if empty else ["provider-redacted-name-a", "provider-redacted-name-b"],
                    "natural_decision_provider_id": "" if empty else "provider-redacted-natural",
                    "natural_decision_fallback_provider_ids": [] if empty else ["provider-redacted-natural-a", "provider-redacted-natural-b"],
                },
                "final_review": {
                    "provider_id": "" if empty else "provider-redacted-review",
                    "fallback_provider_ids": [] if empty else ["provider-redacted-review-a", "provider-redacted-review-b"],
                },
                "presentation": {
                    "model_segment_provider_id": "" if empty else "provider-redacted-segment",
                    "model_segment_fallback_provider_ids": [] if empty else ["provider-redacted-segment-a", "provider-redacted-segment-b"],
                },
            }}

        for empty in (False, True):
            with self.subTest(empty=empty):
                events = []

                class FakeConfig:
                    def save_config(self, value):
                        events.append(("save", copy.deepcopy(value)))

                class FakePluginManager:
                    async def reload(self, name):
                        events.append(("reload", name))

                config = FakeConfig()
                config.schema = schema
                metadata = SimpleNamespace(name="shio", config=config)
                official = self._load_official_v4274_config_service([metadata])
                original = payload(empty=empty)
                before = json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                errors, validated = official.validate_config(copy.deepcopy(original), schema, is_core=False)
                self.assertEqual([], errors)
                self.assertEqual(before, json.dumps(validated, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

                service = official.ConfigFileService(SimpleNamespace(plugin_manager=FakePluginManager()))
                asyncio.run(service.save_plugin_configs(copy.deepcopy(original), "shio"))
                self.assertEqual(2, len(events))
                self.assertEqual("save", events[0][0])
                self.assertEqual(("reload", "shio"), events[1])
                saved = events[0][1]
                self.assertEqual(before, json.dumps(saved, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                for section, key in selector_paths:
                    with self.subTest(empty=empty, path=f"{section}.{key}"):
                        expected = original["sys001"][section][key]
                        actual = saved["sys001"][section][key]
                        self.assertIs(type(expected), type(actual))
                        if isinstance(expected, str):
                            self.assertEqual(expected.encode("utf-8"), actual.encode("utf-8"))
                        else:
                            self.assertEqual(expected, actual)
                            self.assertEqual(
                                "\0".join(expected).encode("utf-8"),
                                "\0".join(actual).encode("utf-8"),
                            )


class R23LazyNaturalRestoreTests(unittest.IsolatedAsyncioTestCase):
    scope = "qq:group:202"

    def plugin(self, get):
        value = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        value.config = {"sys001": {"group": {
            "natural_reply_cooldown_seconds": 45,
            "natural_frequency_window_minutes": 5,
            "natural_max_replies_per_window": 2,
        }}}
        value._natural_cadence = {}
        value._natural_bindings = {}
        value._natural_ready = {}
        value._natural_locks = {}
        value._natural_terminated = False
        value._auxiliary_terminated = False
        value._auxiliary_epoch = 1
        value._kv_tasks = set()
        # Keep the production eight-second bound intact.  This is a released
        # concurrent restore path, so the fixture uses one stable bounded second.
        value._NATURAL_KV_AWAIT_SECONDS = 1.0
        value.get_kv_data = get
        return value

    def committed(self):
        return SYS001.encode_natural_cadence_record(SYS001.NaturalCadenceRecord(
            self.scope, SYS001.NaturalCadence.empty(),
            "0123456789abcdef0123456789abcdef", "committed",
        ))

    async def test_two_first_events_restore_one_real_umo_once(self):
        calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def get(_key, _default=None):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return self.committed()

        plugin = self.plugin(get)
        first = asyncio.create_task(plugin._ensure_natural_scope_ready(self.scope))
        await entered.wait()
        second = asyncio.create_task(plugin._ensure_natural_scope_ready(self.scope))
        release.set()
        self.assertEqual([True, True], await asyncio.gather(first, second))
        self.assertEqual(1, calls)
        self.assertTrue(plugin._natural_ready[self.scope])

    async def test_pending_damage_exception_timeout_and_cancel_fail_closed(self):
        for name, outcome in (
            ("pending", {**self.committed(), "status": "pending"}),
            ("damage", {"damaged": True}),
            ("exception", RuntimeError("KV failed")),
        ):
            with self.subTest(name=name):
                async def get(_key, _default=None, result=outcome):
                    if isinstance(result, BaseException):
                        raise result
                    return result
                plugin = self.plugin(get)
                self.assertFalse(await plugin._ensure_natural_scope_ready(self.scope))
                self.assertFalse(plugin._natural_ready[self.scope])

        async def never(_key, _default=None):
            await asyncio.Event().wait()

        timed_out = self.plugin(never)
        self.assertFalse(await timed_out._ensure_natural_scope_ready(self.scope))
        self.assertFalse(timed_out._natural_ready[self.scope])

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked(_key, _default=None):
            started.set()
            await release.wait()
            return self.committed()

        cancelled = self.plugin(blocked)
        restoring = asyncio.create_task(
            cancelled._ensure_natural_scope_ready(self.scope)
        )
        await started.wait()
        restoring.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await restoring
        self.assertFalse(cancelled._natural_ready[self.scope])
        release.set()

    async def test_termination_fence_cannot_publish_a_late_restore(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed(_key, _default=None):
            started.set()
            await release.wait()
            return self.committed()

        plugin = self.plugin(delayed)
        restoring = asyncio.create_task(plugin._ensure_natural_scope_ready(self.scope))
        await started.wait()
        plugin._natural_terminated = True
        release.set()
        self.assertFalse(await restoring)
        self.assertFalse(plugin._natural_ready[self.scope])


class R24MasterOrdinaryGroupRouteTests(unittest.IsolatedAsyncioTestCase):
    """Master ordinary group events bypass only Shio's natural route state."""

    scope = "qq:group:202"

    class Event:
        created_at = 1_704_067_200.0

        def __init__(self, *, master):
            self.unified_msg_origin = R24MasterOrdinaryGroupRouteTests.scope
            self.message_obj = SimpleNamespace(message_id="master-ordinary")
            self.is_at_or_wake_command = False
            self._master = master
            self._extras = {}
            self.request_llm_calls = 0
            self.should_call_llm_calls = []

        def get_extra(self, key, default=None):
            return self._extras.get(key, default)

        def set_extra(self, key, value):
            self._extras[key] = value

        def get_messages(self):
            return []

        def get_self_id(self):
            return "999"

        def get_message_str(self):
            return "ordinary group text"

        def get_platform_id(self):
            return "qq"

        def get_group_id(self):
            return "202"

        def get_sender_id(self):
            return "master" if self._master else "member"

        def get_sender_name(self):
            return self.get_sender_id()

        def is_private_chat(self):
            return False

        def is_admin(self):
            return self._master

        def should_call_llm(self, value):
            self.should_call_llm_calls.append(value)

        def request_llm(self, **kwargs):
            self.request_llm_calls += 1
            return SimpleNamespace(**kwargs)

    def plugin(self, *, natural_enabled, natural_scopes, ingress):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {
            "ingress": ingress,
            "group": {
                "natural_participation_enabled": natural_enabled,
                "natural_group_scopes": natural_scopes,
                "continuous_window_enabled": True,
                "natural_reply_cooldown_seconds": 45,
                "natural_frequency_window_minutes": 5,
                "natural_max_replies_per_window": 2,
            },
        }}
        plugin.context = SimpleNamespace(
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=self.current_conversation_id,
                get_conversation=self.get_conversation,
            )
        )
        plugin._natural_ready = {self.scope: True}
        plugin._natural_cadence = {
            self.scope: MAIN.NaturalCadence(1, 0.0, (), 1, 4_102_444_800.0)
        }
        plugin._natural_generations = {self.scope: 7}
        plugin._natural_locks = {}
        plugin._natural_bindings = {self.scope: object()}
        plugin._natural_candidate_sequences = {self.scope: 3}
        plugin._natural_candidates = {self.scope: {3: 7}}
        plugin._continuous_scopes = {"other": object()}
        plugin._continuous_locks = {}
        plugin._natural_terminated = False
        plugin._auxiliary_terminated = False
        plugin._auxiliary_epoch = 1
        plugin.get_kv_data = self.unexpected_natural_kv_read
        plugin.put_kv_data = self.unexpected_natural_kv_write
        return plugin

    async def current_conversation_id(self, _umo):
        return "conversation"

    async def get_conversation(self, _umo, _conversation_id):
        return "official conversation"

    async def unexpected_natural_kv_read(self, *_args, **_kwargs):
        raise AssertionError("Master ordinary group route must not read natural KV")

    async def unexpected_natural_kv_write(self, *_args, **_kwargs):
        raise AssertionError("Master ordinary group route must not write natural KV")

    async def test_master_ordinary_group_uses_natural_policy_not_a_mandatory_route(self):
        disabled = self.plugin(
            natural_enabled=False, natural_scopes=[], ingress={"group_allowed_scopes": []},
        )
        disabled.config["sys001"]["group"]["continuous_window_enabled"] = False
        disabled_event = self.Event(master=True)
        self.assertEqual(
            [], [request async for request in disabled.request_event_bound_reply(disabled_event)]
        )
        self.assertEqual(0, disabled_event.request_llm_calls)

        enabled = self.plugin(
            natural_enabled=True, natural_scopes=["202"],
            ingress={"blocked_sender_ids": ["master"]},
        )
        enabled.config["sys001"]["group"]["continuous_window_enabled"] = False
        enabled_event = self.Event(master=True)
        self.assertEqual(
            [], [request async for request in enabled.request_event_bound_reply(enabled_event)]
        )
        self.assertEqual(0, enabled_event.request_llm_calls)
        # The configured natural cadence is intentionally active here; Master
        # bypasses ingress, not the natural cooling gate.
        self.assertIsNone(enabled_event.get_extra(MAIN.SYS001_TURN_EXTRA))

    async def test_non_master_still_obeys_group_and_cadence_rejections(self):
        disabled = self.plugin(
            natural_enabled=False, natural_scopes=[],
            ingress={"group_allowed_scopes": ["202"]},
        )
        disabled_event = self.Event(master=False)
        self.assertEqual(
            [], [request async for request in disabled.request_event_bound_reply(disabled_event)]
        )
        self.assertEqual(0, disabled_event.request_llm_calls)

        cooling = self.plugin(
            natural_enabled=True, natural_scopes=["202"],
            ingress={"group_allowed_scopes": ["202"]},
        )
        cooling_event = self.Event(master=False)
        self.assertEqual(
            [], [request async for request in cooling.request_event_bound_reply(cooling_event)]
        )
        self.assertEqual(0, cooling_event.request_llm_calls)


if __name__ == "__main__":
    unittest.main()
