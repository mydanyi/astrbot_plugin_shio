"""R14 regressions for lifecycle monotonicity and bounded auxiliary layout."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from test_name_semantic_provider_routing import FakeContext, FakeEvent, MAIN, SYS001
    import test_r13_master_termination_regressions as R13
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        FakeContext,
        FakeEvent,
        MAIN,
        SYS001,
    )
    from astrbot_plugin_shio.tests import test_r13_master_termination_regressions as R13


class _Event:
    unified_msg_origin = "qq:group:202"

    def __init__(self) -> None:
        self.extras: dict[str, object] = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class _CancellationResistantProvider:
    def __init__(self, late_text='{"decision":"DIRECT"}') -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.calls: list[dict[str, object]] = []
        self.late_text = late_text

    async def text_chat(self, *, prompt, func_tool):
        self.calls.append({"prompt": prompt, "func_tool": func_tool})
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
        return SimpleNamespace(completion_text=self.late_text)


class _AuxiliaryEvent(FakeEvent):
    """Recorded public event shape with per-event extras for real gateways."""

    def __init__(self, text: str = "Shio, hello") -> None:
        super().__init__(text)
        self._extras: dict[str, object] = {}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class R14LifecycleAndPresentationTests(unittest.IsolatedAsyncioTestCase):
    def _auxiliary_plugin(self, provider, *, group=None, review=None, explicit_route=False):
        """Use an explicit route only in the four chat-cancellation regressions."""
        plugin = R13.R13MasterTerminationTests().plugin(
            R13.MasterKVSpy()
        )
        plugin.context = FakeContext(
            current=None if explicit_route else provider,
            by_id={"r14-chat": provider} if explicit_route else None,
        )
        plugin.config = {
            "sys001": {
                "group": group or {},
                "final_review": review or {},
            }
        }
        # Production keeps its 8s setting.  This fixture keeps the same
        # Successful/immediately-released fixture paths share this bounded
        # 1s budget; production retains its independent 8s deadline.
        plugin._NATURAL_KV_AWAIT_SECONDS = 1.0
        return plugin

    async def _complete_within_hard_bound(self, provider, coroutine):
        """Watchdog the entry call without cancelling it before test cleanup."""
        entry = asyncio.create_task(coroutine)
        await asyncio.wait_for(provider.started.wait(), timeout=2.0)
        done, pending = await asyncio.wait({entry}, timeout=1.0)
        if pending:
            provider.release.set()
            await asyncio.wait_for(entry, timeout=2.0)
            self.fail("real auxiliary entry exceeded its hard deadline")
        return entry.result()

    async def _release_auxiliary_tasks(self, plugin, provider):
        provider.release.set()
        tasks = tuple(getattr(plugin, "_auxiliary_tasks", set()))
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
        await asyncio.sleep(0)
        self.assertEqual(set(), getattr(plugin, "_auxiliary_tasks", set()))

    async def test_auxiliary_deadline_returns_without_waiting_for_cancel_ack(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin._auxiliary_epoch = 1
        plugin._auxiliary_terminated = False
        plugin._natural_generations = {}
        plugin._natural_candidates = {}
        provider = _CancellationResistantProvider()
        event = _Event()
        binding = plugin._bind_auxiliary_call(event)

        helper = asyncio.create_task(
            plugin._auxiliary_text_chat(
                event, binding, provider, prompt="bounded", timeout=0.01
            )
        )
        await asyncio.wait_for(provider.started.wait(), timeout=2.0)
        try:
            self.assertIsNone(await asyncio.wait_for(helper, timeout=1.0))
            self.assertTrue(provider.cancel_seen.is_set())
            self.assertEqual(1, len(plugin._auxiliary_tasks))
        finally:
            provider.release.set()
            await asyncio.wait_for(
                asyncio.gather(*tuple(plugin._auxiliary_tasks)), timeout=2.0
            )
            await asyncio.sleep(0)
        self.assertEqual(set(), plugin._auxiliary_tasks)

    async def test_name_semantic_gateway_deadline_detaches_late_direct(self):
        provider = _CancellationResistantProvider('{"decision":"DIRECT"}')
        plugin = self._auxiliary_plugin(
            provider,
            explicit_route=True,
            group={
                "name_wake_mode": "semantic",
                "name_semantic_timeout_seconds": 0.01,
                "name_semantic_provider_id": "r14-chat",
                "name_semantic_fallback_provider_ids": [],
            },
        )
        event = _AuxiliaryEvent("Shio, decide")
        try:
            result = await self._complete_within_hard_bound(
                provider, plugin._visible_name_wake(event)
            )
            self.assertFalse(result)
            self.assertTrue(provider.cancel_seen.is_set())
            self.assertEqual(0, plugin.context.current_calls)
            self.assertEqual(["r14-chat"], plugin.context.lookup_ids)
            self.assertEqual([None], [call["func_tool"] for call in provider.calls])
            self.assertNotIn("shio.sys001.generation", event._extras)
        finally:
            await self._release_auxiliary_tasks(plugin, provider)
        self.assertFalse(event.get_extra("shio.sys001.requested", False))

    async def test_natural_gateway_deadline_detaches_late_reply_and_terminate_is_bounded(self):
        provider = _CancellationResistantProvider('{"decision":"REPLY"}')
        plugin = self._auxiliary_plugin(
            provider,
            explicit_route=True,
            group={
                "natural_decision_provider_id": "r14-chat",
                "natural_decision_fallback_provider_ids": [],
                "natural_decision_timeout_seconds": 0.01,
                "natural_participation_prompt": "only when helpful",
            },
        )
        event = _AuxiliaryEvent("ordinary group text")
        snapshot = MAIN.create_snapshot(event, origin="natural")
        try:
            result = await self._complete_within_hard_bound(
                provider, plugin._decide_natural_participation(event, snapshot)
            )
            self.assertEqual("", result)
            attempts = event.get_extra("shio.sys001.natural_decision_attempts")
            self.assertEqual("unavailable", attempts[0]["outcome"])
            self.assertIsInstance(attempts[0]["elapsed_ms"], int)
            self.assertNotIn("shio.sys001.generation", event._extras)
            await asyncio.wait_for(plugin.terminate(), timeout=2.0)
            self.assertTrue(plugin._auxiliary_terminated)
            self.assertTrue(provider.cancel_seen.is_set())
            self.assertEqual(0, plugin.context.current_calls)
            self.assertEqual(["r14-chat"], plugin.context.lookup_ids)
        finally:
            await self._release_auxiliary_tasks(plugin, provider)
        self.assertNotIn("shio.sys001.natural_active", event._extras)

    async def test_final_review_gateway_deadline_exhausts_without_late_replacement(self):
        provider = _CancellationResistantProvider(
            '{"action":"replace","text":"late replacement"}'
        )
        plugin = self._auxiliary_plugin(
            provider,
            explicit_route=True,
            review={
                "mode": "core",
                "timeout_seconds": 0.01,
                "max_repair_attempts": 1,
                "repair_enabled": True,
                "use_repaired_text": True,
                "provider_id": "r14-chat",
                "fallback_provider_ids": [],
            },
        )
        event = _AuxiliaryEvent("reviewed event")
        snapshot = MAIN.create_snapshot(event, origin="private")
        try:
            outcome = await self._complete_within_hard_bound(
                provider, plugin._review_final_text(event, snapshot, "original text")
            )
            self.assertEqual("original text", outcome.text)
            self.assertTrue(outcome.exhausted)
            self.assertFalse(outcome.stale)
            self.assertEqual([None], [call["func_tool"] for call in provider.calls])
            self.assertEqual(0, plugin.context.current_calls)
            self.assertEqual(["r14-chat"], plugin.context.lookup_ids)
        finally:
            await self._release_auxiliary_tasks(plugin, provider)
        self.assertNotIn("shio.sys001.generation", event._extras)

    async def test_model_components_gateway_deadline_falls_back_without_late_segments(self):
        provider = _CancellationResistantProvider('{"segments":["alpha","beta"]}')
        plugin = self._auxiliary_plugin(provider, explicit_route=True)
        event = _AuxiliaryEvent("layout event")
        presentation = {
            "model_segment_provider_id": "r14-chat",
            "model_segment_fallback_provider_ids": [],
            "model_segment_timeout_seconds": 0.01,
            "text_component_min_segments": 1,
            "text_component_max_segments": 3,
        }
        try:
            pieces = await self._complete_within_hard_bound(
                provider,
                plugin._model_text_components(event, "alphabeta", presentation),
            )
            self.assertEqual(["alphabeta"], pieces)
            self.assertEqual([None], [call["func_tool"] for call in provider.calls])
            self.assertEqual(0, plugin.context.current_calls)
            self.assertEqual(["r14-chat"], plugin.context.lookup_ids)
        finally:
            await self._release_auxiliary_tasks(plugin, provider)
        self.assertNotIn("shio.sys001.generation", event._extras)

    @staticmethod
    def _layout_plugin(mode, minimum, maximum):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {
            "sys001": {
                "presentation": {
                    "text_component_mode": mode,
                    "text_component_min_segments": minimum,
                    "text_component_max_segments": maximum,
                    "model_segment_provider_id": "",
                    "model_segment_fallback_provider_ids": [],
                    "model_segment_timeout_seconds": 0.01,
                }
            }
        }
        return plugin

    @staticmethod
    def _layout_event(text, *, origin="direct", before=(), after=()):
        event = _AuxiliaryEvent()
        event._result = SimpleNamespace(chain=[*before, MAIN.Plain(text), *after])
        event.get_result = lambda: event._result
        event.set_extra(
            MAIN.SYS001_TURN_EXTRA,
            MAIN.create_snapshot(FakeEvent(), origin=origin),
        )
        return event

    async def test_plugin_layout_global_minimum_refines_sentence_boundaries_in_place(self):
        before = object()
        after = object()
        for origin in ("private", "name", "natural"):
            with self.subTest(origin=origin):
                event = self._layout_event(
                    "甲。乙甲乙", origin=origin, before=(before,), after=(after,)
                )
                await self._layout_plugin("plugin", 3, 3).layout_text_components(event)
                chain = event.get_result().chain
                pieces = [part.text for part in chain if isinstance(part, MAIN.Plain)]
                self.assertIs(before, chain[0])
                self.assertIs(after, chain[-1])
                self.assertEqual(3, len(pieces))
                self.assertEqual("甲。", pieces[0])
                self.assertEqual("甲。乙甲乙", "".join(pieces))
                self.assertTrue(SYS001.text_components_survive_standard_strip(pieces))
                self.assertIsNone(event.get_extra("shio.sys001.text_component_layout_status"))

    async def test_plugin_layout_minimum_maximum_and_safe_degrade_are_observable(self):
        merged = self._layout_event("甲。乙。丙。")
        await self._layout_plugin("plugin", 2, 2).layout_text_components(merged)
        merged_pieces = [part.text for part in merged.get_result().chain]
        self.assertEqual(["甲。", "乙。丙。"], merged_pieces)
        self.assertEqual("甲。乙。丙。", "".join(merged_pieces))

        unsafe = self._layout_event(" a ")
        original = unsafe.get_result().chain[0]
        await self._layout_plugin("plugin", 3, 3).layout_text_components(unsafe)
        self.assertEqual([original], unsafe.get_result().chain)
        self.assertEqual(" a ", unsafe.get_result().chain[0].text)
        self.assertEqual(
            "degraded_single",
            unsafe.get_extra("shio.sys001.text_component_layout_status"),
        )

    def test_global_minimum_never_splits_unicode_clusters_or_strip_sensitive_text(self):
        text = "Ae\u0301B👩\u200d❤️\u200d💋\u200d👩🇨🇳Z"
        pieces = SYS001.split_text_components(text, minimum=3, maximum=3)
        self.assertEqual(text, "".join(pieces))
        self.assertTrue(SYS001.text_components_survive_standard_strip(pieces))
        for piece in pieces:
            self.assertFalse(piece.startswith(("\u0301", "\ufe0f", "\u200d")))
            self.assertFalse(piece.endswith("\u200d"))
        for left, right in zip(pieces, pieces[1:]):
            self.assertFalse(left.endswith("🇨") and right.startswith("🇳"))

    async def test_single_and_model_modes_keep_their_global_minimum_contract(self):
        single = self._layout_event("abcdef")
        await self._layout_plugin("single", 3, 3).layout_text_components(single)
        self.assertEqual(["abcdef"], [part.text for part in single.get_result().chain])

        model = self._layout_event("abcdef")
        plugin = self._layout_plugin("model", 3, 3)

        async def model_fallback(*_args, **_kwargs):
            return ["abcdef"]

        plugin._model_text_components = model_fallback
        await plugin.layout_text_components(model)
        self.assertEqual(["abcdef"], [part.text for part in model.get_result().chain])
        self.assertEqual(
            "degraded_single",
            model.get_extra("shio.sys001.text_component_layout_status"),
        )

    def test_schema_documents_global_minimum_and_safe_single_component_downgrade(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        field = schema["sys001"]["items"]["presentation"]["items"][
            "text_component_min_segments"
        ]
        self.assertEqual("期望最少文字气泡数", field["description"])
        self.assertIn("仍可只有一段", field["hint"])

    async def test_terminated_instance_initialize_cannot_republish_late_ready(self):
        kv = R13.MasterKVSpy()
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        plugin.config["sys001"]["group"]["natural_group_scopes"] = ["group:r14"]
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_get(_key, default=None):
            started.set()
            await release.wait()
            return default

        plugin.get_kv_data = blocked_get
        plugin._initialized = False
        plugin._initialize_lock = asyncio.Lock()
        restoring = asyncio.create_task(plugin.initialize())
        await started.wait()
        await plugin.terminate()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await restoring
        await plugin.initialize()
        self.assertTrue(plugin._master_alert_terminated)
        self.assertFalse(plugin._master_alert_ready)
        self.assertFalse(plugin._natural_ready.get("group:r14", False))
        self.assertIsNone(plugin._master_alert_timer)

    async def test_terminated_instance_repeat_initialize_makes_no_new_kv_get(self):
        kv = R13.MasterKVSpy()
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        calls = 0

        async def counted_get(_key, default=None):
            nonlocal calls
            calls += 1
            return default

        plugin.get_kv_data = counted_get
        plugin._initialized = False
        plugin._initialize_lock = asyncio.Lock()
        await plugin.terminate()
        await plugin.initialize()
        self.assertEqual(0, calls)
        self.assertTrue(plugin._master_alert_terminated)
        self.assertFalse(plugin._master_alert_ready)

    async def test_concurrent_initialize_publishes_fresh_master_once_without_kv(self):
        kv = R13.MasterKVSpy({"master_alert_state_v1": {"legacy": True}})
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        plugin._initialized = False
        plugin._initialize_lock = asyncio.Lock()
        before = plugin._master_alert_revision
        await asyncio.gather(plugin.initialize(), plugin.initialize())
        self.assertEqual(SYS001.MasterAlertRecord(), plugin._master_alert_record)
        self.assertTrue(plugin._master_alert_ready)
        self.assertEqual(before + 1, plugin._master_alert_revision)
        self.assertIsNone(plugin._master_alert_timer)
        self.assertEqual([], plugin.context.send_trace)
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_new_instance_ignores_stale_pending_and_starts_empty(self):
        kv = R13.MasterKVSpy({
            "master_alert_state_v1": SYS001.encode_master_alert_record(
                SYS001.MasterAlertRecord(
                    master_umo="qq:person:master",
                    report_id="old-pending",
                    report_status="pending",
                )
            )
        })
        first = R13.R13MasterTerminationTests().plugin(kv)
        first._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:master",
            report_id="new-submitting",
            report_status="submitting",
        )
        second = R13.R13MasterTerminationTests().plugin(kv)
        second._initialized = False
        second._initialize_lock = asyncio.Lock()
        await second.initialize()
        self.assertEqual(SYS001.MasterAlertRecord(), second._master_alert_record)
        self.assertTrue(second._master_alert_ready)
        self.assertIsNone(second._master_alert_timer)
        self.assertEqual([], second.context.send_trace)
        kv.assert_unused(self)
        await first.terminate()
        await second.terminate()

    def test_plugin_minimum_is_global_or_downgrades_without_text_loss(self):
        pieces = SYS001.split_text_components("abcdef", minimum=3, maximum=4)
        self.assertEqual(3, len(pieces))
        self.assertTrue(all(piece for piece in pieces))
        self.assertEqual("abcdef", "".join(pieces))
        self.assertEqual(
            ["a b"],
            SYS001.split_text_components("a b", minimum=2, maximum=3),
        )

    async def test_binding_then_terminal_keeps_one_master_record_transition(self):
        """Binding queued first under the mutex keeps both local transitions."""
        kv = R13.MasterKVSpy()
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        plugin.config["sys001"]["master_alert"]["consecutive_threshold"] = 3
        event = R13.RequestEvent()
        snapshot = R13.R13MasterTerminationTests()._private_snapshot(event)
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        binding = asyncio.create_task(plugin.capture_master_alert_binding(event, snapshot))
        await asyncio.sleep(0)
        terminal = asyncio.create_task(
            plugin._record_master_alert_terminal(
                event, snapshot, success=False, terminal_reason="final_error"
            )
        )
        lock.release()
        self.assertTrue(await binding)
        await terminal
        record = plugin._master_alert_record
        self.assertEqual("qq:person:master", record.master_umo)
        self.assertEqual("final_error", record.error_type)
        self.assertEqual(1, record.consecutive_count)
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_terminal_then_binding_keeps_failure_and_replaces_only_umo(self):
        """A binding queued second updates contact without rolling back failure."""
        kv = R13.MasterKVSpy()
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        # This test covers an immediate send, not the wall-clock quiet window.
        plugin.config["sys001"]["master_alert"]["master_alert_quiet_enabled"] = False
        plugin.config["sys001"]["master_alert"]["consecutive_threshold"] = 3
        terminal_event = R13.RequestEvent()
        terminal_snapshot = R13.R13MasterTerminationTests()._private_snapshot(terminal_event)
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        terminal = asyncio.create_task(
            plugin._record_master_alert_terminal(
                terminal_event, terminal_snapshot,
                success=False, terminal_reason="final_error",
            )
        )
        await asyncio.sleep(0)
        binding_event = R13.RequestEvent()
        binding_event.unified_msg_origin = "qq:person:new-master"
        binding_snapshot = R13.R13MasterTerminationTests()._private_snapshot(binding_event)
        binding = asyncio.create_task(
            plugin.capture_master_alert_binding(binding_event, binding_snapshot)
        )
        lock.release()
        await terminal
        self.assertTrue(await binding)
        record = plugin._master_alert_record
        self.assertEqual("qq:person:new-master", record.master_umo)
        self.assertEqual("final_error", record.error_type)
        self.assertEqual(1, record.consecutive_count)
        # The first fault now attempts notification (this fixture rejects it).
        self.assertEqual("failed", record.report_status)
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_stale_binding_schedule_cannot_restore_pending_after_submitting(self):
        """A binding's old pending candidate may not overwrite a submit marker."""
        kv = R13.MasterKVSpy()
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        plugin._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:old", error_type="final_error",
            consecutive_count=1, window_started_at=1.0,
            report_id="r14-pending", report_status="pending",
        )
        context = R13.BlockingSendContext()
        plugin.context = context
        schedule_entered = asyncio.Event()
        schedule_release = asyncio.Event()
        original_schedule = plugin._schedule_pending_master_alert

        async def delayed_schedule(record):
            schedule_entered.set()
            await schedule_release.wait()
            await original_schedule(record)

        plugin._schedule_pending_master_alert = delayed_schedule
        event = R13.RequestEvent()
        binding = asyncio.create_task(
            plugin.capture_master_alert_binding(
                event, R13.R13MasterTerminationTests()._private_snapshot(event)
            )
        )
        await asyncio.wait_for(schedule_entered.wait(), timeout=2.0)
        submitting = asyncio.create_task(plugin._submit_master_alert_report("r14-pending"))
        await asyncio.wait_for(context.send_started.wait(), timeout=2.0)
        self.assertEqual("submitting", plugin._master_alert_record.report_status)
        with patch.object(MAIN, "master_alert_quiet_deadline", return_value=9_999_999_999.0):
            schedule_release.set()
            self.assertTrue(await binding)
        self.assertEqual("submitting", plugin._master_alert_record.report_status)
        self.assertIsNone(plugin._master_alert_timer)
        self.assertEqual(1, len(context.send_trace))
        context.send_release.set()
        await submitting
        self.assertEqual("submitted", plugin._master_alert_record.report_status)
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_submitting_then_binding_preserves_marker_and_sends_once(self):
        """Binding during the official send replaces only the local contact."""
        kv = R13.MasterKVSpy()
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        plugin._master_alert_record = SYS001.MasterAlertRecord(
            master_umo="qq:person:old", error_type="final_error",
            consecutive_count=1, window_started_at=1.0,
            report_id="r14-submit", report_status="pending",
        )
        context = R13.BlockingSendContext()
        plugin.context = context
        submit = asyncio.create_task(plugin._submit_master_alert_report("r14-submit"))
        await asyncio.wait_for(context.send_started.wait(), timeout=2.0)
        binding_event = R13.RequestEvent()
        binding_event.unified_msg_origin = "qq:person:new-master"
        binding = asyncio.create_task(
            plugin.capture_master_alert_binding(
                binding_event,
                R13.R13MasterTerminationTests()._private_snapshot(binding_event),
            )
        )
        self.assertTrue(await binding)
        self.assertEqual("submitting", plugin._master_alert_record.report_status)
        self.assertEqual("qq:person:new-master", plugin._master_alert_record.master_umo)
        self.assertEqual(1, len(context.send_trace))
        context.send_release.set()
        await submit
        self.assertEqual("submitted", plugin._master_alert_record.report_status)
        self.assertEqual("qq:person:new-master", plugin._master_alert_record.master_umo)
        self.assertEqual(1, len(context.send_trace))
        kv.assert_unused(self)
        await plugin.terminate()

    async def test_request_admission_prefence_wins_while_master_mutex_is_held(self):
        """A real event may not yield the official request after terminate wins."""
        kv = R13.MasterKVSpy()
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        # This regression owns the termination pre-fence only; R36 batching is
        # independently exercised in test_r36_ambient_batching.
        plugin.config["sys001"]["group"]["continuous_window_enabled"] = False
        event = R13.RequestEvent()
        conversation_started = asyncio.Event()
        conversation_release = asyncio.Event()

        async def blocked_conversation(_event):
            conversation_started.set()
            await conversation_release.wait()
            return object()

        plugin._current_conversation = blocked_conversation
        old_event = asyncio.create_task(
            anext(plugin.request_event_bound_reply(event), None)
        )
        await asyncio.wait_for(conversation_started.wait(), timeout=2.0)
        # Reaching the official conversation proves capture completed before
        # this test owns the mutex; it is not the weaker "lock before capture"
        # ordering from the original R14 regression.
        master_lock = plugin._master_alert_mutex()
        await master_lock.acquire()
        try:
            ending = asyncio.create_task(plugin.terminate())
            for _ in range(10):
                if plugin._master_alert_terminating:
                    break
                await asyncio.sleep(0)
            self.assertTrue(plugin._master_alert_terminating)
            self.assertFalse(plugin._master_alert_terminated)
            conversation_release.set()
            await asyncio.sleep(0)
        finally:
            master_lock.release()
        self.assertIsNone(await old_event)
        await ending
        self.assertEqual(0, event.request_llm_calls)
        self.assertEqual([], plugin.context.send_trace)
        self.assertFalse(plugin._master_alert_ready)
        self.assertIsNone(plugin._master_alert_timer)

    async def test_request_admission_first_yields_one_bound_request_before_termination(self):
        """The admitted real event is not replayed after bounded termination."""
        kv = R13.MasterKVSpy()
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        event = R13.RequestEvent()
        request = await anext(plugin.request_event_bound_reply(event), None)
        self.assertIsNotNone(request)
        self.assertEqual(1, event.request_llm_calls)
        await plugin.terminate()
        self.assertFalse(plugin._master_alert_ready)
        self.assertIsNone(plugin._master_alert_timer)
        self.assertEqual([], plugin.context.send_trace)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
