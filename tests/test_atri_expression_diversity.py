import unittest
from pathlib import Path

from astrbot_plugin_shio.core.affect import (
    AffectTrigger,
    RelationshipDistance,
    appraise_affect,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.conversation_ledger import ledger_content_digest
from astrbot_plugin_shio.core.identity import resolve_principal
from astrbot_plugin_shio.core.persona import load_persona_package
from astrbot_plugin_shio.core.persona_expression import (
    build_persona_expression_plan,
)


ROOT = Path(__file__).parents[1]
PACKAGE = load_persona_package(ROOT / "assets" / "personas" / "atri.json")
SCOPE = "platform:p|bot:b|group:g"


def peer_principal():
    return resolve_principal(
        sender_id="peer-a",
        sender_key=f"{SCOPE}|user:peer-a",
        chat_type="group",
        owner_ids=("owner-a",),
        verification_source="astrbot_event_sender_id",
        identity_verified=True,
    )


def expression(message: str, *, tags=()):
    principal = peer_principal()
    target = ReplyTarget(
        message_id="message-a",
        sender_key=principal.sender_key,
        session_id="g",
        scope_key=SCOPE,
        content_digest=ledger_content_digest(message),
        source_kind="current_inbound",
        referenced_message_id="",
        degradation_reasons=(),
    )
    appraisal = appraise_affect(
        principal=principal,
        reply_target=target,
        current_message=message,
    )
    return build_persona_expression_plan(
        PACKAGE,
        appraisal,
        principal=principal,
        situational_tags=tuple(tags),
    )


class AtriExpressionDiversityTests(unittest.TestCase):
    def test_closed_calibration_matrix_keeps_stubbornness_situational(self):
        ordinary = expression("请解释一下这个配置为什么失效")
        praise = expression("你今天真的很厉害")
        seen_through = expression("你明明很在意，还在嘴硬")
        teased = expression("急了？我就逗你一下")
        correction = expression("你刚才说错了，不是这个人")
        care = expression("我今天真的很难受，想让你陪陪我")

        self.assertIn(ordinary.trigger, {AffectTrigger.INFORMATION_REQUEST, AffectTrigger.NEUTRAL})
        self.assertEqual(ordinary.catchphrase_candidates, ())
        self.assertNotIn("brief_embarrassed_deflection", ordinary.trajectory_steps)
        self.assertNotIn("brief_aggrieved_protest", ordinary.trajectory_steps)

        for plan in (praise, seen_through, teased):
            with self.subTest(trigger=plan.trigger.value):
                self.assertEqual(plan.trajectory_steps[-1], plan.topic_return)
                self.assertTrue(plan.hidden_reveal_behavior_ids)
                self.assertLess(
                    plan.trajectory_steps.index(plan.surface_behavior_ids[0]),
                    plan.trajectory_steps.index(plan.hidden_reveal_behavior_ids[0]),
                )

        for plan in (correction, care):
            with self.subTest(trigger=plan.trigger.value):
                self.assertNotIn("performance_boasting", plan.trajectory_steps)
                self.assertFalse(
                    any("高性能" in phrase for phrase in plan.catchphrase_candidates)
                )

    def test_peer_and_unverified_never_gain_primary_bond_expression(self):
        for distance in (RelationshipDistance.PEER, RelationshipDistance.UNVERIFIED):
            rule = next(
                item for item in PACKAGE.relationship_rules if item.distance is distance
            )
            with self.subTest(distance=distance.value):
                self.assertIn("exclusive_romance", rule.forbidden_action_ids)
                self.assertIn("owner_title", rule.forbidden_action_ids)
                self.assertNotIn("private_privilege", rule.allowed_action_ids)
                exclusive_materials = tuple(
                    material.id
                    for material in PACKAGE.expression_materials
                    if material.relationship_distances
                    and distance in material.relationship_distances
                    and "primary_bond" in material.id
                )
                self.assertEqual(exclusive_materials, ())

    def test_core_traits_cover_more_than_stubborn_pride(self):
        trait_ids = {trait.id for trait in PACKAGE.core_traits}

        self.assertTrue(
            {
                "earnest_commitment",
                "high_performance_pride",
                "lively_childlike_curiosity",
                "emotionally_sincere",
                "care_through_action",
                "soft_stubbornness",
            }.issubset(trait_ids)
        )

    def test_lively_surprise_is_situational_and_returns_to_topic(self):
        plain = expression("今天群里挺安静的")
        surprised = expression("今天群里挺安静的", tags=("surprised",))

        self.assertNotIn("lively_surprise", plain.material_ids)
        self.assertIn("lively_surprise", surprised.material_ids)
        self.assertIn("brief_surprised_reaction", surprised.trajectory_steps)
        self.assertIn("stay_on_topic", surprised.trajectory_steps)

    def test_light_teasing_can_show_aggrievement_without_hostility(self):
        plan = expression("急了？我就逗你一下")

        self.assertIn("lightly_teased_dignity", plan.material_ids)
        self.assertIn("brief_aggrieved_protest", plan.trajectory_steps)
        self.assertIn("protect_dignity", plan.trajectory_steps)
        self.assertIn("sudden_hostility", plan.avoid_behavior_ids)
        self.assertIn("react_then_answer", plan.trajectory_steps)

    def test_serious_information_answer_prioritizes_content(self):
        plan = expression("为什么这个配置会失效？")

        self.assertIn("answer_current_question_first", plan.trajectory_steps)
        self.assertIn("fact_answer_voice", plan.material_ids)
        self.assertEqual(plan.catchphrase_candidates, ())
        self.assertIn("persona_detour", plan.avoid_behavior_ids)

    def test_competitive_retry_needs_game_loss_context(self):
        normal_teasing = expression("急了？我就逗你一下")
        game_loss = expression(
            "急了？这一局你输了",
            tags=("playful_loss",),
        )

        self.assertNotIn("playful_retry", normal_teasing.material_ids)
        self.assertIn("playful_retry", game_loss.material_ids)
        self.assertIn("ask_for_retry", game_loss.trajectory_steps)
        self.assertIn("stay_friendly", game_loss.trajectory_steps)
        self.assertEqual(len(game_loss.catchphrase_candidates), 1)

    def test_care_suppresses_boasting_and_keeps_specific_support(self):
        plan = expression("我今天真的很难受，想让你陪陪我")

        self.assertIn("care_first", plan.material_ids)
        self.assertIn("specific_comfort", plan.trajectory_steps)
        self.assertIn("gentle_follow_up", plan.trajectory_steps)
        self.assertIn("performance_boasting", plan.avoid_behavior_ids)
        self.assertEqual(plan.catchphrase_candidates, ())

    def test_being_seen_through_has_deflection_reveal_and_return(self):
        plan = expression("你明明很在意，还在嘴硬")

        self.assertIn("seen_through_arc", plan.material_ids)
        self.assertIn("brief_embarrassed_deflection", plan.trajectory_steps)
        self.assertIn("unintended_concern_reveal", plan.trajectory_steps)
        self.assertIn("react_then_answer", plan.trajectory_steps)
        self.assertIn("total_emotion_denial", plan.avoid_behavior_ids)

    def test_praise_and_mistake_are_not_the_same_stubborn_template(self):
        praise = expression("你今天真的很厉害")
        mistake = expression("你刚才说错了，不是这个人")

        self.assertNotEqual(praise.surface_behavior_ids, mistake.surface_behavior_ids)
        self.assertNotEqual(
            praise.hidden_reveal_behavior_ids,
            mistake.hidden_reveal_behavior_ids,
        )
        self.assertIn("soften_and_accept", praise.trajectory_steps)
        self.assertIn("clear_correction", mistake.trajectory_steps)
        self.assertIn("repair_current_answer", mistake.trajectory_steps)

    def test_semantic_scenarios_have_distinct_behavior_signatures(self):
        plans = (
            expression("今天群里挺安静的", tags=("surprised",)),
            expression("急了？我就逗你一下"),
            expression("为什么这个配置会失效？"),
            expression("急了？这一局你输了", tags=("playful_loss",)),
            expression("我今天真的很难受，想让你陪陪我"),
            expression("你明明很在意，还在嘴硬"),
        )
        signatures = {
            (
                plan.surface_behavior_ids,
                plan.hidden_reveal_behavior_ids,
                plan.material_ids,
                plan.topic_return,
            )
            for plan in plans
        }

        self.assertEqual(len(signatures), len(plans))
        for plan in plans:
            self.assertTrue(plan.is_actionable)
            self.assertEqual(plan.trajectory_steps[-1], plan.topic_return)


if __name__ == "__main__":
    unittest.main()
