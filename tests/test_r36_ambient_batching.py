"""R36 owner-contract regressions for ambient batching.

These tests drive the public event hook and inspect only externally observable
request ownership/contexts.  They do not duplicate the plugin scheduler.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

try:
    from test_name_semantic_provider_routing import MAIN, SYS001, FakeContext, FakeProvider
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        MAIN, SYS001, FakeContext, FakeProvider,
    )


SCOPE = "qq:group:202"
R35_CANDIDATE = Path(__file__).resolve().parents[2] / "deploy" / "SYS-001" / "astrbot_plugin_shio_sys001_candidate_r35.zip"
R35_CANDIDATE_SHA256 = "5e1c848f3c587db1963e1f8db49286d16e886e68dab641975fce5a7a9fa63956"


class _Inbound:
    unified_msg_origin = SCOPE
    created_at = 1_704_067_200.0

    def __init__(
        self, message_id: str, text: str, *, admin: bool = False,
        directed: bool = True, components=None, scope: str = SCOPE,
    ):
        self.unified_msg_origin = scope
        self.message_obj = SimpleNamespace(message_id=message_id)
        self._text = text
        self._components = components or [SimpleNamespace(type="plain", text=text)]
        self._admin = admin
        self.is_at_or_wake_command = directed
        self.extras = {}
        self.call_llm = False

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def should_call_llm(self, value):
        self.call_llm = value

    def request_llm(self, **kwargs):
        return SimpleNamespace(**kwargs)

    def get_messages(self):
        return self._components

    def get_self_id(self):
        return "999"

    def get_message_str(self):
        return self._text

    def get_platform_id(self):
        return "qq"

    def get_sender_id(self):
        return "202"

    def get_sender_name(self):
        return "member"

    def is_private_chat(self):
        return False

    def is_admin(self):
        return self._admin


def _plugin(*, natural: bool = False, scopes=(SCOPE,), capacity: int = 4):
    plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
    plugin.config = {"sys001": {
        "ingress": {"group_allowed_scopes": list(scopes)},
        "group": {
            "continuous_window_enabled": True,
            "continuous_window_seconds": 1,
            "continuous_window_max_scopes": capacity,
            "natural_participation_enabled": natural,
            "natural_group_scopes": list(scopes),
            "natural_participation_prompt": "strict json only",
            "natural_decision_timeout_seconds": 1,
            "natural_no_action_backoff_base_seconds": 2,
            "natural_no_action_backoff_max_seconds": 8,
        },
        "final_review": {"mode": "off"},
    }}
    plugin.context = FakeContext()
    plugin.context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=lambda _scope: asyncio.sleep(0, result="c"),
        get_conversation=lambda *_args: asyncio.sleep(0, result=object()),
        new_conversation=lambda *_args, **_kwargs: asyncio.sleep(0, result="c"),
    )
    plugin._natural_generations = {SCOPE: 0}
    plugin._natural_cadence = {SCOPE: SYS001.NaturalCadence.empty()}
    plugin._natural_bindings = {}
    plugin._natural_ready = {SCOPE: True}
    plugin._natural_locks = {}
    plugin._natural_terminated = False
    plugin._auxiliary_epoch = 1
    plugin._auxiliary_terminated = False
    plugin._continuous_scopes = {}
    plugin._continuous_locks = {}
    plugin._continuous_terminated = False
    plugin._batch_scopes = {}
    plugin._batch_scheduler_lock = asyncio.Lock()
    plugin._batch_terminated = False
    return plugin


async def _close_window(plugin, scope: str = SCOPE):
    state = plugin._batch_scopes[scope]
    state.waiting_deadline = asyncio.get_running_loop().time() - 0.01
    state.wakeup.set()


class AmbientBatchingHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_r37_ordinary_text_resets_an_existing_direct_batch_even_without_natural(self):
        """R36 red oracle: an admitted ordinary text cannot bypass direct quieting."""
        plugin = _plugin(natural=False)
        direct = _Inbound("F1-direct", "@bot", directed=True)
        ordinary = _Inbound("F1-ordinary", "ordinary", directed=False)
        first_task = asyncio.create_task(anext(plugin.request_event_bound_reply(direct), None))
        for _ in range(20):
            if plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting:
                break
            await asyncio.sleep(0)
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(ordinary), None))
        for _ in range(20):
            if len(plugin._batch_scopes[SCOPE].waiting) == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(2, len(plugin._batch_scopes[SCOPE].waiting))
        await _close_window(plugin)
        first, second = await asyncio.gather(first_task, second_task)
        self.assertIsNone(first)
        self.assertEqual("ordinary", second.prompt)

    async def test_r37_duplicate_message_id_requires_the_last_real_event_identity(self):
        """R36 red oracle: duplicate ids cannot grant two live watermark owners."""
        plugin = _plugin()
        first = _Inbound("same-id", "first")
        second = _Inbound("same-id", "second")
        tasks = [
            asyncio.create_task(anext(plugin.request_event_bound_reply(event), None))
            for event in (first, second)
        ]
        for _ in range(20):
            if len(plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting) == 2:
                break
            await asyncio.sleep(0)
        await _close_window(plugin)
        results = await asyncio.gather(*tasks)
        self.assertEqual([None, "second"], [None if item is None else item.prompt for item in results])

    async def test_r37_media_boundary_is_immediate_and_text_waits_for_its_official_terminal(self):
        """R36 red oracle: media is not a Shio text batch, but holds its scope."""
        plugin = _plugin()
        image = _Inbound("media-1", "caption", components=[
            SimpleNamespace(type="image", url="https://example.invalid/one.png"),
            SimpleNamespace(type="plain", text="caption"),
        ])
        image_task = asyncio.create_task(anext(plugin.request_event_bound_reply(image), None))
        await asyncio.sleep(0)
        self.assertTrue(image_task.done())
        self.assertIsNone(await image_task)
        state = plugin._batch_scopes[SCOPE]
        self.assertEqual("media-1", state.active_boundary.message_id)
        self.assertFalse(state.waiting)
        later = _Inbound("media-2", "later")
        later_task = asyncio.create_task(anext(plugin.request_event_bound_reply(later), None))
        await asyncio.sleep(0)
        self.assertFalse(later_task.done())
        await plugin.record_standard_send(image)
        await _close_window(plugin)
        self.assertEqual("later", (await later_task).prompt)

    async def test_r37_missing_official_terminal_never_releases_a_second_generation(self):
        """R36 red oracle: private five-minute expiry is no longer a release path."""
        plugin = _plugin()
        first = _Inbound("hold-1", "first")
        first_task = asyncio.create_task(anext(plugin.request_event_bound_reply(first), None))
        for _ in range(20):
            if plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting:
                break
            await asyncio.sleep(0)
        await _close_window(plugin)
        self.assertIsNotNone(await first_task)
        state = plugin._batch_scopes[SCOPE]
        self.assertFalse(hasattr(state, "current_terminal_deadline"))
        later = _Inbound("hold-2", "later")
        later_task = asyncio.create_task(anext(plugin.request_event_bound_reply(later), None))
        await asyncio.sleep(0.02)
        self.assertEqual("hold-1", state.current_watermark_id)
        self.assertFalse(later_task.done())
        later_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await later_task

    async def test_three_real_events_wait_then_only_last_event_owns_one_request(self):
        plugin = _plugin()
        first = _Inbound("A-1", "first")
        second = _Inbound("B-2", "second")
        third = _Inbound("A-3", "third")
        tasks = [asyncio.create_task(anext(plugin.request_event_bound_reply(event), None)) for event in (first, second, third)]
        for _ in range(20):
            if len(plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting) == 3:
                break
            await asyncio.sleep(0)
        self.assertEqual(3, len(plugin._batch_scopes[SCOPE].waiting))
        await _close_window(plugin)
        results = await asyncio.gather(*tasks)
        self.assertEqual([None, None, "third"], [None if value is None else value.prompt for value in results])
        request = results[-1]
        request.system_prompt = ""
        request.func_tool = None
        await plugin.attach_turn_and_project_capabilities(third, request)
        self.assertEqual(2, len(request.contexts))
        batch = [json.loads(item["content"]) for item in request.contexts]
        self.assertEqual(["A-1", "B-2"], [item["platform_message_id"] for item in batch])
        self.assertEqual(["first", "second"], [item["message_text"] for item in batch])

    async def test_master_ordinary_group_message_uses_natural_not_mandatory_entry(self):
        plugin = _plugin(natural=True)
        # The classifier is deliberately not reached until after the batch;
        # this assertion protects the entry/permission boundary itself.
        master = _Inbound("M-1", "ordinary", admin=True, directed=False)
        task = asyncio.create_task(anext(plugin.request_event_bound_reply(master), None))
        await asyncio.sleep(0)
        await _close_window(plugin)
        # No configured Provider means natural classification fails closed.
        self.assertIsNone(await task)
        self.assertFalse(master.get_extra("shio.sys001.batch_mandatory"))

    async def test_any_direct_message_makes_the_whole_batch_one_mandatory_request(self):
        plugin = _plugin(natural=True)
        direct = _Inbound("D-1", "@bot", directed=True)
        ordinary = _Inbound("N-2", "ordinary", directed=False)
        tasks = [
            asyncio.create_task(anext(plugin.request_event_bound_reply(event), None))
            for event in (direct, ordinary)
        ]
        for _ in range(20):
            if len(plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting) == 2:
                break
            await asyncio.sleep(0)
        await _close_window(plugin)
        first, request = await asyncio.gather(*tasks)
        self.assertIsNone(first)
        self.assertEqual("ordinary", request.prompt)
        self.assertTrue(ordinary.get_extra("shio.sys001.batch_mandatory"))
        self.assertEqual("direct", ordinary.get_extra(MAIN.SYS001_TURN_EXTRA).origin)
        self.assertIsNone(ordinary.get_extra("shio.sys001.natural_decision_attempts"))

    async def test_long_current_generation_keeps_two_later_events_as_one_waiting_batch(self):
        plugin = _plugin()
        first = _Inbound("C-1", "first")
        first_task = asyncio.create_task(anext(plugin.request_event_bound_reply(first), None))
        for _ in range(20):
            if plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting:
                break
            await asyncio.sleep(0)
        await _close_window(plugin)
        first_request = await first_task
        self.assertIsNotNone(first_request)
        # The 1-second quiet window has elapsed while AstrBot is still
        # generating the first official request.
        await asyncio.sleep(1.05)
        second = _Inbound("C-2", "second")
        third = _Inbound("C-3", "third")
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
        third_task = asyncio.create_task(anext(plugin.request_event_bound_reply(third), None))
        for _ in range(20):
            if len(plugin._batch_scopes[SCOPE].waiting) == 2:
                break
            await asyncio.sleep(0)
        state = plugin._batch_scopes[SCOPE]
        self.assertEqual("C-1", state.current_watermark_id)
        self.assertEqual(["C-2", "C-3"], [item.snapshot.message_id for item in state.waiting])
        await plugin._batch_mark_terminal(SCOPE, "C-1", state.generation)
        await _close_window(plugin)
        second_result, third_result = await asyncio.gather(second_task, third_task)
        self.assertIsNone(second_result)
        self.assertEqual("third", third_result.prompt)

    async def test_missing_terminal_keeps_arrived_waiting_batch_fail_closed(self):
        plugin = _plugin()
        first = _Inbound("T-1", "first")
        first_task = asyncio.create_task(anext(plugin.request_event_bound_reply(first), None))
        for _ in range(20):
            if plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting:
                break
            await asyncio.sleep(0)
        await _close_window(plugin)
        self.assertIsNotNone(await first_task)
        second = _Inbound("T-2", "second")
        third = _Inbound("T-3", "third")
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
        third_task = asyncio.create_task(anext(plugin.request_event_bound_reply(third), None))
        for _ in range(20):
            if len(plugin._batch_scopes[SCOPE].waiting) == 2:
                break
            await asyncio.sleep(0)
        state = plugin._batch_scopes[SCOPE]
        self.assertTrue(state.current)
        self.assertEqual(["T-2", "T-3"], [item.snapshot.message_id for item in state.waiting])
        await asyncio.sleep(0.02)
        self.assertFalse(second_task.done())
        self.assertFalse(third_task.done())
        await plugin._batch_mark_terminal(SCOPE, "T-1", state.generation)
        await _close_window(plugin)
        second_result, third_result = await asyncio.gather(second_task, third_task)
        self.assertIsNone(second_result)
        self.assertEqual("third", third_result.prompt)

    async def test_official_reply_path_binds_turn_generation_and_after_send_is_idempotent(self):
        plugin = _plugin()
        reply = _Inbound(
            "R-1", "reply text", components=[
                SimpleNamespace(type="reply", id="quoted-1", user_id="101"),
                SimpleNamespace(type="plain", text="reply text"),
            ],
        )
        reply_task = asyncio.create_task(anext(plugin.request_event_bound_reply(reply), None))
        await asyncio.sleep(0)
        self.assertIsNone(await reply_task)
        state = plugin._batch_scopes[SCOPE]
        self.assertEqual("R-1", state.active_boundary.message_id)
        self.assertTrue(reply.is_at_or_wake_command)
        snapshot = reply.get_extra(MAIN.SYS001_TURN_EXTRA)
        lifecycle = reply.get_extra(MAIN.SYS001_LIFECYCLE_EXTRA)
        generation = reply.get_extra("shio.sys001.batch_generation")
        token = reply.get_extra("shio.sys001.batch_watermark_token")
        self.assertIsInstance(snapshot, MAIN.TurnSnapshot)
        self.assertIsInstance(lifecycle, MAIN.TurnLifecycle)
        self.assertEqual("R-1", snapshot.message_id)
        self.assertIsInstance(generation, int)
        self.assertIs(token, state.active_boundary.token)
        self.assertEqual(["reply", "plain"], [item.type for item in reply.get_messages()])
        later = _Inbound("R-2", "later")
        later_task = asyncio.create_task(anext(plugin.request_event_bound_reply(later), None))
        for _ in range(20):
            if len(plugin._batch_scopes[SCOPE].waiting) == 1:
                break
            await asyncio.sleep(0)
        self.assertEqual("R-1", plugin._batch_scopes[SCOPE].active_boundary.message_id)
        self.assertFalse(later_task.done())
        await plugin.record_standard_send(reply)
        self.assertIsNone(plugin._batch_scopes[SCOPE].active_boundary)
        await plugin.record_standard_send(reply)
        self.assertIsNone(plugin._batch_scopes[SCOPE].active_boundary)
        await _close_window(plugin)
        self.assertEqual("later", (await later_task).prompt)

    async def test_r37_current_text_boundary_reservation_orders_later_text_after_media(self):
        """T1 -> M2 -> T3 keeps M2 ahead once T1's official turn ends."""
        plugin = _plugin()
        first = _Inbound("T1", "first")
        first_task = asyncio.create_task(anext(plugin.request_event_bound_reply(first), None))
        for _ in range(20):
            if plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting:
                break
            await asyncio.sleep(0)
        await _close_window(plugin)
        self.assertEqual("first", (await first_task).prompt)

        media = _Inbound("M2", "caption", components=[SimpleNamespace(type="image")])
        media_task = asyncio.create_task(anext(plugin.request_event_bound_reply(media), None))
        await asyncio.sleep(0)
        self.assertFalse(media_task.done())
        state = plugin._batch_scopes[SCOPE]
        self.assertEqual(["M2"], [item.message_id for item in state.boundary_queue])
        self.assertFalse(hasattr(state.boundary_queue[0], "snapshot"))

        later = _Inbound("T3", "later")
        later_task = asyncio.create_task(anext(plugin.request_event_bound_reply(later), None))
        for _ in range(20):
            if len(plugin._batch_scopes[SCOPE].waiting) == 1:
                break
            await asyncio.sleep(0)
        await plugin._batch_mark_terminal(
            SCOPE, "T1", first.get_extra("shio.sys001.batch_generation"),
            first.get_extra("shio.sys001.batch_watermark_token"),
        )
        self.assertIsNone(await media_task)
        self.assertEqual("M2", plugin._batch_scopes[SCOPE].active_boundary.message_id)
        self.assertFalse(later_task.done())
        await plugin.record_standard_send(media)
        await _close_window(plugin)
        self.assertEqual("later", (await later_task).prompt)

    async def test_r37_boundary_cuts_a_quiet_text_window_before_later_text_arrives(self):
        """A boundary closes T1's prior quiet batch; T3 cannot join it."""
        plugin = _plugin()
        first = _Inbound("Q1", "first")
        first_task = asyncio.create_task(anext(plugin.request_event_bound_reply(first), None))
        for _ in range(20):
            if plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting:
                break
            await asyncio.sleep(0)
        media = _Inbound("Q2", "caption", components=[SimpleNamespace(type="image")])
        media_task = asyncio.create_task(anext(plugin.request_event_bound_reply(media), None))
        for _ in range(20):
            state = plugin._batch_scopes[SCOPE]
            if state.current and state.boundary_queue:
                break
            await asyncio.sleep(0)
        state = plugin._batch_scopes[SCOPE]
        self.assertEqual(["Q1"], [item.snapshot.message_id for item in state.current])
        self.assertEqual(["Q2"], [item.message_id for item in state.boundary_queue])
        self.assertEqual("first", (await first_task).prompt)

        later = _Inbound("Q3", "later")
        later_task = asyncio.create_task(anext(plugin.request_event_bound_reply(later), None))
        for _ in range(20):
            if len(plugin._batch_scopes[SCOPE].waiting) == 1:
                break
            await asyncio.sleep(0)
        self.assertEqual(["Q3"], [item.snapshot.message_id for item in state.waiting])
        await plugin._batch_mark_terminal(
            SCOPE, "Q1", first.get_extra("shio.sys001.batch_generation"),
            first.get_extra("shio.sys001.batch_watermark_token"),
        )
        self.assertIsNone(await media_task)
        self.assertFalse(later_task.done())
        await plugin.record_standard_send(media)
        await _close_window(plugin)
        self.assertEqual("later", (await later_task).prompt)

    async def test_r37_consecutive_boundaries_keep_real_arrival_order(self):
        plugin = _plugin()
        first = _Inbound("B1", "image", components=[SimpleNamespace(type="image")])
        second = _Inbound("B2", "reply", components=[SimpleNamespace(type="reply")])
        self.assertIsNone(await anext(plugin.request_event_bound_reply(first), None))
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
        await asyncio.sleep(0)
        state = plugin._batch_scopes[SCOPE]
        self.assertEqual("B1", state.active_boundary.message_id)
        self.assertEqual(["B2"], [item.message_id for item in state.boundary_queue])
        await plugin.record_standard_send(first)
        self.assertIsNone(await second_task)
        self.assertEqual("B2", plugin._batch_scopes[SCOPE].active_boundary.message_id)
        await plugin.record_standard_send(second)
        self.assertNotIn(SCOPE, plugin._batch_scopes)

    async def test_r37_boundary_without_terminal_keeps_later_text_closed_until_reload(self):
        plugin = _plugin()
        media = _Inbound("F1", "image", components=[SimpleNamespace(type="image")])
        self.assertIsNone(await anext(plugin.request_event_bound_reply(media), None))
        later = _Inbound("F2", "later")
        later_task = asyncio.create_task(anext(plugin.request_event_bound_reply(later), None))
        await asyncio.sleep(0.02)
        self.assertFalse(later_task.done())
        self.assertEqual("F1", plugin._batch_scopes[SCOPE].active_boundary.message_id)
        await plugin.terminate()
        self.assertIsNone(await later_task)


