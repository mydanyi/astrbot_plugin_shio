"""R6 behavioral regressions for auxiliary-only work at the AstrBot boundary.

The fixtures use the public event/Context/Provider shapes exercised by the
plugin test loader; they deliberately assert visible call ownership rather
than reproduce the implementation's routing algorithm.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest

try:  # unittest discovery imports this file as a top-level module.
    from test_name_semantic_provider_routing import (
        FakeContext, FakeEvent, FakeProvider, MAIN, SYS001,
    )
except ImportError:  # Direct module execution keeps the package form useful.
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        FakeContext, FakeEvent, FakeProvider, MAIN, SYS001,
    )


class _Event(FakeEvent):
    def __init__(self, text="hello", *, private=False, at=False, reply_to_self=False):
        super().__init__(text)
        self.extras = {}
        self.private = private
        self.is_at_or_wake_command = at
        self.reply_to_self = reply_to_self
        self.message_obj = SimpleNamespace(message_id="r6-message")
        self.requests = []

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def is_private_chat(self):
        return self.private

    def should_call_llm(self, _value):
        return None

    def request_llm(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(**kwargs)


def _snapshot(origin="natural"):
    return MAIN.TurnSnapshot(
        platform_id="qq", scope="qq:group:202", account_id="999", sender_id="202",
        sender_name="member", self_id="999", message_id="r6-message",
        created_at=datetime(2024, 1, 1, tzinfo=UTC), message_text="ordinary group text",
        is_private=False, is_master=False, at_self=False, reply_to_self=False, origin=origin,
    )


class R6AuxiliaryProviderTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, provider, *, group=None):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.context = FakeContext(current=provider)
        plugin.config = {"sys001": {"group": group or {}}}
        plugin._auxiliary_epoch = 1
        plugin._auxiliary_terminated = False
        plugin._natural_generations = {"qq:group:202": 1}
        return plugin

    async def test_natural_gate_uses_tool_free_strict_provider_decision(self):
        provider = FakeProvider('{"decision":"REPLY"}')
        plugin = self.plugin(provider)
        event = _Event()
        event.set_extra("shio.sys001.generation", 1)

        decision = await plugin._decide_natural_participation(event, _snapshot())

        self.assertEqual("REPLY", decision)
        self.assertEqual(1, len(provider.calls))
        self.assertIsNone(provider.calls[0]["func_tool"])
        self.assertNotIn("conversation", provider.calls[0]["prompt"])

    async def test_natural_malformed_or_late_result_is_fail_closed(self):
        provider = FakeProvider("not-json")
        plugin = self.plugin(provider)
        event = _Event()
        event.set_extra("shio.sys001.generation", 1)
        self.assertEqual("", await plugin._decide_natural_participation(event, _snapshot()))

        slow = FakeProvider(("sleep", 0.02, '{"decision":"REPLY"}'))
        plugin = self.plugin(slow)
        event = _Event()
        event.set_extra("shio.sys001.generation", 1)
        task = asyncio.create_task(plugin._decide_natural_participation(event, _snapshot()))
        await asyncio.sleep(0)
        plugin._natural_generations["qq:group:202"] = 2
        self.assertEqual("", await task)

    async def test_auxiliary_epoch_fences_model_segments_without_text_rewrite(self):
        provider = FakeProvider(("sleep", 0.02, '{"segments":["hello"," world"]}'))
        plugin = self.plugin(provider)
        event = _Event()
        task = asyncio.create_task(plugin._model_text_components(
            event, "hello world", {"text_component_max_segments": 2, "model_segment_timeout_seconds": 1},
            snapshot=_snapshot(origin="direct"),
        ))
        await asyncio.sleep(0)
        plugin._auxiliary_terminated = True
        self.assertEqual(["hello world"], await task)

    async def test_name_and_review_late_results_are_ignored_after_epoch_termination(self):
        name_provider = FakeProvider(("sleep", 0.02, '{"decision":"DIRECT"}'))
        plugin = self.plugin(name_provider, group={"name_wake_mode": "semantic"})
        name_event = _Event("Shio, late")
        name_task = asyncio.create_task(plugin._visible_name_wake(name_event))
        await asyncio.sleep(0)
        plugin._auxiliary_terminated = True
        self.assertFalse(await name_task)

        review_provider = FakeProvider(("sleep", 0.02, '{"action":"keep"}'))
        plugin = self.plugin(review_provider)
        plugin.config["sys001"]["final_review"] = {
            "mode": "core", "timeout_seconds": 1, "max_repair_attempts": 1,
            "provider_id": "", "fallback_provider_ids": [],
        }
        review_event = _Event()
        review_event.set_extra("shio.sys001.generation", None)
        review_task = asyncio.create_task(plugin._review_final_text(
            review_event, _snapshot(origin="direct"), "unchanged"
        ))
        await asyncio.sleep(0)
        plugin._auxiliary_epoch += 1
        outcome = await review_task
        self.assertTrue(outcome.stale)
        self.assertEqual("unchanged", outcome.text)

    async def test_forced_private_at_and_reply_paths_skip_semantic_provider(self):
        for private, at, reply in ((True, False, False), (False, True, False), (False, False, True)):
            with self.subTest(private=private, at=at, reply=reply):
                provider = FakeProvider(asyncio.CancelledError())
                plugin = self.plugin(provider, group={"name_wake_mode": "semantic"})
                plugin.config["sys001"]["ingress"] = {
                    "private_allowed_sender_ids": ["202"],
                    "group_allowed_scopes": ["qq:group:202"],
                }
                plugin.context.conversation_manager = SimpleNamespace(
                    get_curr_conversation_id=lambda _scope: asyncio.sleep(0, result="c"),
                    get_conversation=lambda *_args: asyncio.sleep(0, result=object()),
                    new_conversation=lambda *_args, **_kwargs: asyncio.sleep(0, result="c"),
                )
                plugin._continuous_scopes = {}
                plugin._continuous_locks = {}
                plugin._continuous_terminated = False
                event = _Event("Shio semantic-looking text", private=private, at=at, reply_to_self=reply)
                if reply:
                    # The official snapshot reads the Reply component, not this
                    # convenience flag; recorded shape is supplied below.
                    event.get_messages = lambda: [SimpleNamespace(id="old", qq=0, sender_id="999", time=1)]
                request = await anext(plugin.request_event_bound_reply(event))
                self.assertEqual("Shio semantic-looking text", request.prompt)
                self.assertEqual([], provider.calls)

    async def test_only_reply_natural_decision_reaches_the_standard_event_request(self):
        for classifier, expected_requests in (("REPLY", 1), ("WAIT", 0), ("NO_ACTION", 0)):
            with self.subTest(classifier=classifier):
                provider = FakeProvider('{"decision":"%s"}' % classifier)
                plugin = self.plugin(provider, group={
                    "name_wake_mode": "semantic", "natural_participation_enabled": True,
                    "natural_group_scopes": ["qq:group:202"], "continuous_window_enabled": False,
                })
                plugin.config["sys001"]["ingress"] = {"group_allowed_scopes": ["qq:group:202"]}
                plugin.context.conversation_manager = SimpleNamespace(
                    get_curr_conversation_id=lambda _scope: asyncio.sleep(0, result="c"),
                    get_conversation=lambda *_args: asyncio.sleep(0, result=object()),
                    new_conversation=lambda *_args, **_kwargs: asyncio.sleep(0, result="c"),
                )
                plugin._natural_cadence = {"qq:group:202": MAIN.NaturalCadence.empty()}
                plugin._natural_ready = {"qq:group:202": True}
                plugin._natural_bindings = {}
                plugin._natural_locks = {}
                plugin._natural_terminated = False
                plugin._continuous_scopes = {}
                plugin._continuous_locks = {}
                plugin._continuous_terminated = False
                event = _Event("ordinary group text")
                if classifier == "NO_ACTION":
                    plugin.put_kv_data = lambda *_args: asyncio.sleep(0)
                requests = [item async for item in plugin.request_event_bound_reply(event)]
                self.assertEqual(expected_requests, len(requests))
                self.assertEqual(1, len(provider.calls))
                self.assertIsNone(provider.calls[0]["func_tool"])


class R6HistoryAndStripTests(unittest.IsolatedAsyncioTestCase):
    def test_current_official_history_row_is_excluded_without_content_guessing(self):
        now = datetime(2024, 1, 1, tzinfo=UTC)
        row = lambda ident, content: SimpleNamespace(
            id=ident, created_at=now, sender_id="202", sender_name="member",
            content={"type": "user", "message": [{"type": "plain", "text": content}]},
        )
        contexts = SYS001.project_official_group_history(
            [row(10, "past"), row(11, "current")],
            blocked_sender_ids=frozenset(), current_history_row_id=11,
        )
        self.assertEqual(1, len(contexts))
        self.assertIn("past", contexts[0]["content"])
        self.assertNotIn("current", contexts[0]["content"])

    async def test_group_history_hook_uses_official_current_row_id_not_text_matching(self):
        now = datetime(2024, 1, 1, tzinfo=UTC)
        records = [
            SimpleNamespace(id=41, created_at=now, sender_id="202", sender_name="member",
                content={"type": "user", "message": [{"type": "plain", "text": "same text"}]}),
            SimpleNamespace(id=42, created_at=now, sender_id="203", sender_name="member",
                content={"type": "user", "message": [{"type": "plain", "text": "same text"}]}),
        ]
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {"ingress": {}}}
        plugin.context = SimpleNamespace(
            get_config=lambda **_kwargs: {"provider_ltm_settings": {
                "group_message_history_enable": True, "group_icl_enable": False,
                "group_message_history_max_cnt": 50,
            }},
            message_history_manager=SimpleNamespace(get=lambda **_kwargs: asyncio.sleep(0, result=records)),
        )
        event = _Event()
        # builtin_stars writes the real current row through the event extra;
        # equal message text is deliberately not a safe substitute for this ID.
        event.set_extra("_current_platform_message_history_id", 42)
        req = SimpleNamespace(contexts=[{"unsafe": "must be replaced"}])
        await plugin._apply_official_group_history(event, _snapshot(origin="direct"), req)
        self.assertEqual(1, len(req.contexts))
        self.assertIn('"history_row_id":41', req.contexts[0]["content"])
        self.assertNotIn('"history_row_id":42', req.contexts[0]["content"])

    def test_multi_plain_layout_downgrades_when_fixed_downstream_strip_would_change_a_piece(self):
        self.assertFalse(SYS001.text_components_survive_standard_strip(["hello ", "world"]))
        self.assertFalse(SYS001.text_components_survive_standard_strip(["\n  hello", "world"]))
        self.assertTrue(SYS001.text_components_survive_standard_strip(["hello", "world"]))

    async def test_plugin_layout_keeps_one_plain_when_fixed_result_decorate_strip_would_lose_boundary_space(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.config = {"sys001": {"presentation": {
            "text_component_mode": "plugin", "text_component_max_segments": 3,
        }}}
        event = _Event()
        result = SimpleNamespace(chain=[MAIN.Plain("hello。 world")])
        event.get_result = lambda: result
        event.set_extra(MAIN.SYS001_TURN_EXTRA, _snapshot(origin="direct"))
        await plugin.layout_text_components(event)
        self.assertEqual(["hello。 world"], [part.text for part in result.chain])


if __name__ == "__main__":
    unittest.main()
