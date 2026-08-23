from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.learning_artifact import LearnedBehaviorArtifactStore
from astrbot_plugin_shio.core.learning_candidate import (
    CandidatePrivacyClass,
    CandidateSourceKind,
    CandidateStatus,
    LearningCandidateAuthority,
)
from astrbot_plugin_shio.core.learning_cluster import BehaviorOutcomeCluster
from astrbot_plugin_shio.core.conversation_runtime import ConversationRuntime
from astrbot_plugin_shio.core.feedback import (
    FeedbackSignal,
    classify_feedback_evidence,
)
from astrbot_plugin_shio.core.learning_cluster import (
    make_learning_context,
)


class _Logger:
    def warning(self, *_args, **_kwargs):
        return None


def _cluster(**overrides) -> BehaviorOutcomeCluster:
    values = {
        "persona_key": "persona-0123456789abcdef",
        "situation_id": "gratitude",
        "relationship_scope": "peer",
        "behavior_id": "warm_acknowledgement",
        "positive_weight": 3.0,
        "negative_weight": 0.0,
        "sample_count": 3,
        "high_confidence_count": 3,
        "low_confidence_count": 0,
        "last_observed_at": 100.0,
        "reviewer_fingerprints": tuple(f"{index:064x}" for index in range(1, 4)),
    }
    values.update(overrides)
    return BehaviorOutcomeCluster(**values)


class P8LearningCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearnedBehaviorArtifactStore(
            Path(self.temp.name) / "candidate.json",
            _Logger(),
        )
        self.store.refresh((_cluster(),))
        self.authority = LearningCandidateAuthority.issue_for_store(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_candidate_has_complete_aggregated_provenance_and_is_never_active(self):
        candidates = self.authority.refresh_candidates()
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertIs(candidate.status, CandidateStatus.CANDIDATE)
        self.assertIs(
            candidate.source_kind,
            CandidateSourceKind.AGGREGATED_FEEDBACK_CLUSTER,
        )
        self.assertIs(
            candidate.privacy_class,
            CandidatePrivacyClass.AGGREGATED_ONLY,
        )
        self.assertEqual(candidate.persona_key, "persona-0123456789abcdef")
        self.assertEqual(candidate.situation_id, "gratitude")
        self.assertEqual(candidate.relationship_scope, "peer")
        self.assertEqual(candidate.behavior_id, "warm_acknowledgement")
        self.assertEqual(candidate.sample_count, 3)
        self.assertEqual(candidate.high_confidence_count, 3)
        self.assertGreater(candidate.confidence, 0.0)
        self.assertFalse(hasattr(self.authority, "enable"))
        self.assertFalse(hasattr(candidate, "prompt_text"))

    def test_exact_store_artifact_and_candidate_identity_are_required(self):
        candidate = self.authority.refresh_candidates()[0]
        self.assertIs(self.authority.inspect_candidate(candidate), candidate)
        with self.assertRaisesRegex(Exception, "candidate_not_canonical"):
            self.authority.inspect_candidate(copy.copy(candidate))

        original = candidate.status
        object.__setattr__(candidate, "status", "enabled")
        with self.assertRaisesRegex(Exception, "candidate_corrupt"):
            self.authority.inspect_candidate(candidate)
        object.__setattr__(candidate, "status", original)
        self.assertIs(self.authority.inspect_candidate(candidate), candidate)

        artifact = self.store.candidates()[0]
        self.store.artifacts[artifact.artifact_id] = copy.copy(artifact)
        with self.assertRaisesRegex(Exception, "artifact_not_canonical"):
            self.authority.refresh_candidates()
        self.store.artifacts[artifact.artifact_id] = artifact
        self.assertIs(self.authority.refresh_candidates()[0], candidate)

    def test_authority_is_unique_per_store_and_cross_authority_is_rejected(self):
        self.assertIs(
            LearningCandidateAuthority.issue_for_store(self.store),
            self.authority,
        )
        with tempfile.TemporaryDirectory() as other_dir:
            other_store = LearnedBehaviorArtifactStore(
                Path(other_dir) / "candidate.json",
                _Logger(),
            )
            other_store.refresh((_cluster(),))
            other = LearningCandidateAuthority.issue_for_store(other_store)
            candidate = self.authority.refresh_candidates()[0]
            with self.assertRaisesRegex(Exception, "candidate_not_canonical"):
                other.inspect_candidate(candidate)

    def test_privacy_filter_rejects_raw_text_path_url_and_identity_like_fields(self):
        unsafe = (
            _cluster(behavior_id="C:\\secret\\token.txt"),
            _cluster(behavior_id="https://example.invalid/private"),
            _cluster(situation_id="用户原话 谢谢你"),
            _cluster(persona_key="real-person-name"),
        )
        for index, cluster in enumerate(unsafe):
            with self.subTest(index=index):
                with tempfile.TemporaryDirectory() as other_dir:
                    store = LearnedBehaviorArtifactStore(
                        Path(other_dir) / "candidate.json",
                        _Logger(),
                    )
                    store.refresh((cluster,))
                    authority = LearningCandidateAuthority.issue_for_store(store)
                    self.assertEqual(authority.refresh_candidates(), ())

    def test_trace_is_bounded_and_contains_no_applicability_identifiers(self):
        candidate = self.authority.refresh_candidates()[0]
        rendered = repr(candidate) + json.dumps(
            candidate.trace_metadata(),
            ensure_ascii=False,
        )
        for forbidden in (
            candidate.persona_key,
            candidate.situation_id,
            candidate.relationship_scope,
            candidate.behavior_id,
        ):
            self.assertNotIn(forbidden, rendered)
        metadata = self.authority.trace_metadata()
        self.assertEqual(metadata["candidate_count"], 1)
        self.assertTrue(metadata["candidate_only"])

    def test_conversation_runtime_flush_exposes_candidate_without_auto_activation(self):
        with tempfile.TemporaryDirectory() as runtime_dir:
            runtime = ConversationRuntime(
                Path(runtime_dir),
                _Logger(),
                now_fn=lambda: 100.0,
            )
            context = make_learning_context(
                persona_key="persona-0123456789abcdef",
                situation_id="gratitude",
                relationship_scope="peer",
                behavior_ids=("warm_acknowledgement",),
            )
            for index in range(3):
                reviewer = f"scope/reviewer-{index}"
                evidence = classify_feedback_evidence(
                    reviewer_key=reviewer,
                    target_identity_key=reviewer,
                    reply_id=f"reply-{index}",
                    signal=FeedbackSignal.POSITIVE,
                )
                admission = runtime.learning_poison_guard.admit(
                    cluster_store=runtime.behavior_learning,
                    context=context,
                    evidence=evidence,
                    feedback_text="真棒！",
                    observed_at=100.0 + index,
                )
                runtime.behavior_learning.observe_admission(
                    admission,
                    runtime.learning_poison_guard,
                )
            runtime.flush()
            candidates = runtime.learning_candidates()
            self.assertEqual(len(candidates), 1)
            self.assertIs(candidates[0].status, CandidateStatus.CANDIDATE)
            activation = runtime.learning_activation.activations[
                candidates[0].candidate_id
            ]
            self.assertEqual(activation.state.value, "disabled")
            self.assertEqual(
                runtime.expression_feedback_scores(
                    scope_key="scope",
                    persona_key=candidates[0].persona_key,
                    situation_id=candidates[0].situation_id,
                    relationship_scope=candidates[0].relationship_scope,
                ).get(candidates[0].behavior_id, 0.0),
                0.0,
            )


if __name__ == "__main__":
    unittest.main()
