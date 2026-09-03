"""R13 Master termination fences for process-local alert state."""
from __future__ import annotations

import asyncio
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from test_name_semantic_provider_routing import MAIN, SYS001, FakeEvent
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        MAIN,
        SYS001,
        FakeEvent,
    )


class MasterKVSpy:
    """A public PluginKV spy: Master alert paths must never call it."""

    def __init__(self, legacy: dict[str, object] | None = None) -> None:
        self.legacy = dict(legacy or {})
        self.get_calls: list[str] = []
        self.put_calls: list[tuple[str, object]] = []
        self.delete_calls: list[str] = []

    async def get(self, key, default=None):
        self.get_calls.append(str(key))
        return self.legacy.get(str(key), default)

    async def put(self, key, value):
        self.put_calls.append((str(key), value))

    async def delete(self, key):
        self.delete_calls.append(str(key))

    def assert_unused(self, testcase: unittest.TestCase) -> None:
        testcase.assertEqual([], self.get_calls)
        testcase.assertEqual([], self.put_calls)
        testcase.assertEqual([], self.delete_calls)


class RequestEvent(FakeEvent):
    def __init__(self) -> None:
        super().__init__("direct private request")
        self.unified_msg_origin = "qq:person:master"
        self.message_obj = SimpleNamespace(message_id="request-1", timestamp=1_704_067_200)
        self.is_at_or_wake_command = False
        self._extras: dict[str, object] = {}
        self.request_llm_calls = 0
        self.send_trace: list[object] = []

    def is_private_chat(self):
        return True

    def is_admin(self):
        return True

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def should_call_llm(self, _value):
        return None

    def request_llm(self, **kwargs):
        self.request_llm_calls += 1
        return kwargs


class TraceContext:
    def __init__(self, send) -> None:
        self._send = send
        self.send_trace: list[tuple[object, object]] = []
        self.conversation_manager = self

    async def send_message(self, *args):
        self.send_trace.append(args)
        return await self._send(*args)

    async def get_curr_conversation_id(self, _umo):
        return "official-conversation"

    async def get_conversation(self, _umo, _conversation_id):
        return object()


class BlockingSendContext(TraceContext):
    def __init__(self) -> None:
        super().__init__(self._accept)
        self.send_started = asyncio.Event()
        self.send_release = asyncio.Event()
        self.send_finished = asyncio.Event()

    async def _accept(self, *_args):
        self.send_started.set()
        await self.send_release.wait()
        self.send_finished.set()
        return True


class DelayedCancellationSendContext(BlockingSendContext):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_seen = asyncio.Event()

    async def _accept(self, *_args):
        self.send_started.set()
        try:
            await self.send_release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await asyncio.sleep(0.03)
            return False
        self.send_finished.set()
        return True


class StubbornCancellationSendContext(BlockingSendContext):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_seen = asyncio.Event()

    async def _accept(self, *_args):
        self.send_started.set()
        while not self.send_release.is_set():
            try:
                await self.send_release.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
        self.send_finished.set()
        return True


