import unittest
from dataclasses import replace
from pathlib import Path

from astrbot_plugin_shio.core.affect import (
    AffectAppraisal,
    AffectTrigger,
    BehavioralTendency,
    HiddenConcern,
    RelationshipDistance,
    SurfaceEmotion,
    TopicReturn,
)
from astrbot_plugin_shio.core.persona import load_persona_package
from astrbot_plugin_shio.core.persona_expression import (
    build_persona_expression_plan,
)
from astrbot_plugin_shio.core.identity import PrincipalContext


ROOT = Path(__file__).parents[1]
PERSONA_DIR = ROOT / "assets" / "personas"


def appraisal(
    trigger: AffectTrigger,
    *,
    distance: RelationshipDistance = RelationshipDistance.PEER,
) -> AffectAppraisal:
    topic_by_trigger = {
        AffectTrigger.PRAISE: TopicReturn.REACT_THEN_ANSWER,
        AffectTrigger.BEING_SEEN_THROUGH: TopicReturn.REACT_THEN_ANSWER,
        AffectTrigger.PLAYFUL_PROVOCATION: TopicReturn.REACT_THEN_ANSWER,
        AffectTrigger.CORRECTION_OR_MISTAKE: TopicReturn.REPAIR_THEN_CONTINUE,
        AffectTrigger.USER_NEEDS_CARE: TopicReturn.CARE_THEN_FOLLOW_UP,
        AffectTrigger.APOLOGY: TopicReturn.ACKNOWLEDGE_THEN_ANSWER,
    }
    return AffectAppraisal(
        trigger=trigger,
        focus_sender_key="sender-a",
        relationship_distance=distance,
        surface_emotion=SurfaceEmotion.CALM,
        secondary_emotion=None,
        hidden_concern=HiddenConcern.NONE,
        behavioral_tendency=(BehavioralTendency.ANSWER_CURRENT_REQUEST,),
        topic_return=topic_by_trigger.get(trigger, TopicReturn.ANSWER_TARGET),
        target_message_id="message-1",
        target_content_digest="digest-1",
        confidence=0.8,
        degradation_reasons=(),
    )


def principal(
    distance: RelationshipDistance = RelationshipDistance.PEER,
) -> PrincipalContext:
    if distance == RelationshipDistance.PRIMARY_BOND:
        return PrincipalContext(
            "sender-a",
            "owner-a",
            True,
            "owner",
            "astrbot_event_sender_id:configured_owner_id",
        )
    if distance == RelationshipDistance.PEER:
        return PrincipalContext(
            "sender-a",
            "peer-a",
            False,
            "group_peer",
            "astrbot_event_sender_id:owner_allowlist_miss",
        )
    return PrincipalContext("", "", False, "unverified", "identity_unverified")


class PersonaExpressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.atri = load_persona_package(PERSONA_DIR / "atri.json")
        cls.alternate = load_persona_package(PERSONA_DIR / "su_cheng.json")

    def test_neutral_reply_does_not_force_reaction_or_catchphrase(self):
        plan = build_persona_expression_plan(
            self.atri,
            appraisal(AffectTrigger.NEUTRAL),
            principal=principal(),
        )

        self.assertTrue(plan.is_actionable)
        self.assertEqual(plan.catchphrase_candidates, ())
        self.assertEqual(plan.material_ids, ())
        self.assertEqual(plan.surface_behavior_ids, ("natural_direct_reply",))
        self.assertIn("forced_denial", plan.avoid_behavior_ids)

    def test_information_request_answers_first_without_forced_role_detour(self):
        plan = build_persona_expression_plan(
            self.atri,
            appraisal(AffectTrigger.INFORMATION_REQUEST),
            principal=principal(),
        )

        self.assertEqual(plan.material_ids, ("fact_answer_voice",))
        self.assertEqual(plan.catchphrase_candidates, ())
        self.assertEqual(
            plan.trajectory_steps,
            (
                "answer_current_question_first",
                "natural_voice_after_facts",
                "answer_target",
            ),
        )
        self.assertIn("answer_from_unrelated_memory", plan.avoid_behavior_ids)

    def test_praise_has_open_reaction_without_forced_catchphrase(self):
        plan = build_persona_expression_plan(
            self.atri,
            appraisal(AffectTrigger.PRAISE),
            principal=principal(),
        )

        self.assertEqual(plan.material_ids, ("praise_softening",))
        self.assertIn("openly_pleased_reaction", plan.trajectory_steps)
        self.assertIn("accept_or_invite_more", plan.trajectory_steps)
        self.assertIn("react_then_answer", plan.trajectory_steps)
        self.assertEqual(plan.catchphrase_candidates, ())
        self.assertIn("forced_praise_denial", plan.avoid_behavior_ids)
        self.assertIn("topic_abandonment", plan.avoid_behavior_ids)

        primary = build_persona_expression_plan(
            self.atri,
            appraisal(
                AffectTrigger.PRAISE,
                distance=RelationshipDistance.PRIMARY_BOND,
            ),
            principal=principal(RelationshipDistance.PRIMARY_BOND),
        )
        self.assertEqual(
            primary.material_ids,
            ("praise_softening", "primary_bond_intimacy"),
        )

    def test_mistake_arc_requires_real_repair_after_fluster(self):
        plan = build_persona_expression_plan(
            self.atri,
            appraisal(AffectTrigger.CORRECTION_OR_MISTAKE),
            principal=principal(),
        )

        self.assertEqual(plan.material_ids, ("mistake_repair",))
        self.assertEqual(
            plan.trajectory_steps,
            (
                "brief_flustered_acknowledgement",
                "plain_apology",
                "clear_correction",
                "repair_current_answer",
                "repair_then_continue",
            ),
        )
        self.assertEqual(plan.catchphrase_candidates, ())
        self.assertIn("excuse_before_repair", plan.avoid_behavior_ids)
        self.assertIn("machine_status_excuse", plan.avoid_behavior_ids)

    def test_care_and_apology_use_distinct_non_boasting_arcs(self):
        care = build_persona_expression_plan(
            self.atri,
            appraisal(AffectTrigger.USER_NEEDS_CARE),
            principal=principal(),
        )
        apology = build_persona_expression_plan(
            self.atri,
            appraisal(AffectTrigger.APOLOGY),
            principal=principal(),
        )

        self.assertIn("specific_comfort", care.trajectory_steps)
        self.assertIn("performance_boasting", care.avoid_behavior_ids)
        self.assertIn("accept_apology_naturally", apology.trajectory_steps)
        self.assertEqual(care.catchphrase_candidates, ())
        self.assertEqual(apology.catchphrase_candidates, ())
        self.assertNotEqual(care.trajectory_steps, apology.trajectory_steps)

    def test_recent_phrase_use_suppresses_repetition(self):
        plan = build_persona_expression_plan(
            self.atri,
            appraisal(AffectTrigger.PRAISE),
            principal=principal(),
            recent_visible_replies=("哼哼，我是高性能的嘛！",),
        )

        self.assertEqual(plan.catchphrase_candidates, ())

    def test_same_resolver_handles_non_atri_direct_style(self):
        plan = build_persona_expression_plan(
            self.alternate,
            appraisal(AffectTrigger.INFORMATION_REQUEST),
            principal=principal(),
        )

        self.assertTrue(plan.is_actionable)
        self.assertEqual(plan.package_id, "su_cheng_test")
        self.assertEqual(plan.material_ids, ("clear_answer",))
        self.assertEqual(plan.catchphrase_candidates, ())
        self.assertIn("answer_key_point_first", plan.trajectory_steps)

    def test_missing_character_trigger_requests_replan_instead_of_fallback_style(self):
        plan = build_persona_expression_plan(
            self.alternate,
            appraisal(AffectTrigger.PRAISE),
            principal=principal(),
        )

        self.assertFalse(plan.is_actionable)
        self.assertTrue(plan.requires_replan)
        self.assertIn("missing_emotion_expression_rule", plan.degradation_reasons)
        self.assertEqual(plan.trajectory_steps, ())

    def test_degraded_appraisal_requests_replan_without_expression(self):
        degraded = replace(
            appraisal(AffectTrigger.PRAISE),
            confidence=0.25,
            degradation_reasons=("principal_target_mismatch",),
        )

        plan = build_persona_expression_plan(
            self.atri,
            degraded,
            principal=principal(),
        )

        self.assertTrue(plan.requires_replan)
        self.assertIn("principal_target_mismatch", plan.degradation_reasons)
        self.assertIn("unactionable_affect_appraisal", plan.degradation_reasons)
        self.assertEqual(plan.catchphrase_candidates, ())

    def test_generic_resolver_has_no_role_specific_tokens(self):
        source = (ROOT / "core" / "persona_expression.py").read_text(encoding="utf-8")

        for token in ("亚托莉", "ATRI", "高性能机器人", "才没有", "校准失误"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
