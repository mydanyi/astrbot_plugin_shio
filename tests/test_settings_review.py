"""Review configuration must affect the real final-response hook payload."""

import copy
import json
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from astrbot_plugin_shio.tests import test_name_semantic_provider_routing as fixtures
from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import MAIN, FakeContext, FakeProvider


class SettingsReviewTests(unittest.IsolatedAsyncioTestCase):
    async def review(self, settings, *, provider=None, batch=()):
        event, lifecycle = fixtures.FinalAgentObservationTests.event_with_live_turn()
        snapshot = replace(event.get_extra(MAIN.SYS001_TURN_EXTRA),
                           message_text="请说说我刚才的饮料偏好", sender_name="当前群友")
        event.set_extra(MAIN.SYS001_TURN_EXTRA, snapshot)
        event.unified_msg_origin = snapshot.scope
        event.set_extra("shio.sys001.batch", tuple(
            MAIN._BatchMessage(event, replace(snapshot, **item), False, (), None)
            for item in batch
        ))
        provider = provider or FakeProvider('{"action":"keep"}')
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        plugin.context = FakeContext(current=provider)
        plugin.config = {"sys001": {"final_review": copy.deepcopy(settings)}}
        before = copy.deepcopy(plugin.config)
        response = fixtures.FinalAgentObservationTests.response(completion_text="知道啦。&&meme:abc123&&")
        await plugin.observe_final_agent_response(event, response)
        self.assertEqual(before, plugin.config)
        return response, provider, event, lifecycle

    async def test_custom_rules_are_selected_by_mode_without_being_overwritten(self):
        for mode, core, extra in (("core", True, False), ("additional", False, True),
                                  ("combined", True, True), ("off", False, False)):
            with self.subTest(mode=mode):
                response, provider, _, _ = await self.review({
                    "mode": mode, "core_prompt": "自定义基础：不把机器人当群友",
                    "additional_prompt": "自定义附加：避免客服式结尾",
                })
                self.assertEqual("知道啦。&&meme:abc123&&", response.completion_text)
                self.assertEqual(0 if mode == "off" else 1, len(provider.calls))
                if provider.calls:
                    prompt = provider.calls[0]["prompt"]
                    self.assertEqual(core, "自定义基础：不把机器人当群友" in prompt)
                    self.assertEqual(extra, "自定义附加：避免客服式结尾" in prompt)
                    self.assertIsNone(provider.calls[0]["func_tool"])

    async def test_review_receives_current_question_and_only_same_scope_earlier_people(self):
        event, _ = fixtures.FinalAgentObservationTests.event_with_live_turn()
        snapshot = event.get_extra(MAIN.SYS001_TURN_EXTRA)
        _, provider, _, _ = await self.review({"mode": "core"}, batch=(
            {"message_id": "earlier", "sender_id": "other-member", "sender_name": "前一位群友",
             "message_text": "我喜欢乌龙茶", "created_at": snapshot.created_at - timedelta(seconds=1)},
            {"message_id": "future", "message_text": "未来不应出现", "created_at": snapshot.created_at + timedelta(seconds=1)},
            {"message_id": "elsewhere", "scope": "other:group", "message_text": "其他群不应出现"},
            {"message_id": "self", "sender_id": snapshot.self_id, "message_text": "机器人不能当群友"},
        ))
        prompt = provider.calls[0]["prompt"]
        for value in ("请说说我刚才的饮料偏好", "当前群友", "other-member", "我喜欢乌龙茶", snapshot.self_id):
            self.assertIn(value, prompt)
        for value in ("未来不应出现", "其他群不应出现", "机器人不能当群友"):
            self.assertNotIn(value, prompt)

    async def test_schema_default_rules_reach_provider_and_saved_empty_extra_stays_empty(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
        fields = schema["sys001"]["items"]["final_review"]["items"]
        self.assertIn("core_prompt", fields)
        defaults = {key: copy.deepcopy(value["default"]) for key, value in fields.items()}
        defaults["mode"] = "combined"
        _, provider, _, _ = await self.review(defaults)
        self.assertIn(fields["core_prompt"]["default"], provider.calls[0]["prompt"])
        self.assertIn(fields["additional_prompt"]["default"], provider.calls[0]["prompt"])
        self.assertTrue(fields["additional_prompt"]["default"].strip())
        _, missing_provider, _, _ = await self.review({"mode": "combined"})
        self.assertIn(fields["core_prompt"]["default"], missing_provider.calls[0]["prompt"])
        self.assertIn(fields["additional_prompt"]["default"], missing_provider.calls[0]["prompt"])
        defaults["additional_prompt"] = ""
        _, provider, _, _ = await self.review(defaults)
        self.assertNotIn(fields["additional_prompt"]["default"], provider.calls[0]["prompt"])

    async def test_no_repair_failure_policy_can_block_or_send_original(self):
        for send_last in (False, True):
            with self.subTest(send_last=send_last):
                response, _, _, _ = await self.review(
                    {"mode": "core", "repair_enabled": False,
                     "send_last_reply_on_review_exhausted": send_last},
                    provider=FakeProvider('{"action":"replace","text":"不能偷偷采用"}'),
                )
                self.assertEqual("知道啦。&&meme:abc123&&" if send_last else "", response.completion_text)
