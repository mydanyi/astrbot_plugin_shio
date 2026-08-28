import copy
import dataclasses
import hashlib
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
from astrbot_plugin_shio.core.model_input_contract import (
    canonical_model_messages_digest,
    project_group_scene_model_messages,
    project_model_identity_prompt_data,
)
from astrbot_plugin_shio.core.proactive_topic import (
    ProactiveTopicPlan,
    ProactiveTopicSource,
)


class _AtComponent:
    type = "at"

    def __init__(self, sender_id: str, display_name: str) -> None:
        self.qq = sender_id
        self.name = display_name


class _ReplyComponent:
    type = "reply"

    def __init__(
        self,
        message_id: str,
        sender_id: str,
        display_name: str,
        quoted_content: str,
    ) -> None:
        self.id = message_id
        self.sender_id = sender_id
        self.sender_nickname = display_name
        self.message_str = quoted_content


class ProactiveTopicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)
        self.plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "persona_name": "亚托莉",
                "natural_name_wake_aliases": ["星汐"],
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
            prior.get_sender_name = lambda: "测试乙"
            prior.message_id = f"p7-topic-{self.turn}"
            prior.created_at = 1000.0 + self.turn
            self.plugin.admit_ingress_event(prior)
        self.turn += 1
        event = FakeEvent("peer-a", message, group_id="group-a")
        event.get_sender_name = lambda: "测试甲"
        event.message_id = f"p7-topic-{self.turn}"
        event.created_at = 1000.0 + self.turn
        self.plugin.admit_ingress_event(event)
        scene = event.get_extra(main.SHIO_GROUP_SCENE_SNAPSHOT)
        self.assertIsNotNone(scene)
        return scene

    @staticmethod
    def _bounded_body(index: int, length: int) -> str:
        prefix = f"第{index}段公开讨论"
        if length < len(prefix):
            raise AssertionError("named proactive body is too short")
        return prefix + (chr(0x4E00 + index) * (length - len(prefix)))

    def _named_scene(
        self,
        bodies: tuple[str, ...],
        *,
        message_prefix: str,
        mention_star_at: tuple[int, ...] = (),
    ):
        """Record a real-name chain: each speaker replies to the prior speaker."""

        names = ("山茶", "明川", "白露", "Danyi")
        sender_ids = {
            "山茶": "synthetic-shancha",
            "明川": "synthetic-mingchuan",
            "白露": "synthetic-bailu",
            "Danyi": "synthetic-danyi",
        }
        previous: tuple[str, str, str, str] | None = None
        scene = None
        for index, body in enumerate(bodies, 1):
            name = names[(index - 1) % len(names)]
            event = FakeEvent(sender_ids[name], body, group_id="group-a")
            event.get_sender_name = lambda name=name: name
            event.message_id = f"{message_prefix}-{index}"
            event.created_at = 1000.0 + index
            components: list[object] = []
            if previous is not None:
                prior_name, prior_id, prior_message_id, prior_body = previous
                components.append(
                    _ReplyComponent(
                        prior_message_id,
                        prior_id,
                        prior_name,
                        prior_body,
                    )
                )
            if index in mention_star_at:
                components.append(_AtComponent("bot-10000", "星汐"))
            event.get_messages = lambda values=tuple(components): list(values)
            self.plugin.admit_ingress_event(event)
            scene = event.get_extra(main.SHIO_GROUP_SCENE_SNAPSHOT)
            self.assertIsNotNone(scene)
            previous = (name, sender_ids[name], event.message_id, body)
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

    def test_unmatched_scene_uses_latest_verified_public_topic_not_persona_fallback(self):
        scene = self._scene("今天天气真不错")
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )
        self.assertEqual(plan.topic_text, "今天天气真不错")
        self.assertEqual(plan.interest_id, "general_public_topic")

    def test_allowlisted_group_can_continue_latest_grounded_public_topic(self):
        """A group must not be permanently silent only because keywords miss."""

        scene = self._scene("这台平板已经刷好系统了")
        persona = self.plugin._configured_persona_package()
        self.assertTrue(
            self.plugin.proactive_topic_authority.has_grounded_context(
                scene,
                persona=persona,
            )
        )
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=persona,
        )
        self.assertEqual(plan.topic_text, "这台平板已经刷好系统了")
        self.assertEqual(plan.interest_id, "general_public_topic")

    def test_plan_carries_pure_content_and_real_identity_metadata(self):
        scene = self._scene("大家晚饭吃什么？")
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )

        self.assertGreaterEqual(len(plan.recent_public_context), 2)
        rendered = "\n".join(
            message.content for message in plan.recent_public_context
        )
        identities = project_model_identity_prompt_data(
            plan.recent_public_context
        )
        self.assertIn("刚才的话题还没聊完", rendered)
        self.assertIn("大家晚饭吃什么", rendered)
        self.assertNotIn("测试甲", rendered)
        self.assertNotIn("测试乙", rendered)
        self.assertEqual(
            [
                item["speaker"]["display_name"]
                for item in identities["history"]
            ],
            ["测试乙", "测试甲"],
        )
        self.assertNotIn("群友1", repr(identities))
        self.assertNotIn("peer-a", rendered)
        self.assertNotIn("peer-b", rendered)
        self.assertNotIn("peer-a", repr(identities))
        self.assertNotIn("peer-b", repr(identities))

    def test_long_context_preserves_forward_last_eight_budget_digest_and_provider_contract(self):
        bodies = tuple(self._bounded_body(index, 600) for index in range(1, 9))
        scene = self._named_scene(
            bodies,
            message_prefix="proactive-boundary",
            mention_star_at=(6,),
        )
        persona = self.plugin._configured_persona_package()
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=persona,
        )

        expected_ids = tuple(
            f"proactive-boundary-{index}" for index in range(1, 7)
        )
        expected_digest = (
            "a9decc983830c30394ec2f54037e2e4fa58a84713e800d2a1c3a82815118fb61"
        )
        self.assertEqual(
            (
                tuple(
                    message.source_message_id
                    for message in plan.recent_public_context
                ),
                plan.public_context_digest,
            ),
            (expected_ids, expected_digest),
        )
        self.assertEqual(
            tuple(message.content for message in plan.recent_public_context),
            bodies[:6],
        )
        self.assertEqual(
            sum(len(message.content) for message in plan.recent_public_context),
            3600,
        )
        self.assertEqual(plan.public_context_digest, expected_digest)
        self.assertEqual(
            canonical_model_messages_digest(plan.recent_public_context),
            expected_digest,
        )

        identity_data = project_model_identity_prompt_data(
            plan.recent_public_context
        )
        self.assertEqual(
            [item["speaker"]["display_name"] for item in identity_data["history"]],
            ["山茶", "明川", "白露", "Danyi", "山茶", "明川"],
        )
        self.assertEqual(
            identity_data["history"][1]["addressing"]["reply_message_index"],
            1,
        )
        self.assertEqual(
            identity_data["history"][5]["addressing"]["reply_message_index"],
            5,
        )
        self.assertEqual(
            [
                target["display_name"]
                for target in identity_data["history"][5]["addressing"][
                    "mention_targets"
                ]
            ],
            ["星汐"],
        )

        request = self.plugin.proactive_execution_authority.prepare(
            plan,
            persona=persona,
            temporal_context=self.plugin._build_temporal_context(now=2000.0),
        )
        provider_contexts = self.plugin.proactive_execution_authority.provider_contexts(
            request
        )
        self.assertEqual(
            provider_contexts,
            [{"role": "user", "content": body} for body in bodies[:6]],
        )
        self.assertEqual(tuple(request.model_messages), plan.recent_public_context)
        for display_name in ("山茶", "明川", "白露", "Danyi", "星汐"):
            self.assertIn(display_name, request.system_prompt)

    def test_shared_projector_default_keeps_latest_selection_for_participation(self):
        bodies = tuple(self._bounded_body(index, 600) for index in range(1, 9))
        scene = self._named_scene(
            bodies,
            message_prefix="participation-latest",
            mention_star_at=(6,),
        )

        messages = project_group_scene_model_messages(
            scene,
            assistant_display_name="星汐",
            source_kind="participation_public_scene",
            max_messages=8,
            max_chars=4000,
            max_message_chars=600,
        )
        self.assertEqual(
            tuple(message.source_message_id for message in messages),
            tuple(f"participation-latest-{index}" for index in range(3, 9)),
        )
        self.assertEqual(sum(len(message.content) for message in messages), 3600)

    def test_last_eight_window_does_not_backfill_older_messages_after_skips(self):
        bodies = (
            self._bounded_body(1, 200),
            self._bounded_body(2, 200),
            (" " * 600) + "末尾可见",
            *(self._bounded_body(index, 600) for index in range(4, 10)),
            self._bounded_body(10, 401),
        )
        scene = self._named_scene(
            bodies,
            message_prefix="proactive-no-backfill",
            mention_star_at=(6,),
        )
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )

        self.assertEqual(
            tuple(message.source_message_id for message in plan.recent_public_context),
            tuple(f"proactive-no-backfill-{index}" for index in range(3, 10)),
        )
        self.assertEqual(
            sum(len(message.content) for message in plan.recent_public_context),
            3604,
        )
        self.assertEqual(plan.recent_public_context[0].content, "末尾可见")
        self.assertTrue(
            all(
                message.source_message_id
                not in {"proactive-no-backfill-1", "proactive-no-backfill-2"}
                for message in plan.recent_public_context
            )
        )

    def test_last_eight_window_skips_blank_truncation_without_backfill(self):
        bodies = (
            self._bounded_body(1, 200),
            self._bounded_body(2, 200),
            self._bounded_body(3, 200),
            *(self._bounded_body(index, 600) for index in range(4, 10)),
            self._bounded_body(10, 401),
        )
        scene = self._named_scene(
            bodies,
            message_prefix="proactive-blank-window",
            mention_star_at=(6,),
        )
        blank_after_truncation = (" " * 600) + "末尾可见"
        topics = list(scene.public_topics)
        topics[2] = dataclasses.replace(
            topics[2],
            content=blank_after_truncation,
            content_digest=hashlib.sha256(
                blank_after_truncation.encode("utf-8")
            ).hexdigest(),
        )
        boundary_scene = dataclasses.replace(
            scene,
            public_topics=tuple(topics),
        )

        messages = self.plugin.proactive_topic_authority._recent_public_context(
            boundary_scene,
            self.plugin._configured_persona_package(),
        )
        self.assertEqual(
            tuple(message.source_message_id for message in messages),
            tuple(f"proactive-blank-window-{index}" for index in range(4, 10)),
        )
        self.assertEqual(sum(len(message.content) for message in messages), 3600)
        self.assertTrue(
            all(
                message.source_message_id
                not in {"proactive-blank-window-1", "proactive-blank-window-2"}
                for message in messages
            )
        )

    def test_forward_budget_truncates_one_message_and_accepts_exact_boundary(self):
        bodies = (
            self._bounded_body(1, 650),
            *(self._bounded_body(index, 600) for index in range(2, 7)),
            self._bounded_body(7, 400),
            "超",
        )
        scene = self._named_scene(
            bodies,
            message_prefix="proactive-exact-budget",
            mention_star_at=(6,),
        )
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )

        self.assertEqual(
            tuple(message.source_message_id for message in plan.recent_public_context),
            tuple(f"proactive-exact-budget-{index}" for index in range(1, 8)),
        )
        self.assertEqual(len(plan.recent_public_context[0].content), 600)
        self.assertEqual(
            sum(len(message.content) for message in plan.recent_public_context),
            4000,
        )
        self.assertNotIn(
            "proactive-exact-budget-8",
            {message.source_message_id for message in plan.recent_public_context},
        )

    def test_short_named_context_keeps_people_and_reply_order(self):
        bodies = (
            "山茶说设备说明在桌上。",
            "明川回复山茶，说第一行是处理器。",
            "白露回复明川，说后面还有重量。",
            "Danyi 回复白露，并请星汐记住当前讨论。",
        )
        scene = self._named_scene(
            bodies,
            message_prefix="proactive-short-named",
            mention_star_at=(4,),
        )
        plan = self.plugin.proactive_topic_authority.select(
            self._decision(),
            scene=scene,
            persona=self.plugin._configured_persona_package(),
        )
        identity_data = project_model_identity_prompt_data(
            plan.recent_public_context
        )["history"]

        self.assertEqual(
            [item["speaker"]["display_name"] for item in identity_data],
            ["山茶", "明川", "白露", "Danyi"],
        )
        self.assertEqual(
            [item["addressing"]["reply_message_index"] for item in identity_data],
            [None, 1, 2, 3],
        )
        self.assertEqual(
            [
                target["display_name"]
                for target in identity_data[-1]["addressing"]["mention_targets"]
            ],
            ["星汐"],
        )

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
