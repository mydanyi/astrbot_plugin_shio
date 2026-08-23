from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

try:
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeStarTools,
        FakeTool,
        FakeToolSet,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeStarTools,
        FakeTool,
        FakeToolSet,
        main,
    )

from astrbot_plugin_shio.core.contracts import (
    ActionKind,
    ContractViolation,
    ParticipationLevel,
)
from astrbot_plugin_shio.core.participation_engine import ParticipationAssessment


class At:
    type = "at"

    def __init__(self, qq: str) -> None:
        self.qq = qq


class P5ParticipationEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.plugin_count = 0

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _plugin(
        self,
        *,
        persona_name: str = "亚托莉",
        trusted_bot_identities: tuple[str, ...] = (),
    ):
        self.plugin_count += 1
        FakeStarTools.data_dir = Path(self.temp.name) / f"runtime-{self.plugin_count}"
        provider = FakeProvider([])
        plugin = main.ShioPlugin(
            FakeContext(provider, global_tools=[FakeTool("anysearch_search")]),
            {
                "persona_name": persona_name,
                "natural_name_wake_aliases": ["亚托莉", "萝卜子"],
                "guest_allowed_tools": ["anysearch_search"],
                "prefer_livingmemory_group_history": False,
                "trusted_bot_identities": list(trusted_bot_identities),
            },
        )
        return plugin, provider

    @staticmethod
    def _event(
        message: str,
        suffix: str,
        *,
        direct: bool = False,
        components: tuple[object, ...] = (),
    ) -> FakeEvent:
        event = FakeEvent("peer-a", message, group_id="p5-participation-group")
        event.message_id = f"p5-participation-{suffix}"
        event.is_at_or_wake_command = direct
        event.get_messages = lambda: list(components)
        return event

    def _admit(self, plugin, event: FakeEvent) -> ParticipationAssessment:
        result = plugin.admit_ingress_event(event)
        self.assertIsNotNone(result)
        self.assertTrue(result.decision.allows_state_mutation)
        assessment = event.get_extra(main.SHIO_PARTICIPATION_ASSESSMENT)
        self.assertIs(type(assessment), ParticipationAssessment)
        return assessment

    def test_direct_required_and_wait_addresses_are_closed_before_scoring(self):
        direct_plugin, _ = self._plugin()
        direct = self._event("你今天开心吗？", "direct", direct=True)
        direct_assessment = self._admit(direct_plugin, direct)
        self.assertIs(
            direct_assessment.decision.level,
            ParticipationLevel.MUST_REPLY,
        )
        self.assertEqual(direct_assessment.self_relevance, 1.0)

        for name, message, components in (
            ("uncertain", "今天下雨了", ()),
            ("other-person", "小林你怎么看？", (At("peer-b"),)),
        ):
            with self.subTest(name=name):
                wait_plugin, _ = self._plugin()
                wait = self._event(message, name, components=components)
                wait_assessment = self._admit(wait_plugin, wait)
                self.assertIs(
                    wait_assessment.decision.level,
                    ParticipationLevel.NO_ACTION,
                )
                self.assertFalse(wait.is_at_or_wake_command)

    def test_about_self_can_join_but_open_group_requires_persona_interest(self):
        about_plugin, _ = self._plugin()
        about = self._event("我觉得萝卜子这个称呼很有趣", "about")
        about_assessment = self._admit(about_plugin, about)
        self.assertIs(about_assessment.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertTrue(about.is_at_or_wake_command)
        self.assertTrue(about.is_wake)

        atri_plugin, _ = self._plugin(persona_name="亚托莉")
        atri = self._event("大家今晚吃什么？", "atri-meal")
        atri_assessment = self._admit(atri_plugin, atri)
        self.assertIs(atri_assessment.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertGreater(atri_assessment.interest_relevance, 0.0)
        self.assertTrue(atri.is_at_or_wake_command)

        neutral_plugin, _ = self._plugin(persona_name="中性基准")
        neutral = self._event("大家今晚吃什么？", "neutral-meal")
        neutral_assessment = self._admit(neutral_plugin, neutral)
        self.assertIs(neutral_assessment.decision.level, ParticipationLevel.NO_ACTION)
        self.assertEqual(neutral_assessment.interest_relevance, 0.0)
        self.assertFalse(neutral.is_at_or_wake_command)

    async def test_may_join_uses_typed_reply_pipeline_without_tools(self):
        plugin, provider = self._plugin()
        event = self._event("大家今晚吃什么？", "hot-path")
        assessment = self._admit(plugin, event)
        request = FakeRequest(event.message)
        request.func_tool = FakeToolSet([FakeTool("anysearch_search")])

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        planned = event.get_extra(main.SHIO_PLANNED_ACTION)
        self.assertIs(planned.kind, ActionKind.REPLY)
        self.assertIs(
            assessment.decision.level,
            ParticipationLevel.MAY_JOIN,
        )
        self.assertEqual(tuple(request.func_tool.tools), ())
        self.assertEqual(provider.calls, [])

    def test_assessment_copy_mutation_cross_authority_and_scene_corruption_fail_closed(self):
        plugin, _ = self._plugin()
        event = self._event("大家今晚吃什么？", "canonical")
        assessment = self._admit(plugin, event)
        opportunity = event.get_extra(main.SHIO_OPPORTUNITY_ATTENTION)
        scene = event.get_extra(main.SHIO_GROUP_SCENE_SNAPSHOT)

        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            plugin.participation_authority.inspect(copy.copy(assessment))

        original_value = assessment.response_value
        object.__setattr__(assessment, "response_value", 0.0)
        with self.assertRaisesRegex(ContractViolation, "corrupt"):
            plugin.participation_authority.inspect(assessment)
        object.__setattr__(assessment, "response_value", original_value)

        original_level = assessment.decision.level
        object.__setattr__(
            assessment.decision,
            "level",
            ParticipationLevel.MUST_REPLY,
        )
        with self.assertRaisesRegex(ContractViolation, "corrupt"):
            plugin.participation_authority.inspect(assessment)
        object.__setattr__(assessment.decision, "level", original_level)

        other_plugin, _ = self._plugin()
        with self.assertRaisesRegex(ContractViolation, "not_canonical"):
            other_plugin.participation_authority.inspect(assessment)

        original_revision = scene.conversation_revision
        object.__setattr__(scene, "conversation_revision", original_revision + 1)
        with self.assertRaisesRegex(ContractViolation, "scene_snapshot_corrupt"):
            plugin.participation_authority.inspect(assessment)
        object.__setattr__(scene, "conversation_revision", original_revision)
        self.assertIs(plugin.participation_authority.inspect(assessment), assessment)
        self.assertIs(assessment.opportunity, opportunity)

    def test_newer_group_turn_does_not_invalidate_sealed_earlier_assessment(self):
        plugin, _ = self._plugin()
        earlier = self._event(
            "亚托莉，他们刚才在聊什么？",
            "earlier-direct",
            direct=True,
        )
        earlier_assessment = self._admit(plugin, earlier)

        later = self._event(
            "小林，这个问题你怎么看？",
            "later-other-person",
            components=(At("peer-b"),),
        )
        self._admit(plugin, later)

        self.assertIs(
            plugin.participation_authority.inspect(earlier_assessment),
            earlier_assessment,
        )

    def test_rejected_ingress_never_creates_participation_or_promotes(self):
        plugin, provider = self._plugin(
            trusted_bot_identities=("亚托莉|bot-peer",),
        )
        event = self._event("大家今晚吃什么？", "known-bot")
        event.sender_id = "bot-peer"

        result = plugin.admit_ingress_event(event)
        self.assertIsNotNone(result)
        self.assertFalse(result.decision.allows_state_mutation)
        self.assertIsNone(event.get_extra(main.SHIO_PARTICIPATION_ASSESSMENT))
        self.assertFalse(event.is_at_or_wake_command)
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
