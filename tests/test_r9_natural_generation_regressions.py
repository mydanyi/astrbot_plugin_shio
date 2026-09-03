"""R9 regressions for overlapping natural pre-Agent candidates.

The oracle drives the plugin's public event-bound request hook and standard
response/after-send hooks.  It asserts externally visible ownership: a
non-REPLY candidate never gets a main request, while an already requested E1
still receives review and exactly one RespondStage cadence commit.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

try:
    from test_name_semantic_provider_routing import FakeContext, FakeProvider, MAIN, SYS001
    from test_r7_reviewer_regressions import _Event
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        FakeContext, FakeProvider, MAIN, SYS001,
    )
    from astrbot_plugin_shio.tests.test_r7_reviewer_regressions import _Event


SCOPE = "qq:group:202"


def _event(message_id: str, text: str) -> _Event:
    event = _Event(text)
    event.message_obj = SimpleNamespace(message_id=message_id)
    return event


def _plugin(classifier: FakeProvider):
    plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
    plugin.context = FakeContext(current=classifier)
    plugin.config = {"sys001": {
        "ingress": {"group_allowed_scopes": [SCOPE]},
        "group": {
            "natural_participation_enabled": True,
            "natural_group_scopes": [SCOPE],
            "continuous_window_enabled": False,
            "natural_reply_cooldown_seconds": 45,
            "natural_frequency_window_minutes": 5,
            "natural_max_replies_per_window": 2,
            "natural_no_action_backoff_base_seconds": 2,
            "natural_no_action_backoff_max_seconds": 30,
        },
        "final_review": {"mode": "off"},
    }}
    plugin._auxiliary_epoch = 1
    plugin._auxiliary_terminated = False
    plugin._natural_generations = {SCOPE: 0}
    plugin._natural_cadence = {SCOPE: SYS001.NaturalCadence.empty()}
    plugin._natural_bindings = {}
    plugin._natural_ready = {SCOPE: True}
    plugin._natural_locks = {}
    plugin._natural_terminated = False
    plugin._continuous_scopes = {}
    plugin._continuous_locks = {}
    plugin._continuous_terminated = False
    plugin.context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=lambda _scope: asyncio.sleep(0, result="c"),
        get_conversation=lambda *_args: asyncio.sleep(0, result=object()),
        new_conversation=lambda *_args, **_kwargs: asyncio.sleep(0, result="c"),
    )
    store = {}

    async def put(key, value):
        store[key] = value

    plugin.put_kv_data = put
    return plugin, store


class R9ConcurrentNaturalCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_nonreply_pair_preserves_e1(self, second: str, third: str):
        def outcome(decision: str, delay: float):
            if decision == "timeout":
                return ("sleep", 0.02, '{"decision":"WAIT"}')
            if decision == "invalid":
                return ("sleep", delay, "not-json")
            return ("sleep", delay, '{"decision":"%s"}' % decision)

        classifier = FakeProvider(
            '{"decision":"REPLY"}',
            outcome(second, 0.001),
            outcome(third, 0.002),
        )
        plugin, store = _plugin(classifier)
        first = _event("E1", "first natural")
        first_request = await anext(plugin.request_event_bound_reply(first))
        self.assertEqual("first natural", first_request.prompt)
        # The short budget belongs only to the later E2/E3 timeout oracle.
        # E1 already owns an official main request, which is the lifecycle
        # guarantee this test protects; applying 5ms before E1 instead made
        # IsolatedAsyncio debug scheduling part of the asserted contract.
        if "timeout" in {second, third}:
            plugin.config["sys001"]["group"]["natural_decision_timeout_seconds"] = 0.005

        # R37 keeps E2/E3 behind the live text-batch owner. They cannot begin
        # auxiliary classification while E1 has no official terminal.
        second_event = _event("E2", "second natural")
        third_event = _event("E3", "third natural")
        second_task = asyncio.create_task(
            anext(plugin.request_event_bound_reply(second_event), None)
        )
        await asyncio.sleep(0)
        third_task = asyncio.create_task(
            anext(plugin.request_event_bound_reply(third_event), None)
        )
        self.assertFalse(second_task.done())
        self.assertFalse(third_task.done())

        response = SimpleNamespace(
            role="assistant", completion_text="E1 visible answer", reasoning_content=""
        )
        await plugin.observe_final_agent_response(first, response)
        self.assertEqual("E1 visible answer", response.completion_text)
        self.assertIsNotNone(first.get_extra("shio.sys001.natural_reply_pending"))
        await plugin.record_standard_send(first)
        await plugin.record_standard_send(first)

        self.assertIsNone(await second_task)
        self.assertIsNone(await third_task)

        record = store[plugin._natural_kv_key(SCOPE)]
        self.assertEqual(1, len(record["completed_at"]))
        self.assertTrue(first.get_extra("shio.sys001.natural_stage_committed"))
        self.assertEqual([], second_event.requests)
        self.assertEqual([], third_event.requests)

    async def test_e2_then_e3_waits_do_not_cancel_started_e1(self):
        await self._assert_nonreply_pair_preserves_e1("WAIT", "WAIT")

    async def test_no_action_invalid_and_timeout_do_not_cancel_started_e1(self):
        for second, third in (
            ("NO_ACTION", "WAIT"), ("invalid", "WAIT"),
            ("WAIT", "invalid"), ("timeout", "WAIT"),
        ):
            with self.subTest(second=second, third=third):
                await self._assert_nonreply_pair_preserves_e1(second, third)

    async def test_true_later_reply_still_fences_e1_cadence(self):
        classifier = FakeProvider(
            '{"decision":"REPLY"}', '{"decision":"REPLY"}',
            '{"decision":"REPLY"}',
        )
        plugin, store = _plugin(classifier)
        first = _event("E1", "first natural")
        second = _event("E2", "second natural")
        third = _event("E3", "third natural")
        self.assertIsNotNone(await anext(plugin.request_event_bound_reply(first), None))
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
        third_task = asyncio.create_task(anext(plugin.request_event_bound_reply(third), None))
        await asyncio.sleep(0)
        self.assertFalse(second_task.done())
        self.assertFalse(third_task.done())
        await plugin.observe_final_agent_response(first, SimpleNamespace(
            role="assistant", completion_text="old visible", reasoning_content=""
        ))
        await plugin.record_standard_send(first)
        self.assertTrue(first.get_extra("shio.sys001.natural_stage_committed", False))
        self.assertIsNone(await second_task)
        self.assertIsNone(await third_task)

    async def test_cancel_and_epoch_reload_retire_only_the_pending_candidate(self):
        cancelled, store = _plugin(FakeProvider(asyncio.CancelledError()))
        event = _event("cancel", "cancel natural")
        with self.assertRaises(asyncio.CancelledError):
            await anext(cancelled.request_event_bound_reply(event), None)
        self.assertEqual(0, cancelled._natural_generations[SCOPE])
        self.assertEqual({}, store)

        slow, store = _plugin(FakeProvider(("sleep", 0.02, '{"decision":"REPLY"}')))
        event = _event("reload", "reload natural")
        task = asyncio.create_task(anext(slow.request_event_bound_reply(event), None))
        await asyncio.sleep(0)
        slow._auxiliary_epoch += 1
        self.assertIsNone(await task)
        self.assertEqual(0, slow._natural_generations[SCOPE])
        self.assertEqual([], event.requests)
        self.assertEqual({}, store)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
