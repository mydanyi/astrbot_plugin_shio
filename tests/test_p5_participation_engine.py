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
        FakeResponse,
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
        FakeResponse,
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


_SEMANTIC_REPLY = (
    '{"decision":"REPLY","target":"current_message",'
    '"topic_anchor":"current_message","reason_code":"natural_continuation",'
    '"confidence":0.9}'
)


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
        natural_participation_enabled: bool = True,
        proactive_enabled: bool = False,
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
                "natural_group_participation_enabled": natural_participation_enabled,
                "natural_group_participation_allowlist": [
                    "p5-participation-group"
                ],
                "natural_group_participation_min_context_messages": 2,
                "proactive_initiation_enabled": proactive_enabled,
                "proactive_group_allowlist": ["p5-participation-group"],
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

    def _prime_context(self, plugin, message: str = "刚才的话题还没说完") -> None:
        prior = self._event(message, f"prior-{self.plugin_count}")
        prior.sender_id = "peer-b"
        self._admit(plugin, prior)

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

    def test_about_self_interest_and_grounded_open_question_can_join(self):
        about_plugin, _ = self._plugin()
        self._prime_context(about_plugin)
        about = self._event("我觉得萝卜子这个称呼很有趣", "about")
        about_assessment = self._admit(about_plugin, about)
        self.assertIs(about_assessment.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertFalse(about.is_at_or_wake_command)
        self.assertIsNotNone(
            about.get_extra(main.SHIO_PARTICIPATION_SEMANTIC_REQUEST)
        )

        atri_plugin, _ = self._plugin(persona_name="亚托莉")
        self._prime_context(atri_plugin)
        atri = self._event("大家今晚吃什么？", "atri-meal")
        atri_assessment = self._admit(atri_plugin, atri)
        self.assertIs(atri_assessment.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertEqual(atri_assessment.interest_relevance, 0.0)
        self.assertFalse(atri.is_at_or_wake_command)

        neutral_plugin, _ = self._plugin(persona_name="中性基准")
        self._prime_context(neutral_plugin)
        neutral = self._event("大家今晚吃什么？", "neutral-meal")
        neutral_assessment = self._admit(neutral_plugin, neutral)
        self.assertIs(neutral_assessment.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertEqual(neutral_assessment.interest_relevance, 0.0)
        self.assertIn(
            "semantic_participation_candidate",
            neutral_assessment.reason_codes,
        )
        self.assertFalse(neutral.is_at_or_wake_command)

        unrelated_plugin, _ = self._plugin(persona_name="中性基准")
        self._prime_context(unrelated_plugin, "刚把文档整理完")
        unrelated = self._event("我换了个新头像", "neutral-unrelated")
        unrelated_assessment = self._admit(unrelated_plugin, unrelated)
        self.assertIs(
            unrelated_assessment.decision.level,
            ParticipationLevel.MAY_JOIN,
        )
        self.assertIn(
            "semantic_participation_candidate",
            unrelated_assessment.reason_codes,
        )

    async def test_may_join_uses_typed_reply_pipeline_without_tools(self):
        plugin, provider = self._plugin()
        self._prime_context(plugin)
        event = self._event("大家今晚吃什么？", "hot-path")
        assessment = self._admit(plugin, event)
        provider.outputs.append(_SEMANTIC_REPLY)
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
        self.assertEqual(len(provider.calls), 1)
        policy = event.get_extra(main.SHIO_ACTIVE_CAPABILITY_POLICY)
        self.assertEqual(policy.conversation_mode, "group_join")
        self.assertFalse(policy.is_degraded)

        response = FakeResponse("那就吃拉面吧，我也想来一碗。")
        await plugin.guard_persona_reply(event, response)
        self.assertTrue(response.completion_text)
        self.assertIsNotNone(event.get_extra(main.SHIO_PRESENTATION_HANDOFF))
        self.assertIsNotNone(event.get_extra(main.SHIO_SEMANTIC_VALIDATION_SEAL))

    async def test_may_join_uses_kb_then_keeps_final_renderer_tool_free(self):
        kb_tool = FakeTool(
            "astr_kb_search",
            "Query the knowledge base for facts or relevant context.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            module="astrbot.core.tools.knowledge_base_tools",
            result_content="仿生人是外表和行为接近人类的人造生命。",
        )
        self.plugin_count += 1
        FakeStarTools.data_dir = Path(self.temp.name) / f"runtime-{self.plugin_count}"
        provider = FakeProvider([_SEMANTIC_REPLY])
        plugin = main.ShioPlugin(
            FakeContext(provider, global_tools=[kb_tool]),
            {
                "persona_name": "亚托莉",
                "guest_allowed_tools": ["astr_kb_search"],
                "prefer_livingmemory_group_history": False,
                "natural_group_participation_enabled": True,
                "natural_group_participation_rules": "像普通群友一样顺着话题接一句。",
                "natural_group_participation_allowlist": ["p5-participation-group"],
                "natural_group_participation_min_context_messages": 2,
            },
        )
        self._prime_context(plugin, "他们刚才一直在聊仿生人这个网络黑话")
        event = self._event("仿生人这个网络黑话是什么意思？", "group-kb")
        assessment = self._admit(plugin, event)
        self.assertIs(assessment.decision.level, ParticipationLevel.MAY_JOIN)
        request = FakeRequest(event.message)
        request.func_tool = FakeToolSet([kb_tool])

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        planned = event.get_extra(main.SHIO_PLANNED_ACTION)
        acquisition = event.get_extra(main.SHIO_ACQUISITION_REQUEST)
        evidence = event.get_extra(main.SHIO_EVIDENCE_OUTCOME)
        self.assertIs(planned.kind, ActionKind.USE_TOOL)
        self.assertEqual(acquisition.selection.tool_name, "astr_kb_search")
        self.assertTrue(evidence.facts)
        self.assertTrue(request.func_tool.empty())
        self.assertIn("自然接话完整规则", request.system_prompt)

    def test_recent_group_context_can_supply_interest_for_elliptical_current_turn(self):
        plugin, _ = self._plugin()
        self._prime_context(plugin, "你们刚才说晚饭想吃面来着")
        event = self._event("大家也这么想吗？", "contextual-interest")

        assessment = self._admit(plugin, event)

        self.assertIs(assessment.decision.level, ParticipationLevel.MAY_JOIN)
        self.assertEqual(assessment.interest_relevance, 0.0)
        self.assertGreaterEqual(assessment.context_message_count, 2)
        self.assertFalse(event.is_at_or_wake_command)
        self.assertIsNotNone(
            event.get_extra(main.SHIO_PARTICIPATION_SEMANTIC_REQUEST)
        )

    def test_independent_master_switch_and_context_depth_fail_closed(self):
        disabled_plugin, _ = self._plugin(natural_participation_enabled=False)
        self._prime_context(disabled_plugin)
        disabled = self._event("大家晚饭吃什么？", "disabled")
        disabled_assessment = self._admit(disabled_plugin, disabled)
        self.assertIs(
            disabled_assessment.decision.level,
            ParticipationLevel.NO_ACTION,
        )
        self.assertIn(
            "opportunistic_group_join_disabled",
            disabled_assessment.reason_codes,
        )
        self.assertFalse(disabled.is_at_or_wake_command)

        shallow_plugin, _ = self._plugin()
        shallow = self._event("大家今晚吃什么？", "shallow")
        shallow_assessment = self._admit(shallow_plugin, shallow)
        self.assertIs(shallow_assessment.decision.level, ParticipationLevel.NO_ACTION)
        self.assertIn(
            "verified_group_context_too_shallow",
            shallow_assessment.reason_codes,
        )
        self.assertFalse(shallow.is_at_or_wake_command)

    def test_cold_scheduler_and_natural_participation_switches_are_independent(self):
        natural_only, _ = self._plugin(
            natural_participation_enabled=True,
            proactive_enabled=False,
        )
        self._prime_context(natural_only)
        natural_event = self._event("大家晚饭吃什么？", "natural-only")
        natural_assessment = self._admit(natural_only, natural_event)
        self.assertIs(
            natural_assessment.decision.level,
            ParticipationLevel.MAY_JOIN,
        )
        self.assertFalse(natural_only.proactive_policy_state.operational)

        cold_only, _ = self._plugin(
            natural_participation_enabled=False,
            proactive_enabled=True,
        )
        self._prime_context(cold_only)
        cold_event = self._event("大家晚饭吃什么？", "cold-only")
        cold_assessment = self._admit(cold_only, cold_event)
        self.assertIs(cold_assessment.decision.level, ParticipationLevel.NO_ACTION)
        self.assertFalse(cold_event.is_at_or_wake_command)
        self.assertTrue(cold_only.proactive_policy_state.operational)

    def test_assessment_copy_mutation_cross_authority_and_scene_corruption_fail_closed(self):
        plugin, _ = self._plugin()
        self._prime_context(plugin)
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
