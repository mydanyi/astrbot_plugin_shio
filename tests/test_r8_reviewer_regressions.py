"""Independent R8 regressions for the R7 reviewer findings."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime

try:
    from test_name_semantic_provider_routing import FakeContext, FakeProvider, MAIN, SYS001
    from test_r7_reviewer_regressions import _Event, _snapshot
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import FakeContext, FakeProvider, MAIN, SYS001
    from astrbot_plugin_shio.tests.test_r7_reviewer_regressions import _Event, _snapshot


def _plugin(context, *, final_review=None):
    plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
    plugin.context = context
    plugin.config = {"sys001": {"final_review": final_review or {}, "group": {}}}
    plugin._auxiliary_epoch = 1
    plugin._auxiliary_terminated = False
    plugin._natural_generations = {"qq:group:202": 1}
    return plugin


class R8ReviewFallbackAndNaturalGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_route_failures_do_not_consume_repair_budget(self):
        bad = FakeProvider(asyncio.TimeoutError())
        healthy = FakeProvider('{"action":"keep"}')
        context = FakeContext(by_id={"bad": bad, "healthy": healthy})
        plugin = _plugin(context, final_review={
            "mode": "core", "timeout_seconds": 1, "max_repair_attempts": 0,
            "provider_id": "bad", "fallback_provider_ids": ["healthy"],
        })
        event = _Event()

        outcome = await plugin._review_final_text(event, _snapshot(origin="direct"), "visible")

        self.assertFalse(outcome.exhausted)
        self.assertEqual("visible", outcome.text)
        self.assertEqual(1, len(bad.calls))
        self.assertEqual(1, len(healthy.calls))

    async def test_review_all_routes_run_even_when_more_than_repair_limit(self):
        first = FakeProvider("invalid")
        middle = FakeProvider(RuntimeError("broken"))
        healthy = FakeProvider('{"action":"keep"}')
        context = FakeContext(by_id={"first": first, "middle": middle, "healthy": healthy})
        plugin = _plugin(context, final_review={
            "mode": "core", "timeout_seconds": 1, "max_repair_attempts": 0,
            "provider_id": "first", "fallback_provider_ids": ["middle", "healthy"],
        })

        outcome = await plugin._review_final_text(_Event(), _snapshot(origin="direct"), "visible")

        self.assertFalse(outcome.exhausted)
        self.assertEqual([1, 1, 1], [len(first.calls), len(middle.calls), len(healthy.calls)])

    async def test_repair_then_rereview_restarts_the_full_route_list(self):
        """A repair cycle is one limit unit; each review gets every route."""
        bad = FakeProvider(asyncio.TimeoutError())
        healthy = FakeProvider(
            '{"action":"replace","text":"repaired"}',
            '{"action":"keep"}',
        )
        context = FakeContext(by_id={"bad": bad, "healthy": healthy})
        plugin = _plugin(context, final_review={
            "mode": "combined", "timeout_seconds": 1, "max_repair_attempts": 1,
            "repair_enabled": True, "use_repaired_text": True,
            "provider_id": "bad", "fallback_provider_ids": ["healthy"],
        })

        outcome = await plugin._review_final_text(
            _Event(), _snapshot(origin="direct"), "original"
        )

        self.assertFalse(outcome.exhausted)
        self.assertEqual("repaired", outcome.text)
        self.assertEqual(2, len(bad.calls))
        self.assertEqual(2, len(healthy.calls))

    async def test_waiting_second_natural_candidate_does_not_stale_started_first_review(self):
        reviewer = FakeProvider('{"action":"keep"}')
        plugin = _plugin(FakeContext(current=reviewer), final_review={
            "mode": "core", "timeout_seconds": 1, "max_repair_attempts": 0,
            "provider_id": "", "fallback_provider_ids": [],
        })
        first = _Event()
        first_snapshot = plugin._snapshot(first, "natural")
        self.assertTrue(await plugin._activate_natural_candidate(first, first_snapshot))

        # E2 has been classified WAIT. It may be newer as a candidate, but it
        # did not start a main Agent and therefore cannot cancel E1 review.
        second = _Event()
        second_snapshot = plugin._snapshot(second, "natural")
        plugin._discard_unstarted_natural_generation(second, second_snapshot)
        self.assertEqual(first.get_extra("shio.sys001.generation"), plugin._natural_generations["qq:group:202"])
        outcome = await plugin._review_final_text(first, first_snapshot, "E1 reply")

        self.assertFalse(outcome.stale)
        self.assertFalse(outcome.exhausted)
        self.assertEqual("E1 reply", outcome.text)

    async def test_e2_wait_preserves_started_e1_review_and_after_send_commit(self):
        """A non-REPLY E2 may not steal E1's RespondStage cadence binding."""
        reviewer = FakeProvider('{"action":"keep"}')
        plugin = _plugin(FakeContext(current=reviewer), final_review={
            "mode": "core", "timeout_seconds": 1, "max_repair_attempts": 0,
        })
        scope = "qq:group:202"
        store = {}
        plugin._natural_cadence = {scope: SYS001.NaturalCadence.empty()}
        plugin._natural_bindings = {}
        plugin._natural_ready = {scope: True}
        plugin._natural_locks = {}
        plugin._natural_terminated = False

        async def put(key, value):
            store[key] = value

        plugin.put_kv_data = put
        first = _Event("E1")
        first_snapshot = plugin._snapshot(first, "natural")
        self.assertTrue(await plugin._activate_natural_candidate(first, first_snapshot))

        # E2 gets a fresh candidate number, then its pre-Agent classifier says
        # WAIT.  The real request path invokes this same discard helper.
        second = _Event("E2")
        second_snapshot = plugin._snapshot(second, "natural")
        plugin._discard_unstarted_natural_generation(second, second_snapshot)
        self.assertEqual(
            first.get_extra("shio.sys001.generation"),
            plugin._natural_generations[scope],
        )

        response = type("Response", (), {
            "role": "assistant", "completion_text": "E1 visible", "reasoning_content": "",
        })()
        await plugin.observe_final_agent_response(first, response)
        self.assertEqual("E1 visible", response.completion_text)
        self.assertIsNotNone(first.get_extra("shio.sys001.natural_reply_pending"))

        await plugin.record_standard_send(first)
        record = store[plugin._natural_kv_key(scope)]
        self.assertEqual([record["completed_at"][0]], record["completed_at"])
        self.assertTrue(first.get_extra("shio.sys001.natural_stage_committed"))


class R8DocumentationContractTests(unittest.TestCase):
    def test_closed_contracts_are_described_as_official_owners_not_open_gaps(self):
        root = __import__("pathlib").Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        compatibility = (root / "docs" / "COMPATIBILITY.md").read_text(encoding="utf-8")
        self.assertNotIn("没有可靠跨event连续窗口", readme)
        self.assertIn("连续消息", readme)
        self.assertNotIn("D-077", readme)
        for text in (readme, compatibility):
            self.assertNotIn("Skill 逐项来源（CR-015）", text)
            self.assertNotIn("动作确认（CR-016）", text)
            self.assertNotIn("分段发送协作（CR-018）", text)
        self.assertIn("AstrBot", readme)
        self.assertIn("segmented_reply", compatibility)
        self.assertIn("Meme Manager", compatibility)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
