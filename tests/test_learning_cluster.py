import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.feedback import (
    FeedbackConfidence,
    FeedbackSignal,
    classify_feedback_evidence,
)
from astrbot_plugin_shio.core.learning_cluster import (
    BehaviorOutcomeClusterStore,
    make_learning_context,
    persona_learning_key,
)
from astrbot_plugin_shio.core.learning_poison_guard import (
    LearningPoisoningGuard,
    LearningPoisoningRejected,
)


class FakeLogger:
    def warning(self, *args, **kwargs):
        pass


class BehaviorOutcomeClusterStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "behavior_outcomes.json"
        self.guard = LearningPoisoningGuard.issue_for_root(self.root, FakeLogger())
        self.store = BehaviorOutcomeClusterStore(
            self.path,
            FakeLogger(),
            poison_guard=self.guard,
        )
        self.context = make_learning_context(
            persona_key=persona_learning_key("测试角色", "角色声音卡全文"),
            situation_id="praise",
            relationship_scope="public_group",
            behavior_ids=("soften", "return_to_topic"),
        )

    def tearDown(self):
        self.temp.cleanup()

    def observe(
        self,
        *,
        context=None,
        reviewer="private-reviewer",
        signal=FeedbackSignal.POSITIVE,
        observed_at=1000.0,
    ):
        evidence = classify_feedback_evidence(
            reviewer_key=reviewer,
            target_identity_key=reviewer,
            reply_id="private-reply",
            signal=signal,
        )
        admission = self.guard.admit(
            cluster_store=self.store,
            context=context or self.context,
            evidence=evidence,
            feedback_text="真棒！" if signal is FeedbackSignal.POSITIVE else "不对！",
            observed_at=float(observed_at),
        )
        self.store.observe_admission(admission, self.guard)

    def test_single_sample_cannot_form_eligible_cluster(self):
        self.observe()

        self.assertEqual(self.store.eligible_clusters(), ())

    def test_three_matching_samples_form_stable_behavior_clusters(self):
        for index, timestamp in enumerate((1000, 1001, 1002)):
            self.observe(
                reviewer=f"private-reviewer-{index}",
                observed_at=timestamp,
            )

        clusters = self.store.eligible_clusters(persona_key=self.context.persona_key)

        self.assertEqual(len(clusters), 2)
        self.assertEqual({item.behavior_id for item in clusters}, {
            "soften",
            "return_to_topic",
        })
        self.assertTrue(all(item.sample_count == 3 for item in clusters))
        self.assertTrue(all(item.score == 1.0 for item in clusters))

    def test_persona_situation_relationship_and_behavior_are_separate_buckets(self):
        contexts = (
            self.context,
            make_learning_context(
                persona_key="persona-fedcba9876543210",
                situation_id="praise",
                relationship_scope="public_group",
                behavior_ids=("soften",),
            ),
            make_learning_context(
                persona_key=self.context.persona_key,
                situation_id="correction_or_mistake",
                relationship_scope="public_group",
                behavior_ids=("soften",),
            ),
            make_learning_context(
                persona_key=self.context.persona_key,
                situation_id="praise",
                relationship_scope="owner",
                behavior_ids=("soften",),
            ),
        )
        for index, context in enumerate(contexts):
            self.observe(
                context=context,
                reviewer=f"private-reviewer-{index}",
                observed_at=1000,
            )

        self.assertEqual(len(self.store.clusters), 5)

    def test_low_confidence_group_result_cannot_enter_learning_cluster(self):
        evidence = classify_feedback_evidence(
            reviewer_key="private-reviewer",
            target_identity_key="different-target",
            reply_id="private-reply",
            signal=FeedbackSignal.POSITIVE,
        )
        self.assertIs(evidence.confidence, FeedbackConfidence.LOW)
        with self.assertRaisesRegex(
            LearningPoisoningRejected,
            "learning_feedback_not_target_high_confidence",
        ):
            self.guard.admit(
                cluster_store=self.store,
                context=self.context,
                evidence=evidence,
                feedback_text="真棒！",
                observed_at=1000.0,
            )
        self.assertEqual(self.store.clusters, {})

    def test_persistence_contains_no_chat_voice_card_or_identity_values(self):
        for index in range(3):
            self.observe(
                reviewer=f"private-reviewer-{index}",
                observed_at=1000 + index,
            )
        self.store.flush()
        content = self.path.read_text(encoding="utf-8")
        payload = json.loads(content)

        self.assertEqual(payload["version"], 2)
        self.assertNotIn("测试角色", content)
        self.assertNotIn("角色声音卡全文", content)
        self.assertNotIn("private-reviewer", content)
        self.assertNotIn("private-reply", content)
        self.assertNotIn("完整回复台词", content)

        reloaded = BehaviorOutcomeClusterStore(
            self.path,
            FakeLogger(),
            poison_guard=self.guard,
        )
        self.assertEqual(len(reloaded.clusters), 2)


if __name__ == "__main__":
    unittest.main()