class R13MasterTerminationTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, kv: MasterKVSpy | None = None):
        kv = kv or MasterKVSpy()
        value = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        value.get_kv_data = kv.get
        value.put_kv_data = kv.put
        value.delete_kv_data = kv.delete
        value.context = TraceContext(self.no_send)
        value.config = {"sys001": {
            "ingress": {"private_allowed_sender_ids": ["202"]},
            "group": {"natural_group_scopes": []},
            "master_alert": {
                "master_alert_enabled": True,
                "main_reply_exhausted_enabled": True,
                "review_repair_exhausted_enabled": True,
                "consecutive_threshold": 1,
                "window_minutes": 10,
                "master_alert_quiet_enabled": True,
                "quiet_start": "23:00",
                "quiet_end": "08:00",
                "display_timezone": "+08:00",
            },
        }}
        value._master_alert_record = SYS001.MasterAlertRecord(master_umo="qq:person:old")
        value._master_alert_ready = True
        value._master_alert_revision = 0
        value._master_alert_lock = asyncio.Lock()
        value._master_alert_terminated = False
        value._master_alert_terminating = False
        value._master_alert_timer = None
        value._master_alert_send_tasks = set()
        value._natural_ready = {}
        value._natural_locks = {}
        value._natural_candidates = {}
        value._natural_candidate_sequences = {}
        value._natural_active_candidate_watermarks = {}
        value._natural_generations = {}
        value._natural_cadence = {}
        value._natural_bindings = {}
        value._continuous_scopes = {}
        value._continuous_locks = {}
        value._auxiliary_epoch = 1
        value._auxiliary_terminated = False
        value._natural_terminated = False
        value._continuous_terminated = False
        value._batch_scopes = {}
        value._batch_terminated = False
        value._bubble_epoch = 0
        value._bubble_terminated = False
        value._auxiliary_tasks = set()
        value._kv_tasks = set()
        value._NATURAL_KV_AWAIT_SECONDS = 1.0
        return value

    async def no_send(self, *_args, **_kwargs):
        raise AssertionError("terminated instance must not send")

    def _private_snapshot(self, event: RequestEvent):
        return MAIN.create_snapshot(event, origin="private")

    async def _wait_for_terminating(self, plugin) -> None:
        for _ in range(50):
            if plugin._master_alert_terminating:
                return
            await asyncio.sleep(0)
        self.fail("termination pre-fence was not published")

    async def test_capture_waiting_on_mutex_never_publishes_after_terminate_prefence(self):
        self.assertEqual(Path(MAIN.__file__).resolve(), Path(__file__).resolve().parents[1] / "main.py")
        kv = MasterKVSpy({"master_alert_state_v1": {"legacy": True}})
        plugin = self.plugin(kv)
        old_record = plugin._master_alert_record
        event = RequestEvent()
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        capture = asyncio.create_task(plugin.capture_master_alert_binding(event, self._private_snapshot(event)))
        await asyncio.sleep(0)
        ending = asyncio.create_task(plugin.terminate())
        await self._wait_for_terminating(plugin)
        lock.release()
        self.assertFalse(await capture)
        await ending
        self.assertFalse(plugin._master_alert_ready)
        self.assertIs(plugin._master_alert_record, old_record)
        self.assertIsNone(plugin._master_alert_timer)
        kv.assert_unused(self)

    async def test_terminal_waiting_on_mutex_cannot_schedule_after_terminate(self):
        kv = MasterKVSpy()
        plugin = self.plugin(kv)
        event = RequestEvent()
        snapshot = self._private_snapshot(event)
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        with patch.object(MAIN, "master_alert_quiet_deadline", return_value=4_102_444_000.0):
            terminal = asyncio.create_task(plugin._record_master_alert_terminal(event, snapshot, success=False, terminal_reason="final_error"))
            await asyncio.sleep(0)
            ending = asyncio.create_task(plugin.terminate())
            await self._wait_for_terminating(plugin)
            lock.release()
            await terminal
            await ending
        self.assertFalse(plugin._master_alert_ready)
        self.assertIsNone(plugin._master_alert_timer)
        self.assertEqual([], plugin.context.send_trace)
        kv.assert_unused(self)

    async def test_request_capture_waiting_on_mutex_terminates_without_llm_request(self):
        kv = MasterKVSpy()
        plugin = self.plugin(kv)
        event = RequestEvent()
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        first = asyncio.create_task(anext(plugin.request_event_bound_reply(event), None))
        await asyncio.sleep(0)
        ending = asyncio.create_task(plugin.terminate())
        await self._wait_for_terminating(plugin)
        lock.release()
        self.assertIsNone(await first)
        await ending
        self.assertEqual(0, event.request_llm_calls)
        self.assertEqual([], plugin.context.send_trace)
        self.assertFalse(plugin._master_alert_ready)
        kv.assert_unused(self)

    async def test_terminate_prefence_before_submit_ownership_prevents_send(self):
        kv = MasterKVSpy()
        plugin = self.plugin(kv)
        context = BlockingSendContext()
        plugin.context = context
        plugin._master_alert_record = SYS001.MasterAlertRecord(master_umo="qq:person:old", report_id="r13-submit", report_status="pending")
        lock = plugin._master_alert_mutex()
        await lock.acquire()
        submit = asyncio.create_task(plugin._submit_master_alert_report("r13-submit"))
        await asyncio.sleep(0)
        ending = asyncio.create_task(plugin.terminate())
        await self._wait_for_terminating(plugin)
        lock.release()
        await submit
        await ending
        self.assertFalse(context.send_started.is_set())
        self.assertEqual([], context.send_trace)
        self.assertFalse(plugin._master_alert_ready)
        kv.assert_unused(self)

    async def test_started_send_is_cancelled_before_terminate_returns(self):
        kv = MasterKVSpy()
        plugin = self.plugin(kv)
        context = BlockingSendContext()
        plugin.context = context
        plugin._master_alert_record = SYS001.MasterAlertRecord(master_umo="qq:person:old", report_id="r13-started", report_status="pending")
        submit = asyncio.create_task(plugin._submit_master_alert_report("r13-started"))
        await context.send_started.wait()
        await plugin.terminate()
        with self.assertRaises(asyncio.CancelledError):
            await submit
        self.assertFalse(context.send_finished.is_set())
        self.assertFalse(plugin._master_alert_ready)
        self.assertEqual(set(), plugin._master_alert_send_tasks)
        kv.assert_unused(self)

    async def test_delayed_send_cleanup_returns_bounded_and_cannot_publish_late_state(self):
        kv = MasterKVSpy()
        plugin = self.plugin(kv)
        context = DelayedCancellationSendContext()
        plugin.context = context
        plugin._master_alert_record = SYS001.MasterAlertRecord(master_umo="qq:person:old", report_id="r13-stubborn", report_status="pending")
        submit = asyncio.create_task(plugin._submit_master_alert_report("r13-stubborn"))
        await asyncio.wait_for(context.send_started.wait(), timeout=2.0)
        plugin._NATURAL_KV_AWAIT_SECONDS = 0.02
        await asyncio.wait_for(plugin.terminate(), timeout=2.0)
        self.assertTrue(context.cancel_seen.is_set())
        self.assertFalse(context.send_finished.is_set())
        self.assertFalse(plugin._master_alert_ready)
        self.assertIsNone(plugin._master_alert_timer)
        self.assertEqual(set(), plugin._master_alert_send_tasks)
        await asyncio.wait_for(submit, timeout=2.0)
        self.assertFalse(plugin._master_alert_ready)
        self.assertEqual(1, len(context.send_trace))
        kv.assert_unused(self)

    async def test_timer_worker_send_drain_returns_bounded_and_detaches_late_timer(self):
        kv = MasterKVSpy()
        plugin = self.plugin(kv)
        context = StubbornCancellationSendContext()
        plugin.context = context
        deadline = time.time() - 1.0
        plugin._master_alert_record = SYS001.MasterAlertRecord(master_umo="qq:person:old", report_id="r13-timer", report_status="pending", quiet_deadline=deadline)
        timer = asyncio.create_task(plugin._master_alert_timer_worker("r13-timer", deadline))
        plugin._master_alert_timer = timer
        await asyncio.wait_for(context.send_started.wait(), timeout=2.0)
        plugin._NATURAL_KV_AWAIT_SECONDS = 0.02
        ending = asyncio.create_task(plugin.terminate())
        try:
            await asyncio.wait_for(asyncio.shield(ending), timeout=2.0)
            self.assertTrue(context.cancel_seen.is_set())
            self.assertFalse(context.send_finished.is_set())
            self.assertFalse(plugin._master_alert_ready)
            self.assertIsNone(plugin._master_alert_timer)
            self.assertEqual(set(), plugin._master_alert_send_tasks)
            self.assertEqual(1, len(context.send_trace))
        finally:
            context.send_release.set()
            await asyncio.wait_for(context.send_finished.wait(), timeout=2.0)
            await asyncio.wait_for(ending, timeout=2.0)
            await asyncio.gather(timer, return_exceptions=True)
        self.assertFalse(plugin._master_alert_ready)
        self.assertIsNone(plugin._master_alert_timer)
        self.assertEqual(1, len(context.send_trace))
        kv.assert_unused(self)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
