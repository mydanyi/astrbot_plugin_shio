import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path

try:
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeStarTools,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeStarTools,
        main,
    )

from astrbot_plugin_shio.core.contracts import ContractViolation
from astrbot_plugin_shio.core.proactive_topic import (
    ProactiveTopicPlan,
    ProactiveTopicSource,
)


class ProactiveTopicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "persona_name": "亚托莉",
                "proactive_initiation_enabled": True,
                "proactive_group_allowlist": ["group-a"],
                "proactive_active_hour_start": 0,
                "proactive_active_hour_end": 24,
                "proactive_timezone_offset_minutes": 0,
                "proactive_observation_minutes": 0,
                "proactive_idle_minutes": 0,
                "proactive_cooldown_minutes": 0,
                "proactive_daily_limit": 16,
            },
        )
        self.turn = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _scene(self, message: str):
        if self.turn == 0:
            self.turn += 1
            prior = FakeEvent("peer-b", "刚才的话题还没聊完", group_id="group-a")
            prior.message_id = f"p7-topic-{self.turn}"
            prior.created_at = 1000.0 + self.turn
            self.plugin.admit_ingress_event(prior)
        self.turn += 1
        event = FakeEvent("peer-a", message, group_id="group-a")
        event.message_id = f"p7-topic-{self.turn}"
        event.created_at = 1000.0 + self.turn
        self.plugin.admit_ingress_event(event)
        scene = event.get_extra(main.SHIO_GROUP_SCENE_SNAPSHOT)
        self.assertIsNotNone(scene)
        return scene

    def _decision(self, *, observed_at: float = 2000.0):
        observation = self.plugin.proactive_trigger_authority.observe_group(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="group-a",
            unified_msg_origin="group:group-a",
            observed_at=observed_at,
        )
        candidate = self.plugin.proactive_trigger_authority.issue_candidate(
            self.plugin.proactive_trigger_authority.issue_source(observation)
        )
        decision = self.plugin.proactive_policy_state.evaluate(
            candidate,
            now=observed_at,
        )
        self.assertTrue(decision.admitted)
        return decision

    def test_matching_public_topic_is_selected_without_model_or_send(self):
        scene = self._scene("大家晚饭吃什么？")
        decision = self._decision()
        persona = self.plugin._configured_persona_package()
        plan = self.plugin.proactive_topic_authority.select(
            decision,
            scene=scene,
            persona=persona,
        )

        self.assertIs(type(plan), ProactiveTopicPlan)
        self.assertIs(plan.source, ProactiveTopicSource.GROUP_PUBLIC_TOPIC)
        self.assertEqual(plan.topic_text, "大家晚饭吃什么？")
        self.assertEqual(plan.interest_id, "shared_meals")
        self.assertFalse(plan.model_authorized)
        self.assertFalse(plan.send_authorized)
        self.assertIs(self.plugin.proactive_topic_authority.inspect(plan), plan)

    def test_unmatched_scene_is_silent_instead_of_starting_persona_fallback(self):
        scene = self._scene("今天天气真不错")
        with self.assertRaisesRegex(
            ContractViolation,
            "proactive_topic_no_grounded_match",
        ):
            self.plugin.proactive_topic_authority.select(
                self._decision(),
                scene=scene,
                persona=self.plugin._configured_persona_package(),
            )

    def test_plan_carries_bounded_anonymized_multi_turn_public_context(self):
        scene = self._scene("大家晚饭吃什么？")
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )

        self.assertGreaterEqual(len(plan.recent_public_context), 2)
        rendered = "\n".join(plan.recent_public_context)
        self.assertIn("刚才的话题还没聊完", rendered)
        self.assertIn("大家晚饭吃什么", rendered)
        self.assertNotIn("peer-a", rendered)
        self.assertNotIn("peer-b", rendered)

    def test_plan_surface_contains_no_scene_participants_or_private_memory(self):
        scene = self._scene("大家晚饭吃什么？")
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )
        public_fields = {
            field.name
            for field in dataclasses.fields(ProactiveTopicPlan)
            if not field.name.startswith("_")
        }
        for forbidden in (
            "scene",
            "participants",
            "personal_facts",
            "sender_id",
            "sender_key",
            "principal",
            "memory",
            "owner",
        ):
            self.assertNotIn(forbidden, public_fields)
        rendered = repr(plan) + repr(plan.trace_metadata())
        self.assertNotIn("peer-a", rendered)
        self.assertNotIn("大家晚饭吃什么", rendered)
        source = (
            Path(main.__file__).parent / "core" / "proactive_topic.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("personal_facts", source)
        self.assertNotIn(".participants", source)
        self.assertNotIn("livingmemory", source.casefold())

    def test_blocked_policy_copy_persona_and_cross_authority_are_rejected(self):
        scene = self._scene("大家晚饭吃什么？")
        persona = self.plugin._configured_persona_package()
        decision = self._decision()

        with self.assertRaises(ContractViolation):
            self.plugin.proactive_topic_authority.select(
                decision,
                scene=scene,
                persona=dataclasses.replace(persona),
            )
        other = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        with self.assertRaises(ContractViolation):
            other.proactive_topic_authority.select(
                decision,
                scene=scene,
                persona=persona,
            )

        disabled_observation = other.proactive_trigger_authority.observe_group(
            platform_id="test",
            bot_id="bot",
            group_id="group-a",
            unified_msg_origin="group:group-a",
            observed_at=3000.0,
        )
        disabled_candidate = other.proactive_trigger_authority.issue_candidate(
            other.proactive_trigger_authority.issue_source(disabled_observation)
        )
        blocked = other.proactive_policy_state.evaluate(
            disabled_candidate,
            now=3000.0,
        )
        with self.assertRaisesRegex(ContractViolation, "proactive_topic_policy_not_admitted"):
            other.proactive_topic_authority.select(
                blocked,
                scene=scene,
                persona=other._configured_persona_package(),
            )

    def test_decision_is_one_shot_and_plan_copy_or_mutation_is_not_canonical(self):
        scene = self._scene("大家晚饭吃什么？")
        decision = self._decision()
        plan = self.plugin.proactive_topic_authority.select(
            decision,
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )
        with self.assertRaisesRegex(ContractViolation, "proactive_topic_policy_consumed"):
            self.plugin.proactive_topic_authority.select(
                decision,
                scene=scene,
                persona=self.plugin._configured_persona_package(),
            )
        with self.assertRaises((ContractViolation, TypeError)):
            self.plugin.proactive_topic_authority.inspect(copy.copy(plan))

        original = plan.topic_text
        object.__setattr__(plan, "topic_text", "PRIVATE-MARKER")
        with self.assertRaisesRegex(ContractViolation, "proactive_topic_plan_corrupt"):
            self.plugin.proactive_topic_authority.inspect(plan)
        self.assertNotIn("PRIVATE-MARKER", repr(plan) + repr(plan.trace_metadata()))
        object.__setattr__(plan, "topic_text", original)
        self.assertIs(self.plugin.proactive_topic_authority.inspect(plan), plan)

    def test_new_group_message_invalidates_old_scene_bound_plan(self):
        scene = self._scene("大家晚饭吃什么？")
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )
        self._scene("对了，换个话题")
        with self.assertRaises(ContractViolation):
            self.plugin.proactive_topic_authority.inspect(plan)

    def test_new_scheduler_generation_invalidates_old_topic_plan(self):
        scene = self._scene("大家晚饭吃什么？")
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )
        observation = self.plugin.proactive_trigger_authority.observe_group(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="group-a",
            unified_msg_origin="group:group-a",
            observed_at=2001.0,
        )
        self.plugin.proactive_trigger_authority.issue_candidate(
            self.plugin.proactive_trigger_authority.issue_source(observation)
        )
        with self.assertRaisesRegex(ContractViolation, "proactive_policy_candidate_stale"):
            self.plugin.proactive_topic_authority.inspect(plan)


if __name__ == "__main__":
    unittest.main()