class R38BoundaryFenceRegressions(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_waiting(self, plugin, scope: str = SCOPE):
        for _ in range(20):
            if plugin._batch_scopes.get(scope, SimpleNamespace(waiting=())).waiting:
                return
            await asyncio.sleep(0)
        self.fail(f"no text waiting batch for {scope}")

    async def test_r38_text_terminal_keeps_queued_boundary_ahead_of_later_text(self):
        """T1 terminal cannot discard M2's fence before T3 actually arrives."""
        plugin = _plugin()
        plugin.config["sys001"]["group"]["continuous_window_seconds"] = 1
        first = _Inbound("R38-T1", "first")
        first_stream = plugin.request_event_bound_reply(first)
        first_task = asyncio.create_task(anext(first_stream, None))
        await self._wait_for_waiting(plugin)
        self.assertEqual("first", (await asyncio.wait_for(first_task, timeout=1.5)).prompt)
        self.assertEqual("R38-T1", plugin._batch_scopes[SCOPE].current_watermark_id)

        media = _Inbound("R38-M2", "image", components=[SimpleNamespace(type="image")])
        media_task = asyncio.create_task(anext(plugin.request_event_bound_reply(media), None))
        for _ in range(20):
            if plugin._batch_scopes[SCOPE].boundary_queue:
                break
            await asyncio.sleep(0)
        self.assertEqual((), plugin._batch_scopes[SCOPE].waiting)
        await plugin._batch_mark_terminal(
            SCOPE, "R38-T1", first.get_extra("shio.sys001.batch_generation"),
            first.get_extra("shio.sys001.batch_watermark_token"),
        )
        self.assertIn(SCOPE, plugin._batch_scopes)
        self.assertTrue(
            plugin._batch_scopes[SCOPE].active_boundary is not None
            or plugin._batch_scopes[SCOPE].boundary_queue
        )
        self.assertIsNone(await asyncio.wait_for(media_task, timeout=0.5))
        self.assertEqual("R38-M2", plugin._batch_scopes[SCOPE].active_boundary.message_id)

        later = _Inbound("R38-T3", "later")
        later_stream = plugin.request_event_bound_reply(later)
        later_task = asyncio.create_task(anext(later_stream, None))
        await self._wait_for_waiting(plugin)
        self.assertFalse(later_task.done())
        await plugin.record_standard_send(media)
        self.assertEqual("later", (await asyncio.wait_for(later_task, timeout=1.5)).prompt)

    async def test_r38_capacity_waits_then_fences_its_later_text(self):
        """A full B hook waits for a real slot before its official boundary runs."""
        scope_a = "qq:group:R38-A"
        scope_b = "qq:group:R38-B"
        plugin = _plugin(scopes=(scope_a, scope_b), capacity=1)
        first = _Inbound("R38-A1", "first", scope=scope_a)
        first_stream = plugin.request_event_bound_reply(first)
        first_task = asyncio.create_task(anext(first_stream, None))
        await self._wait_for_waiting(plugin, scope_a)
        await _close_window(plugin, scope_a)
        self.assertEqual("first", (await first_task).prompt)

        media = _Inbound(
            "R38-B1", "image", scope=scope_b,
            components=[SimpleNamespace(type="image")],
        )
        media_stream = plugin.request_event_bound_reply(media)
        media_task = asyncio.create_task(anext(media_stream, None))
        for _ in range(20):
            if getattr(plugin, "_batch_capacity_event", None) is not None:
                break
            await asyncio.sleep(0)
        self.assertFalse(media_task.done())
        self.assertNotIn(scope_b, plugin._batch_scopes)
        self.assertLessEqual(len(plugin._batch_scopes), 1)
        for _ in range(3):
            plugin._batch_capacity_wakeup().set()
            await asyncio.sleep(0)
            self.assertFalse(media_task.done())
            self.assertNotIn(scope_b, plugin._batch_scopes)
            self.assertLessEqual(len(plugin._batch_scopes), 1)
        await plugin._batch_mark_terminal(
            scope_a, "R38-A1", first.get_extra("shio.sys001.batch_generation"),
            first.get_extra("shio.sys001.batch_watermark_token"),
        )
        self.assertIsNone(await asyncio.wait_for(media_task, timeout=0.5))
        self.assertEqual("R38-B1", plugin._batch_scopes[scope_b].active_boundary.message_id)
        self.assertLessEqual(len(plugin._batch_scopes), 1)

        later = _Inbound("R38-B2", "later", scope=scope_b)
        later_stream = plugin.request_event_bound_reply(later)
        later_task = asyncio.create_task(anext(later_stream, None))
        await self._wait_for_waiting(plugin, scope_b)
        self.assertFalse(later_task.done())
        await plugin.record_standard_send(media)
        await _close_window(plugin, scope_b)
        self.assertEqual("later", (await later_task).prompt)

    async def test_r38_capacity_still_rejects_a_new_text_scope_without_boundary(self):
        """Boundary safety does not silently admit an unrelated extra text scope."""
        scope_a = "qq:group:R38-text-A"
        scope_b = "qq:group:R38-text-B"
        plugin = _plugin(scopes=(scope_a, scope_b), capacity=1)
        scheduler_lock = plugin._batch_scheduler_lock
        first = _Inbound("R38-text-A1", "first", scope=scope_a)
        first_stream = plugin.request_event_bound_reply(first)
        first_task = asyncio.create_task(anext(first_stream, None))
        await self._wait_for_waiting(plugin, scope_a)
        await _close_window(plugin, scope_a)
        self.assertEqual("first", (await first_task).prompt)

        other = _Inbound("R38-text-B1", "other", scope=scope_b)
        self.assertIsNone(await anext(plugin.request_event_bound_reply(other), None))
        self.assertNotIn(scope_b, plugin._batch_scopes)
        self.assertFalse(hasattr(plugin, "_batch_locks"))
        self.assertIs(scheduler_lock, plugin._batch_scheduler_lock)
        self.assertIsInstance(scheduler_lock, asyncio.Lock)
        await plugin._batch_mark_terminal(
            scope_a, "R38-text-A1", first.get_extra("shio.sys001.batch_generation"),
            first.get_extra("shio.sys001.batch_watermark_token"),
        )

    async def test_r38_capacity_waiter_cancellation_leaves_no_boundary_state(self):
        scope_a = "qq:group:R38-cancel-A"
        scope_b = "qq:group:R38-cancel-B"
        plugin = _plugin(scopes=(scope_a, scope_b), capacity=1)
        first = _Inbound("R38-cancel-A1", "first", scope=scope_a)
        first_stream = plugin.request_event_bound_reply(first)
        first_task = asyncio.create_task(anext(first_stream, None))
        await self._wait_for_waiting(plugin, scope_a)
        await _close_window(plugin, scope_a)
        self.assertEqual("first", (await first_task).prompt)
        media = _Inbound("R38-cancel-B1", "image", scope=scope_b, components=[SimpleNamespace(type="image")])
        media_task = asyncio.create_task(anext(plugin.request_event_bound_reply(media), None))
        await asyncio.sleep(0)
        media_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await media_task
        self.assertNotIn(scope_b, plugin._batch_scopes)
        await plugin._batch_mark_terminal(
            scope_a, "R38-cancel-A1", first.get_extra("shio.sys001.batch_generation"),
            first.get_extra("shio.sys001.batch_watermark_token"),
        )

    async def test_r38_capacity_waiter_exits_on_reload_without_boundary_state(self):
        scope_a = "qq:group:R38-reload-A"
        scope_b = "qq:group:R38-reload-B"
        plugin = _plugin(scopes=(scope_a, scope_b), capacity=1)
        first = _Inbound("R38-reload-A1", "first", scope=scope_a)
        first_stream = plugin.request_event_bound_reply(first)
        first_task = asyncio.create_task(anext(first_stream, None))
        await self._wait_for_waiting(plugin, scope_a)
        await _close_window(plugin, scope_a)
        self.assertEqual("first", (await first_task).prompt)
        media = _Inbound("R38-reload-B1", "image", scope=scope_b, components=[SimpleNamespace(type="image")])
        media_task = asyncio.create_task(anext(plugin.request_event_bound_reply(media), None))
        await asyncio.sleep(0)
        await plugin.terminate()
        self.assertIsNone(await media_task)
        self.assertEqual({}, plugin._batch_scopes)

    async def test_r39_capacity_waiters_never_create_scope_lock_keys(self):
        scope_a = "qq:group:R39-A"
        blocked = ("qq:group:R39-B", "qq:group:R39-C", "qq:group:R39-D")
        plugin = _plugin(scopes=(scope_a, *blocked), capacity=1)
        plugin.config["sys001"]["group"]["continuous_window_enabled"] = False
        first = _Inbound("R39-A1", "first", scope=scope_a)
        first_stream = plugin.request_event_bound_reply(first)
        first_task = asyncio.create_task(anext(first_stream, None))
        self.assertEqual("first", (await asyncio.wait_for(first_task, timeout=0.5)).prompt)
        scheduler_lock = plugin._batch_scheduler_lock
        task_events = {}
        for index, scope in enumerate(blocked, start=1):
            event = _Inbound(f"R39-{index}", "image", scope=scope, components=[SimpleNamespace(type="image")])
            task = asyncio.create_task(anext(plugin.request_event_bound_reply(event), None))
            task_events[task] = event
        await asyncio.sleep(0)
        self.assertEqual({scope_a}, set(plugin._batch_scopes))
        self.assertFalse(hasattr(plugin, "_batch_locks"))
        self.assertIs(scheduler_lock, plugin._batch_scheduler_lock)
        self.assertTrue(all(not task.done() for task in task_events))

        await plugin.record_standard_send(first)
        pending = set(task_events)
        for remaining in range(len(blocked), 0, -1):
            done, pending = await asyncio.wait(
                pending, timeout=0.5, return_when=asyncio.FIRST_COMPLETED,
            )
            self.assertEqual(1, len(done))
            winner_task = done.pop()
            winner = task_events[winner_task]
            self.assertIsNone(winner_task.result())
            self.assertLessEqual(len(plugin._batch_scopes), 1)
            self.assertEqual(1, sum(
                state.active_boundary is not None
                for state in plugin._batch_scopes.values()
            ))
            self.assertFalse(hasattr(plugin, "_batch_locks"))
            self.assertIs(scheduler_lock, plugin._batch_scheduler_lock)
            self.assertEqual(remaining - 1, len(pending))
            self.assertTrue(all(not task.done() for task in pending))
            await plugin.record_standard_send(winner)
        self.assertEqual({}, plugin._batch_scopes)
        self.assertFalse(hasattr(plugin, "_batch_locks"))


class NaturalDecisionUnavailableAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_unavailable_exit_is_a_redacted_event_local_audit(self):
        cases = (
            ("invalid_config", lambda plugin: plugin.config["sys001"]["group"].update(
                natural_decision_provider_id=object()), None),
            ("invalid_timeout", lambda plugin: plugin.config["sys001"]["group"].update(
                natural_decision_timeout_seconds=object()), None),
            ("lookup_deadline", lambda plugin: setattr(
                plugin, "_auxiliary_providers", lambda *_args, **_kwargs: asyncio.sleep(
                    0, result=MAIN._AUXILIARY_DEADLINE_EXHAUSTED)), None),
            ("stale_lookup", lambda plugin: setattr(
                plugin, "_auxiliary_providers", lambda *_args, **_kwargs: asyncio.sleep(0, result=None)), None),
            ("providers_exhausted", lambda plugin: setattr(
                plugin, "_auxiliary_providers", lambda *_args, **_kwargs: asyncio.sleep(0, result=[])), None),
        )
        for name, configure, _unused in cases:
            with self.subTest(name=name):
                plugin = _plugin(natural=True)
                event = _Inbound(f"A-{name}", "secret body")
                snapshot = MAIN.create_snapshot(event, origin="natural")
                configure(plugin)
                self.assertEqual("", await plugin._decide_natural_participation(event, snapshot))
                attempts = event.get_extra("shio.sys001.natural_decision_attempts")
                self.assertEqual(1, len(attempts))
                self.assertEqual("unavailable", attempts[0]["outcome"])
                self.assertIsInstance(attempts[0]["elapsed_ms"], int)
                self.assertEqual({"outcome", "elapsed_ms"}, set(attempts[0]))
                self.assertNotIn("secret body", json.dumps(attempts))


class NaturalNoActionBackoffTests(unittest.IsolatedAsyncioTestCase):
    async def _run_window(self, plugin, event):
        task = asyncio.create_task(anext(plugin.request_event_bound_reply(event), None))
        for _ in range(20):
            if plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting:
                break
            await asyncio.sleep(0)
        await _close_window(plugin)
        return await task

    async def test_no_action_persists_backoff_blocks_next_batch_then_wait_recovers(self):
        """R37 red oracle: NO_ACTION owns a short D-056 backoff, not a reply."""
        plugin = _plugin(natural=True)
        provider = FakeProvider('{"decision":"NO_ACTION"}', '{"decision":"WAIT"}')
        plugin.context.current = provider
        writes = []

        async def put(key, value):
            writes.append((key, value))

        plugin.put_kv_data = put
        self.assertIsNone(await self._run_window(plugin, _Inbound("N-1", "first", directed=False)))
        self.assertEqual(["pending", "committed"], [value["status"] for _key, value in writes])
        state = plugin._natural_cadence[SCOPE]
        self.assertEqual((0.0, (), 1), (state.last_completed_at, state.completed_at, state.no_action_streak))
        self.assertGreater(state.backoff_until, 0.0)

        self.assertIsNone(await self._run_window(plugin, _Inbound("N-2", "blocked", directed=False)))
        self.assertEqual(1, len(provider.calls))
        self.assertEqual(2, len(writes))

        with patch.object(MAIN.time, "time", return_value=state.backoff_until + 1.0):
            self.assertIsNone(await self._run_window(plugin, _Inbound("N-3", "expired", directed=False)))
        self.assertEqual(2, len(provider.calls))
        self.assertEqual(2, len(writes))
        self.assertEqual((0.0, ()), (
            plugin._natural_cadence[SCOPE].last_completed_at,
            plugin._natural_cadence[SCOPE].completed_at,
        ))

    async def test_no_action_does_not_release_the_serial_owner_before_kv_commit(self):
        """The next text stays waiting while the NO_ACTION transaction is pending."""
        plugin = _plugin(natural=True)
        plugin.context.current = FakeProvider('{"decision":"NO_ACTION"}')
        pending_started = asyncio.Event()
        release_pending = asyncio.Event()
        writes = []

        async def put(key, value):
            writes.append((key, value))
            if len(writes) == 1:
                pending_started.set()
                await release_pending.wait()

        plugin.put_kv_data = put
        first = _Inbound("S-1", "first", directed=False)
        first_task = asyncio.create_task(anext(plugin.request_event_bound_reply(first), None))
        for _ in range(20):
            if plugin._batch_scopes.get(SCOPE, SimpleNamespace(waiting=())).waiting:
                break
            await asyncio.sleep(0)
        await _close_window(plugin)
        await asyncio.wait_for(pending_started.wait(), timeout=0.5)
        second = _Inbound("S-2", "later", directed=False)
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
        for _ in range(20):
            if len(plugin._batch_scopes[SCOPE].waiting) == 1:
                break
            await asyncio.sleep(0)
        self.assertEqual("S-1", plugin._batch_scopes[SCOPE].current_watermark_id)
        self.assertFalse(second_task.done())
        release_pending.set()
        self.assertIsNone(await first_task)
        await _close_window(plugin)
        self.assertIsNone(await second_task)


class AmbientHistoryBoundaryTests(unittest.TestCase):
    @staticmethod
    def _record(row_id: int, when: datetime, text: str):
        return SimpleNamespace(
            id=row_id,
            created_at=when,
            sender_id="202",
            sender_name="member",
            content={"type": "user", "message": [{"type": "plain", "text": text}]},
        )

    def test_watermark_excludes_current_and_later_platform_rows(self):
        now = datetime(2024, 1, 1, tzinfo=UTC)
        contexts = SYS001.project_official_group_history(
            [
                self._record(10, now, "earlier"),
                self._record(11, now + timedelta(seconds=1), "current"),
                self._record(12, now + timedelta(seconds=2), "future"),
            ],
            blocked_sender_ids=frozenset(),
            current_history_row_id=11,
            watermark_history_row_id=11,
        )
        self.assertEqual(1, len(contexts))
        self.assertIn("earlier", contexts[0]["content"])
        self.assertNotIn("future", contexts[0]["content"])

    def test_batch_history_row_ids_are_excluded_instead_of_represented_twice(self):
        now = datetime(2024, 1, 1, tzinfo=UTC)
        contexts = SYS001.project_official_group_history(
            [
                self._record(9, now, "older history"),
                self._record(10, now, "batch first"),
                self._record(11, now, "batch watermark"),
            ],
            blocked_sender_ids=frozenset(),
            current_history_row_id=11,
            watermark_history_row_id=11,
            excluded_history_row_ids=frozenset({10, 11}),
        )
        self.assertEqual(1, len(contexts))
        self.assertIn("older history", contexts[0]["content"])


class R35RedEvidenceTests(unittest.TestCase):
    def test_frozen_r35_candidate_has_the_three_replaced_r36_paths(self):
        """Keep the de-identified red evidence outside the mutable worktree."""
        self.assertEqual(R35_CANDIDATE_SHA256, hashlib.sha256(R35_CANDIDATE.read_bytes()).hexdigest())
        with zipfile.ZipFile(R35_CANDIDATE) as archive:
            source = archive.read("astrbot_plugin_shio/main.py").decode("utf-8")
        self.assertIn("elif provisional.is_master", source)
        self.assertIn('EntryDecision("request", "master"', source)
        self.assertIn('return "immediate"', source)
        self.assertNotIn("watermark_history_row_id", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
