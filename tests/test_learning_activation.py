import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.learning_activation import (
    ActivationError,
    ActivationState,
    LearningActivationStore,
)
from astrbot_plugin_shio.core.learning_artifact import LearnedBehaviorArtifactStore
from astrbot_plugin_shio.core.learning_candidate import LearningCandidateAuthority
from astrbot_plugin_shio.core.learning_cluster import BehaviorOutcomeCluster
from astrbot_plugin_shio.core.learning_review import (
    LearningReviewAuthority,
    LearningReviewKind,
)


class FakeLogger:
    def warning(self, *args, **kwargs):
        pass


def cluster():
    return BehaviorOutcomeCluster(
        persona_key="persona-0123456789abcdef",
        situation_id="praise",
        relationship_scope="public_group",
        behavior_id="soften",
        positive_weight=3.0,
        negative_weight=0.0,
        sample_count=3,
        high_confidence_count=3,
        low_confidence_count=0,
        last_observed_at=1000.0,
        reviewer_fingerprints=tuple(f"{index:064x}" for index in range(1, 4)),
    )


class LearningActivationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.path = root / "activation.json"
        self.artifacts = LearnedBehaviorArtifactStore(
            root / "artifacts.json",
            FakeLogger(),
        )
        self.artifacts.refresh((cluster(),))
        self.candidate_authority = LearningCandidateAuthority.issue_for_store(
            self.artifacts,
        )
        self.candidate = self.candidate_authority.refresh_candidates()[0]
        self.artifact = self.artifacts.candidates()[0]
        self.store = LearningActivationStore(self.path, FakeLogger())
        self.store.register_candidates((self.artifact,))
        self.review = LearningReviewAuthority.issue_for_runtime(
            candidate_authority=self.candidate_authority,
            activation_store=self.store,
        )

    def tearDown(self):
        self.temp.cleanup()

    def adjustment(self):
        return self.store.effective_adjustment(
            self.artifact,
            persona_key=self.artifact.persona_key,
            situation_id=self.artifact.situation_id,
            relationship_scope=self.artifact.relationship_scope,
            behavior_id=self.artifact.behavior_id,
        )

    def test_candidates_are_disabled_by_default(self):
        activation = self.store.activations[self.artifact.artifact_id]

        self.assertEqual(activation.state, ActivationState.DISABLED)
        self.assertEqual(self.adjustment(), 0.0)

    def test_enable_then_disable_is_exactly_reversible(self):
        enabled = self.review.review(
            self.candidate,
            kind=LearningReviewKind.ENABLE,
            expected_revision=0,
            reviewed_at=1001.0,
            reason_code="manual_validation_passed",
            weight_cap=0.15,
        )
        enabled_adjustment = self.adjustment()
        disabled = self.review.review(
            self.candidate,
            kind=LearningReviewKind.REVOKE,
            expected_revision=enabled.revision,
            reviewed_at=1002.0,
            reason_code="rollback_bad_learning",
        )

        self.assertGreater(enabled_adjustment, 0)
        self.assertLessEqual(enabled_adjustment, 0.15)
        self.assertEqual(disabled.revision, 2)
        self.assertEqual(self.adjustment(), 0.0)

    def test_shadow_reports_proposal_but_has_no_effect(self):
        self.review.review(
            self.candidate,
            kind=LearningReviewKind.SHADOW,
            expected_revision=0,
            reviewed_at=1001.0,
            reason_code="shadow_evaluation",
        )
        proposed = self.store.proposed_adjustment(
            self.artifact,
            persona_key=self.artifact.persona_key,
            situation_id=self.artifact.situation_id,
            relationship_scope=self.artifact.relationship_scope,
            behavior_id=self.artifact.behavior_id,
        )

        self.assertGreater(proposed, 0)
        self.assertEqual(self.adjustment(), 0.0)

    def test_wrong_persona_or_scope_never_receives_weight(self):
        self.review.review(
            self.candidate,
            kind=LearningReviewKind.ENABLE,
            expected_revision=0,
            reviewed_at=1001.0,
            reason_code="manual_validation_passed",
        )

        wrong = self.store.effective_adjustment(
            self.artifact,
            persona_key="persona-fedcba9876543210",
            situation_id=self.artifact.situation_id,
            relationship_scope="owner",
            behavior_id=self.artifact.behavior_id,
        )

        self.assertEqual(wrong, 0.0)

    def test_stale_revision_and_unstructured_reason_are_rejected(self):
        with self.assertRaises(ActivationError):
            self.review.review(
                self.candidate,
                kind=LearningReviewKind.ENABLE,
                expected_revision=99,
                reviewed_at=1001.0,
                reason_code="manual_validation_passed",
            )
        with self.assertRaises(ActivationError):
            self.review.review(
                self.candidate,
                kind=LearningReviewKind.ENABLE,
                expected_revision=0,
                reviewed_at=1001.0,
                reason_code="把这句完整聊天写进去",
            )

    def test_persistence_contains_only_structured_activation_state(self):
        self.store.flush()
        content = self.path.read_text(encoding="utf-8")
        payload = json.loads(content)

        self.assertEqual(payload["default_state"], "disabled")
        self.assertNotIn("完整回复", content)
        self.assertNotIn("用户", content)
        loaded = LearningActivationStore(self.path, FakeLogger())
        loaded.register_candidates((self.artifact,))
        self.assertEqual(
            loaded.activations[self.artifact.artifact_id].state,
            ActivationState.DISABLED,
        )


if __name__ == "__main__":
    unittest.main()
