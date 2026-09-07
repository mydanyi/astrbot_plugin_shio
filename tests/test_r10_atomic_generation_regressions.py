"""R10 actual-hook regressions for ordered natural REPLY activation and KV commit."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

try:
    from test_name_semantic_provider_routing import FakeProvider
    from test_r9_natural_generation_regressions import SCOPE, _event, _plugin
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import FakeProvider
    from astrbot_plugin_shio.tests.test_r9_natural_generation_regressions import (
        SCOPE, _event, _plugin,
    )


def _reviewed_plugin(provider):
    plugin, store = _plugin(provider)
    plugin.config["sys001"]["final_review"] = {
        "mode": "core", "timeout_seconds": 1, "max_repair_attempts": 0,
    }
    return plugin, store


class _PromptBoundProvider:
    """Recorded public Provider shape whose result is bound to each event prompt.

    Concurrent candidate tests must not accidentally depend on task scheduling
    deciding which queued fake response belongs to E2 versus E3.
    """

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    async def text_chat(self, *, prompt, func_tool):
        self.calls.append({"prompt": prompt, "func_tool": func_tool})
        key = "review" if prompt.startswith("Review this final") else (
            "E1" if "MESSAGE:\nfirst" in prompt else
            "E2" if "MESSAGE:\nolder" in prompt else "E3"
        )
        outcome = self.outcomes[key]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, tuple):
            await asyncio.sleep(outcome[1])
            outcome = outcome[2]
        return SimpleNamespace(completion_text=outcome)


class _BarrierPromptProvider(_PromptBoundProvider):
    """Prompt-bound recorded Provider with explicit start/completion barriers."""

    def __init__(self, outcomes):
        super().__init__(outcomes)
        self.started = {name: asyncio.Event() for name in ("E2", "E3")}
        self.release = {name: asyncio.Event() for name in ("E2", "E3")}

    async def text_chat(self, *, prompt, func_tool):
        key = "review" if prompt.startswith("Review this final") else (
            "E1" if "MESSAGE:\nfirst" in prompt else
            "E2" if "MESSAGE:\nolder" in prompt else "E3"
        )
        if key in self.started:
            self.started[key].set()
            await self.release[key].wait()
        return await super().text_chat(prompt=prompt, func_tool=func_tool)


class R10OrderedActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_texts_wait_for_the_current_official_terminal(self):
        provider = _BarrierPromptProvider({
            "E1": '{"decision":"REPLY"}', "E2": '{"decision":"REPLY"}',
            "E3": '{"decision":"REPLY"}', "review": '{"action":"keep"}',
        })
        plugin, store = _reviewed_plugin(provider)
        first, second, third = _event("E1", "first"), _event("E2", "older"), _event("E3", "newer")
        self.assertIsNotNone(await anext(plugin.request_event_bound_reply(first), None))
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
        third_task = asyncio.create_task(anext(plugin.request_event_bound_reply(third), None))
        await asyncio.sleep(0)
        self.assertFalse(second_task.done())
        self.assertFalse(third_task.done())

        await plugin.observe_final_agent_response(first, SimpleNamespace(
            role="assistant", completion_text="E1 visible", reasoning_content=""
        ))
        await plugin.record_standard_send(first)
        record = store[plugin._natural_kv_key(SCOPE)]
        self.assertEqual("committed", record["status"])
        self.assertEqual(1, len(record["completed_at"]))
        self.assertIsNone(await second_task)
        self.assertIsNone(await third_task)

    async def test_later_reply_candidates_cannot_start_before_current_terminal(self):
        provider = _BarrierPromptProvider({
            "E1": '{"decision":"REPLY"}', "E2": '{"decision":"REPLY"}',
            "E3": '{"decision":"REPLY"}', "review": '{"action":"keep"}',
        })
        plugin, _store = _reviewed_plugin(provider)
        first, second, third = _event("E1", "first"), _event("E2", "older"), _event("E3", "newer")
        self.assertIsNotNone(await anext(plugin.request_event_bound_reply(first), None))
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
        third_task = asyncio.create_task(anext(plugin.request_event_bound_reply(third), None))
        await asyncio.sleep(0)
        self.assertFalse(second_task.done())
        self.assertFalse(third_task.done())
        await plugin.observe_final_agent_response(first, SimpleNamespace(
            role="assistant", completion_text="E1 visible", reasoning_content=""
        ))
        await plugin.record_standard_send(first)
        self.assertIsNone(await second_task)
        self.assertIsNone(await third_task)
        self.assertEqual(1, plugin._natural_generations[SCOPE])

    async def _assert_nonreply_pair_keeps_e1_reviewable(
        self, second_outcome, third_outcome, *, timeout: float | None = None
    ):
        """Drive the public hook: E1 is reviewed even while E2/E3 resolve."""
        provider = _PromptBoundProvider({
            "E1": '{"decision":"REPLY"}', "E2": second_outcome,
            "E3": third_outcome, "review": '{"action":"keep"}',
        })
        plugin, store = _reviewed_plugin(provider)
        first, second, third = (
            _event("E1", "first"), _event("E2", "older"), _event("E3", "newer"),
        )
        self.assertIsNotNone(await anext(plugin.request_event_bound_reply(first), None))
        # The short budget belongs to the delayed E2 classifier.  E1 has
        # already entered AstrBot's main path, which is the behavior this
        # regression protects; applying it before E1 accidentally tests local
        # debug-loop scheduling instead of E2 timeout fencing.
        if timeout is not None:
            plugin.config["sys001"]["group"][
                "natural_decision_timeout_seconds"
            ] = timeout
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
        await asyncio.sleep(0)
        third_task = asyncio.create_task(anext(plugin.request_event_bound_reply(third), None))
        self.assertFalse(second_task.done())
        self.assertFalse(third_task.done())
        self.assertEqual([], second.requests)
        self.assertEqual([], third.requests)

        response = SimpleNamespace(
            role="assistant", completion_text="E1 reviewed text", reasoning_content=""
        )
        await plugin.observe_final_agent_response(first, response)
        self.assertEqual("E1 reviewed text", response.completion_text)
        self.assertEqual(
            1,
            sum(call["prompt"].startswith("Review this final") for call in provider.calls),
        )
        await plugin.record_standard_send(first)
        await plugin.record_standard_send(first)
        self.assertIsNone(await second_task)
        self.assertIsNone(await third_task)
        record = store[plugin._natural_kv_key(SCOPE)]
        self.assertEqual(1, len(record["completed_at"]))
        self.assertTrue(first.get_extra("shio.sys001.natural_stage_committed"))

    async def test_nonreply_completion_matrix_keeps_started_e1_review_and_once_cadence(self):
        """R9 only checked this with final review off; keep the real hook on."""
        cases = (
            ('{"decision":"WAIT"}', '{"decision":"WAIT"}', None),
            ('{"decision":"NO_ACTION"}', '{"decision":"WAIT"}', None),
            ("not-json", '{"decision":"WAIT"}', None),
            (("sleep", 0.02, '{"decision":"WAIT"}'), '{"decision":"WAIT"}', 0.005),
        )
        for second, third, timeout in cases:
            with self.subTest(second=second):
                await self._assert_nonreply_pair_keeps_e1_reviewable(
                    second, third, timeout=timeout
                )

    async def test_cancel_and_reload_late_candidates_keep_started_e1_reviewable(self):
        for second_outcome, reload_epoch in (
            (asyncio.CancelledError(), False),
            (("sleep", 0.02, '{"decision":"WAIT"}'), True),
        ):
            with self.subTest(reload_epoch=reload_epoch):
                provider = _PromptBoundProvider({
                    "E1": '{"decision":"REPLY"}', "E2": second_outcome,
                    "E3": '{"decision":"WAIT"}', "review": '{"action":"keep"}',
                })
                plugin, store = _reviewed_plugin(provider)
                first, second, third = (
                    _event("E1", "first"), _event("E2", "older"), _event("E3", "newer"),
                )
                self.assertIsNotNone(await anext(plugin.request_event_bound_reply(first), None))
                second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
                await asyncio.sleep(0)
                third_task = asyncio.create_task(anext(plugin.request_event_bound_reply(third), None))
                self.assertFalse(second_task.done())
                self.assertFalse(third_task.done())
                await plugin.observe_final_agent_response(first, SimpleNamespace(
                    role="assistant", completion_text="E1 still reviewed", reasoning_content=""
                ))
                await plugin.record_standard_send(first)
                self.assertIsNone(await second_task)
                self.assertIsNone(await third_task)
                self.assertEqual(1, len(store[plugin._natural_kv_key(SCOPE)]["completed_at"]))


class R10AtomicCadenceCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_reply_waits_while_old_after_send_persists(self):
        provider = FakeProvider(
            '{"decision":"REPLY"}',
            '{"action":"keep"}',
            ("sleep", 0.01, '{"decision":"REPLY"}'),
        )
        plugin, store = _reviewed_plugin(provider)
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_put(key, value):
            entered.set()
            await release.wait()
            store[key] = value

        plugin.put_kv_data = delayed_put
        first, second = _event("E1", "first"), _event("E2", "second")
        self.assertIsNotNone(await anext(plugin.request_event_bound_reply(first), None))
        await plugin.observe_final_agent_response(first, SimpleNamespace(
            role="assistant", completion_text="E1 reviewed", reasoning_content=""
        ))
        # R37 keeps E2 behind E1's text-batch owner.  Once E1's official
        # RespondStage commits cadence, E2 reaches the gate and is correctly
        # suppressed rather than racing the pending public-KV write.
        second_task = asyncio.create_task(anext(plugin.request_event_bound_reply(second), None))
        await asyncio.sleep(0)
        commit_task = asyncio.create_task(plugin.record_standard_send(first))
        await entered.wait()
        await asyncio.sleep(0.02)
        self.assertFalse(second_task.done())
        self.assertEqual(1, plugin._natural_generations[SCOPE])

        release.set()
        await commit_task
        self.assertIsNone(await second_task)
        self.assertEqual(1, plugin._natural_generations[SCOPE])
        self.assertTrue(first.get_extra("shio.sys001.natural_stage_committed"))
        self.assertEqual(1, len(store[plugin._natural_kv_key(SCOPE)]["completed_at"]))

        await plugin.record_standard_send(first)
        self.assertEqual(1, len(store[plugin._natural_kv_key(SCOPE)]["completed_at"]))

    async def test_kv_exception_never_publishes_or_unblocks_a_stale_commit(self):
        provider = FakeProvider('{"decision":"REPLY"}', '{"action":"keep"}')
        plugin, store = _reviewed_plugin(provider)

        async def failing_put(_key, _value):
            raise TimeoutError("KV timeout")

        plugin.put_kv_data = failing_put
        first = _event("E1", "first")
        self.assertIsNotNone(await anext(plugin.request_event_bound_reply(first), None))
        await plugin.observe_final_agent_response(first, SimpleNamespace(
            role="assistant", completion_text="E1 reviewed", reasoning_content=""
        ))
        await plugin.record_standard_send(first)
        self.assertEqual({}, store)
        self.assertFalse(plugin._natural_ready[SCOPE])
        self.assertFalse(first.get_extra("shio.sys001.natural_stage_committed", False))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
