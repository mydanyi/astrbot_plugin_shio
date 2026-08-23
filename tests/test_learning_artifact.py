import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from astrbot_plugin_shio.core.learning_artifact import (
    ArtifactValidationError,
    LearnedBehaviorArtifactStore,
    artifact_from_cluster,
    validate_learned_artifact,
)
from astrbot_plugin_shio.core.learning_cluster import BehaviorOutcomeCluster


class FakeLogger:
    def warning(self, *args, **kwargs):
        pass


def cluster(**overrides):
    values = {
        "persona_key": "persona-0123456789abcdef",
        "situation_id": "praise",
        "relationship_scope": "public_group",
        "behavior_id": "soften",
        "positive_weight": 3.0,
        "negative_weight": 1.0,
        "sample_count": 4,
        "high_confidence_count": 4,
        "low_confidence_count": 0,
        "last_observed_at": 1000.0,
        "reviewer_fingerprints": tuple(f"{index:064x}" for index in range(1, 5)),
    }
    values.update(overrides)
    return BehaviorOutcomeCluster(**values)


class LearnedBehaviorArtifactTests(unittest.TestCase):
    def test_artifact_has_complete_provenance_and_is_candidate_only(self):
        artifact = artifact_from_cluster(cluster())

        self.assertTrue(artifact.artifact_id.startswith("learned-"))
        self.assertEqual(artifact.source_kind, "aggregated_feedback_cluster")
        self.assertEqual(artifact.source_schema, "behavior_outcomes.v2")
        self.assertEqual(artifact.persona_key, "persona-0123456789abcdef")
        self.assertEqual(artifact.situation_id, "praise")
        self.assertEqual(artifact.relationship_scope, "public_group")
        self.assertEqual(artifact.behavior_id, "soften")
        self.assertEqual(artifact.sample_count, 4)
        self.assertEqual(artifact.activation_state, "candidate")
        self.assertGreater(artifact.confidence, 0)

    def test_single_sample_cluster_is_rejected(self):
        with self.assertRaises(ArtifactValidationError):
            artifact_from_cluster(
                cluster(
                    sample_count=1,
                    high_confidence_count=1,
                    low_confidence_count=0,
                    reviewer_fingerprints=(f"{1:064x}",),
                )
            )

    def test_missing_scope_inconsistent_counts_and_nonfinite_values_are_rejected(self):
        artifact = artifact_from_cluster(cluster())

        invalid = (
            replace(artifact, persona_key=""),
            replace(artifact, relationship_scope="unknown"),
            replace(artifact, sample_count=4, high_confidence_count=4, low_confidence_count=1),
            replace(artifact, score=math.nan),
            replace(artifact, activation_state="enabled"),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ArtifactValidationError):
                    validate_learned_artifact(value)

    def test_artifact_ids_separate_persona_and_relationship_scope(self):
        base = artifact_from_cluster(cluster())
        other_persona = artifact_from_cluster(
            cluster(persona_key="persona-fedcba9876543210")
        )
        owner = artifact_from_cluster(cluster(relationship_scope="owner"))

        self.assertEqual(len({base.artifact_id, other_persona.artifact_id, owner.artifact_id}), 3)

    def test_store_persists_validated_candidate_artifacts_without_raw_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "learned_behavior_artifacts.json"
            store = LearnedBehaviorArtifactStore(path, FakeLogger())
            store.refresh((cluster(),))
            store.flush()
            content = path.read_text(encoding="utf-8")
            payload = json.loads(content)

            self.assertEqual(payload["activation_policy"], "candidate_only")
            self.assertEqual(len(payload["artifacts"]), 1)
            self.assertNotIn("原始问题", content)
            self.assertNotIn("完整回复", content)
            self.assertNotIn("reviewer", content)
            self.assertNotIn("message_id", content)

            loaded = LearnedBehaviorArtifactStore(path, FakeLogger())
            self.assertEqual(len(loaded.candidates()), 1)

    def test_trace_does_not_expose_artifact_or_persona_values(self):
        artifact = artifact_from_cluster(cluster())
        rendered = repr(artifact.trace_metadata())

        self.assertNotIn(artifact.artifact_id, rendered)
        self.assertNotIn(artifact.persona_key, rendered)


if __name__ == "__main__":
    unittest.main()
