from __future__ import annotations

import copy
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


class _Logger:
    def warning(self, *_args, **_kwargs):
        return None


def _cluster() -> BehaviorOutcomeCluster:
    return BehaviorOutcomeCluster(
        persona_key="persona-0123456789abcdef",
        situation_id="gratitude",
        relationship_scope="peer",
        behavior_id="warm_acknowledgement",
        positive_weight=3.0,
        negative_weight=0.0,
        sample_count=3,
        high_confidence_count=3,
        low_confidence_count=0,
        last_observed_at=100.0,
        reviewer_fingerprints=tuple(f"{index:064x}" for index in range(1, 4)),
    )


class P8LearningReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.artifacts = LearnedBehaviorArtifactStore(root / "artifacts.json", _Logger())
        self.artifacts.refresh((_cluster(),))
        self.candidates = LearningCandidateAuthority.issue_for_store(self.artifacts)
        self.candidate = self.candidates.refresh_candidates()[0]
        self.activations = LearningActivationStore(root / "activation.json", _Logger())
        self.activations.register_candidates(self.artifacts.candidates())
        self.review = LearningReviewAuthority.issue_for_runtime(
            candidate_authority=self.candidates,
            activation_store=self.activations,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _adjustments(self) -> tuple[float, float]:
        return self.review.adjustments_for(self.candidate)

    def test_default_shadow_enable_and_revoke_are_explicit_and_bounded(self):
        self.assertEqual(self._adjustments(), (0.0, 0.0))
        shadow = self.review.review(
            self.candidate,
            kind=LearningReviewKind.SHADOW,
            expected_revision=0,
            reviewed_at=110.0,
            reason_code="manual_shadow_review",
            weight_cap=0.1,
        )
        self.assertIs(shadow.state, ActivationState.SHADOW)
        proposed, effective = self._adjustments()
        self.assertGreater(proposed, 0.0)
        self.assertEqual(effective, 0.0)

        enabled = self.review.review(
            self.candidate,
            kind=LearningReviewKind.ENABLE,
            expected_revision=1,
            reviewed_at=120.0,
            reason_code="manual_enable_review",
            weight_cap=0.1,
        )
        self.assertIs(enabled.state, ActivationState.ENABLED)
        proposed, effective = self._adjustments()
        self.assertGreater(effective, 0.0)
        self.assertLessEqual(abs(effective), 0.1)
        self.assertEqual(proposed, effective)

        revoked = self.review.review(
            self.candidate,
            kind=LearningReviewKind.REVOKE,
            expected_revision=2,
            reviewed_at=130.0,
            reason_code="manual_revoke",
        )
        self.assertIs(revoked.state, ActivationState.DISABLED)
        self.assertEqual(self._adjustments(), (0.0, 0.0))

    def test_raw_artifact_id_activation_is_permanently_closed(self):
        with self.assertRaisesRegex(ActivationError, "review_authority_required"):
            self.activations.set_state(
                self.candidate.candidate_id,
                state=ActivationState.ENABLED,
                expected_revision=0,
                updated_at=110.0,
                reason_code="bypass",
            )

        with self.assertRaises(TypeError):
            self.activations._apply_candidate_review(
                self.candidate,
                self.candidates,
                state=ActivationState.ENABLED,
                expected_revision=0,
                updated_at=110.0,
                reason_code="missing_review_authority",
            )

        with self.assertRaisesRegex(ActivationError, "review_authority_not_canonical"):
            self.activations._apply_candidate_review(
                copy.copy(self.review),
                self.candidate,
                self.candidates,
                state=ActivationState.ENABLED,
                expected_revision=0,
                updated_at=110.0,
                reason_code="copied_review_authority",
            )

    def test_exact_candidate_cross_runtime_copy_and_stale_revision_are_rejected(self):
        with self.assertRaisesRegex(Exception, "candidate_not_canonical"):
            self.review.review(
                copy.copy(self.candidate),
                kind=LearningReviewKind.SHADOW,
                expected_revision=0,
                reviewed_at=110.0,
                reason_code="copy_attempt",
            )
        first = self.review.review(
            self.candidate,
            kind=LearningReviewKind.SHADOW,
            expected_revision=0,
            reviewed_at=110.0,
            reason_code="first_review",
        )
        self.assertEqual(first.revision, 1)
        with self.assertRaisesRegex(ActivationError, "stale activation revision"):
            self.review.review(
                self.candidate,
                kind=LearningReviewKind.ENABLE,
                expected_revision=0,
                reviewed_at=120.0,
                reason_code="stale_review",
            )

    def test_activation_mutation_or_mapping_substitution_fails_closed(self):
        activation = self.review.review(
            self.candidate,
            kind=LearningReviewKind.ENABLE,
            expected_revision=0,
            reviewed_at=110.0,
            reason_code="manual_enable_review",
        )
        object.__setattr__(activation, "state", ActivationState.DISABLED)
        self.assertEqual(self._adjustments(), (0.0, 0.0))
        object.__setattr__(activation, "state", ActivationState.ENABLED)
        self.assertGreater(self._adjustments()[1], 0.0)

        self.activations.activations[activation.artifact_id] = copy.copy(activation)
        self.assertEqual(self._adjustments(), (0.0, 0.0))
        self.activations.activations[activation.artifact_id] = activation
        self.assertGreater(self._adjustments()[1], 0.0)

    def test_persistence_preserves_review_but_never_changes_candidate_status(self):
        activation = self.review.review(
            self.candidate,
            kind=LearningReviewKind.SHADOW,
            expected_revision=0,
            reviewed_at=110.0,
            reason_code="manual_shadow_review",
        )
        self.activations.flush()
        reloaded = LearningActivationStore(self.activations.path, _Logger())
        reloaded.register_candidates(self.artifacts.candidates())
        review = LearningReviewAuthority.issue_for_runtime(
            candidate_authority=self.candidates,
            activation_store=reloaded,
        )
        self.assertEqual(review.adjustments_for(self.candidate)[1], 0.0)
        self.assertEqual(
            reloaded.activations[activation.artifact_id].state,
            ActivationState.SHADOW,
        )
        self.assertEqual(self.candidate.status.value, "candidate")

    def test_enabled_state_is_restart_revoked_and_private_snapshot_map_is_absent(self):
        activation = self.review.review(
            self.candidate,
            kind=LearningReviewKind.ENABLE,
            expected_revision=0,
            reviewed_at=110.0,
            reason_code="bounded_live_enable",
            weight_cap=0.15,
        )
        self.assertIs(activation.state, ActivationState.ENABLED)
        self.assertFalse(hasattr(self.activations, "_activation_snapshots"))
        self.activations.flush()

        reloaded = LearningActivationStore(self.activations.path, _Logger())
        reloaded.register_candidates(self.artifacts.candidates())
        review = LearningReviewAuthority.issue_for_runtime(
            candidate_authority=self.candidates,
            activation_store=reloaded,
        )
        self.assertIs(
            reloaded.activations[activation.artifact_id].state,
            ActivationState.DISABLED,
        )
        self.assertEqual(review.adjustments_for(self.candidate), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
