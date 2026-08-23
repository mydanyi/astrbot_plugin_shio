import unittest
from pathlib import Path

from astrbot_plugin_shio.core.affect import (
    AffectTrigger,
    BehavioralTendency,
    HiddenConcern,
    RelationshipDistance,
    SurfaceEmotion,
    TopicReturn,
    appraise_affect,
)
from astrbot_plugin_shio.core.context_assembler import ReplyTarget
from astrbot_plugin_shio.core.conversation_ledger import ledger_content_digest
from astrbot_plugin_shio.core.identity import PrincipalContext


class AffectAppraisalTests(unittest.TestCase):
    scope = "platform:p|bot:b|group:g"

    @classmethod
    def principal(cls, *, owner=False, sender="u"):
        return PrincipalContext(
            sender_key=f"{cls.scope}|user:{sender}",
            sender_id=sender,
            is_owner=owner,
            relationship_role=("owner" if owner else "group_peer"),
            verification_source=(
                "astrbot_event_sender_id:configured_owner_id"
                if owner
                else "astrbot_event_sender_id:owner_allowlist_miss"
            ),
        )

    @classmethod
    def target(cls, message, *, sender="u"):
        return ReplyTarget(
            message_id="msg-1",
            sender_key=f"{cls.scope}|user:{sender}",
            session_id="g",
            scope_key=cls.scope,
            content_digest=ledger_content_digest(message),
            source_kind="current_inbound",
            referenced_message_id="",
            degradation_reasons=(),
        )

    def appraise(self, message, *, owner=False, sender="u", target_sender="u"):
        return appraise_affect(
            principal=self.principal(owner=owner, sender=sender),
            reply_target=self.target(message, sender=target_sender),
            current_message=message,
        )

    def test_neutral_question_stays_calm_and_answers_current_target(self):
        result = self.appraise("这个接口是什么意思？")

        self.assertEqual(result.trigger, AffectTrigger.INFORMATION_REQUEST)
        self.assertEqual(result.surface_emotion, SurfaceEmotion.CURIOUS)
        self.assertEqual(result.topic_return, TopicReturn.ANSWER_TARGET)
        self.assertEqual(
            result.behavioral_tendency,
            (BehavioralTendency.ANSWER_CURRENT_REQUEST,),
        )
        self.assertTrue(result.is_actionable)

    def test_praise_has_pleasure_and_embarrassment_without_fixed_dialogue(self):
        result = self.appraise("你今天做得真好，很厉害")

        self.assertEqual(result.trigger, AffectTrigger.PRAISE)
        self.assertEqual(result.surface_emotion, SurfaceEmotion.PLEASED)
        self.assertEqual(result.secondary_emotion, SurfaceEmotion.EMBARRASSED)
        self.assertEqual(result.hidden_concern, HiddenConcern.PROTECT_RELATIONSHIP)
        self.assertIn(BehavioralTendency.SOFTEN, result.behavioral_tendency)

    def test_being_seen_through_is_not_reduced_to_constant_denial(self):
        result = self.appraise("明明就很在意，还嘴硬")

        self.assertEqual(result.trigger, AffectTrigger.BEING_SEEN_THROUGH)
        self.assertIn(BehavioralTendency.SHOW_REACTION, result.behavioral_tendency)
        self.assertIn(BehavioralTendency.SOFTEN, result.behavioral_tendency)
        self.assertEqual(result.topic_return, TopicReturn.REACT_THEN_ANSWER)

    def test_direct_mild_taunt_is_playful_provocation_not_neutral(self):
        result = self.appraise("笨蛋都不会觉得自己是笨蛋而已")

        self.assertEqual(result.trigger, AffectTrigger.PLAYFUL_PROVOCATION)
        self.assertEqual(result.surface_emotion, SurfaceEmotion.STARTLED)
        self.assertEqual(result.secondary_emotion, SurfaceEmotion.EMBARRASSED)
        self.assertEqual(result.hidden_concern, HiddenConcern.RESPECT_BOUNDARY)
        self.assertIn(
            BehavioralTendency.MAINTAIN_BOUNDARY,
            result.behavioral_tendency,
        )

    def test_correction_prioritizes_acknowledgement_repair_and_continue(self):
        result = self.appraise("你刚才认错人了，回答也答非所问")

        self.assertEqual(result.trigger, AffectTrigger.CORRECTION_OR_MISTAKE)
        self.assertEqual(result.hidden_concern, HiddenConcern.RESTORE_TRUST)
        self.assertEqual(result.topic_return, TopicReturn.REPAIR_THEN_CONTINUE)
        self.assertIn(BehavioralTendency.REPAIR, result.behavioral_tendency)

    def test_user_distress_prioritizes_wellbeing_and_followup(self):
        result = self.appraise("我今天很难受，陪陪我")

        self.assertEqual(result.trigger, AffectTrigger.USER_NEEDS_CARE)
        self.assertEqual(result.surface_emotion, SurfaceEmotion.CONCERNED)
        self.assertEqual(result.hidden_concern, HiddenConcern.RECIPIENT_WELLBEING)
        self.assertEqual(result.topic_return, TopicReturn.CARE_THEN_FOLLOW_UP)

    def test_relationship_distance_comes_only_from_principal_context(self):
        owner = self.appraise("你好", owner=True, sender="owner", target_sender="owner")
        peer = self.appraise("你好")
        unverified = appraise_affect(
            principal=PrincipalContext("", "", False, "unverified", "text_claim"),
            reply_target=self.target("你好"),
            current_message="你好",
        )

        self.assertEqual(owner.relationship_distance, RelationshipDistance.PRIMARY_BOND)
        self.assertEqual(peer.relationship_distance, RelationshipDistance.PEER)
        self.assertEqual(
            unverified.relationship_distance,
            RelationshipDistance.UNVERIFIED,
        )
        self.assertFalse(unverified.is_actionable)

    def test_principal_target_mismatch_requests_replan_instead_of_guessing(self):
        result = self.appraise("继续说", sender="a", target_sender="b")

        self.assertIn("principal_target_mismatch", result.degradation_reasons)
        self.assertEqual(result.focus_sender_key, "")
        self.assertEqual(
            result.behavioral_tendency,
            (BehavioralTendency.REQUEST_REPLAN,),
        )
        self.assertEqual(result.topic_return, TopicReturn.REPLAN_CURRENT_TARGET)

    def test_changed_message_digest_requests_replan(self):
        result = appraise_affect(
            principal=self.principal(),
            reply_target=self.target("原问题"),
            current_message="后来换了一个问题",
        )

        self.assertIn("target_content_mismatch", result.degradation_reasons)
        self.assertFalse(result.is_actionable)

    def test_generic_affect_module_contains_no_character_specific_tokens(self):
        source = (Path(__file__).parents[1] / "core" / "affect.py").read_text(
            encoding="utf-8"
        )

        for token in ("亚托莉", "ATRI", "高性能机器人", "才没有"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
