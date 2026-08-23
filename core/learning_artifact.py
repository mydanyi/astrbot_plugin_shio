from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .learning_cluster import BehaviorOutcomeCluster
from .observability import safe_exception_kind, structured_log


ARTIFACT_SCHEMA_VERSION = 1
ARTIFACT_SOURCE_KIND = "aggregated_feedback_cluster"
ARTIFACT_SOURCE_SCHEMA = "behavior_outcomes.v2"
ALLOWED_RELATIONSHIP_SCOPES = frozenset({"owner", "public_group", "peer"})
_OPAQUE_PERSONA_RE = re.compile(r"^persona-[0-9a-f]{16}$")
_STRUCTURED_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")


class ArtifactValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LearnedBehaviorArtifact:
    schema_version: int
    artifact_id: str
    source_kind: str
    source_schema: str
    persona_key: str
    situation_id: str
    relationship_scope: str
    behavior_id: str
    sample_count: int
    high_confidence_count: int
    low_confidence_count: int
    positive_weight: float
    negative_weight: float
    score: float
    confidence: float
    generated_at: float
    activation_state: str = "candidate"

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        return {
            "schema_version": self.schema_version,
            "source_kind": self.source_kind,
            "sample_count": self.sample_count,
            "high_confidence_count": self.high_confidence_count,
            "low_confidence_count": self.low_confidence_count,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "activation_state": self.activation_state,
            "has_persona_key": bool(self.persona_key),
            "has_situation_id": bool(self.situation_id),
            "has_relationship_scope": bool(self.relationship_scope),
            "has_behavior_id": bool(self.behavior_id),
        }


def _artifact_id(cluster: BehaviorOutcomeCluster) -> str:
    material = "\0".join(
        (
            str(ARTIFACT_SCHEMA_VERSION),
            cluster.persona_key,
            cluster.situation_id,
            cluster.relationship_scope,
            cluster.behavior_id,
        )
    )
    digest = hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"learned-{digest}"


def _confidence(cluster: BehaviorOutcomeCluster) -> float:
    sample_strength = min(1.0, math.log1p(cluster.sample_count) / math.log1p(20))
    high_ratio = cluster.high_confidence_count / max(1, cluster.sample_count)
    source_quality = 0.5 + 0.5 * high_ratio
    return max(0.0, min(1.0, sample_strength * source_quality))


def validate_learned_artifact(
    artifact: LearnedBehaviorArtifact,
    *,
    min_samples: int = 3,
) -> None:
    if type(artifact) is not LearnedBehaviorArtifact:
        raise ArtifactValidationError("artifact type is invalid")
    if type(artifact.schema_version) is not int or artifact.schema_version != ARTIFACT_SCHEMA_VERSION:
        raise ArtifactValidationError("unsupported artifact schema version")
    if not artifact.artifact_id.startswith("learned-"):
        raise ArtifactValidationError("invalid artifact ID")
    if artifact.source_kind != ARTIFACT_SOURCE_KIND:
        raise ArtifactValidationError("invalid artifact source kind")
    if artifact.source_schema != ARTIFACT_SOURCE_SCHEMA:
        raise ArtifactValidationError("invalid artifact source schema")
    if type(artifact.persona_key) is not str or not _OPAQUE_PERSONA_RE.fullmatch(
        artifact.persona_key
    ):
        raise ArtifactValidationError("artifact requires an opaque persona key")
    if (
        type(artifact.situation_id) is not str
        or not _STRUCTURED_TOKEN_RE.fullmatch(artifact.situation_id)
        or type(artifact.behavior_id) is not str
        or not _STRUCTURED_TOKEN_RE.fullmatch(artifact.behavior_id)
    ):
        raise ArtifactValidationError("artifact applicability is incomplete")
    if (
        type(artifact.relationship_scope) is not str
        or artifact.relationship_scope not in ALLOWED_RELATIONSHIP_SCOPES
    ):
        raise ArtifactValidationError("artifact relationship scope is invalid")
    for value in (
        artifact.artifact_id,
        artifact.source_kind,
        artifact.source_schema,
        artifact.activation_state,
    ):
        if type(value) is not str:
            raise ArtifactValidationError("artifact string field type is invalid")
    for value in (
        artifact.sample_count,
        artifact.high_confidence_count,
        artifact.low_confidence_count,
    ):
        if type(value) is not int:
            raise ArtifactValidationError("artifact count field type is invalid")
    for value in (
        artifact.positive_weight,
        artifact.negative_weight,
        artifact.score,
        artifact.confidence,
        artifact.generated_at,
    ):
        if type(value) is not float:
            raise ArtifactValidationError("artifact numeric field type is invalid")
    if artifact.sample_count < max(2, int(min_samples)):
        raise ArtifactValidationError("artifact does not meet minimum sample threshold")
    if (
        artifact.high_confidence_count != artifact.sample_count
        or artifact.low_confidence_count != 0
    ):
        raise ArtifactValidationError("artifact requires diverse high-confidence samples")
    numeric = (
        artifact.positive_weight,
        artifact.negative_weight,
        artifact.score,
        artifact.confidence,
        artifact.generated_at,
    )
    if not all(math.isfinite(value) for value in numeric):
        raise ArtifactValidationError("artifact contains non-finite numeric values")
    if artifact.positive_weight < 0 or artifact.negative_weight < 0:
        raise ArtifactValidationError("artifact weights cannot be negative")
    if artifact.positive_weight + artifact.negative_weight <= 0:
        raise ArtifactValidationError("artifact requires outcome weight")
    if not -1.0 <= artifact.score <= 1.0:
        raise ArtifactValidationError("artifact score is out of range")
    if not 0.0 <= artifact.confidence <= 1.0:
        raise ArtifactValidationError("artifact confidence is out of range")
    if artifact.generated_at <= 0:
        raise ArtifactValidationError("artifact generation time is missing")
    if artifact.activation_state != "candidate":
        raise ArtifactValidationError("new learned artifacts must remain candidates")


def artifact_snapshot(artifact: LearnedBehaviorArtifact) -> tuple[object, ...]:
    """Return the complete validated candidate-only provenance snapshot."""

    try:
        validate_learned_artifact(artifact)
        return (
            artifact.schema_version,
            artifact.artifact_id,
            artifact.source_kind,
            artifact.source_schema,
            artifact.persona_key,
            artifact.situation_id,
            artifact.relationship_scope,
            artifact.behavior_id,
            artifact.sample_count,
            artifact.high_confidence_count,
            artifact.low_confidence_count,
            artifact.positive_weight,
            artifact.negative_weight,
            artifact.score,
            artifact.confidence,
            artifact.generated_at,
            artifact.activation_state,
        )
    except (AttributeError, TypeError) as exc:
        raise ArtifactValidationError("artifact is corrupt") from exc


def artifact_from_cluster(
    cluster: BehaviorOutcomeCluster,
    *,
    min_samples: int = 3,
) -> LearnedBehaviorArtifact:
    if (
        type(cluster) is not BehaviorOutcomeCluster
        or cluster.reviewer_count < max(2, int(min_samples))
        or cluster.high_confidence_count != cluster.sample_count
        or cluster.low_confidence_count != 0
    ):
        raise ArtifactValidationError("cluster lacks poisoning-resistant provenance")
    artifact = LearnedBehaviorArtifact(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        artifact_id=_artifact_id(cluster),
        source_kind=ARTIFACT_SOURCE_KIND,
        source_schema=ARTIFACT_SOURCE_SCHEMA,
        persona_key=cluster.persona_key,
        situation_id=cluster.situation_id,
        relationship_scope=cluster.relationship_scope,
        behavior_id=cluster.behavior_id,
        sample_count=cluster.sample_count,
        high_confidence_count=cluster.high_confidence_count,
        low_confidence_count=cluster.low_confidence_count,
        positive_weight=round(cluster.positive_weight, 6),
        negative_weight=round(cluster.negative_weight, 6),
        score=round(cluster.score, 6),
        confidence=round(_confidence(cluster), 6),
        generated_at=cluster.last_observed_at,
    )
    validate_learned_artifact(artifact, min_samples=min_samples)
    return artifact


class LearnedBehaviorArtifactStore:
    def __init__(self, path: Path, logger: Any, *, min_samples: int = 3) -> None:
        self.path = Path(path)
        self.logger = logger
        self.min_samples = max(2, int(min_samples))
        self.artifacts: dict[str, LearnedBehaviorArtifact] = {}
        self._artifact_snapshots: dict[
            str,
            tuple[LearnedBehaviorArtifact, tuple[object, ...]],
        ] = {}
        self._dirty = False
        self._load()

    def refresh(self, clusters: Iterable[BehaviorOutcomeCluster]) -> None:
        next_artifacts: dict[str, LearnedBehaviorArtifact] = {}
        for cluster in clusters:
            try:
                artifact = artifact_from_cluster(
                    cluster,
                    min_samples=self.min_samples,
                )
            except ArtifactValidationError:
                continue
            existing = next_artifacts.get(artifact.artifact_id)
            if existing is not None and existing != artifact:
                raise ArtifactValidationError(
                    "artifact ID collision across applicability scopes"
                )
            next_artifacts[artifact.artifact_id] = artifact
        if next_artifacts != self.artifacts:
            self.artifacts = next_artifacts
            self._artifact_snapshots = {
                artifact_id: (artifact, artifact_snapshot(artifact))
                for artifact_id, artifact in next_artifacts.items()
            }
            self._dirty = True

    def inspect_candidate(
        self,
        artifact: LearnedBehaviorArtifact,
    ) -> LearnedBehaviorArtifact:
        if type(artifact) is not LearnedBehaviorArtifact:
            raise ArtifactValidationError("artifact_not_canonical")
        try:
            artifact_id = artifact.artifact_id
            current = artifact_snapshot(artifact)
        except (AttributeError, TypeError, ArtifactValidationError) as exc:
            raise ArtifactValidationError("artifact_corrupt") from exc
        record = self._artifact_snapshots.get(artifact_id)
        if (
            record is None
            or self.artifacts.get(artifact_id) is not artifact
            or record[0] is not artifact
        ):
            raise ArtifactValidationError("artifact_not_canonical")
        if record[1] != current:
            raise ArtifactValidationError("artifact_corrupt")
        return artifact

    def candidates(
        self,
        *,
        persona_key: str = "",
        situation_id: str = "",
        relationship_scope: str = "",
    ) -> tuple[LearnedBehaviorArtifact, ...]:
        return tuple(
            sorted(
                (
                    artifact
                    for artifact in self.artifacts.values()
                    if (not persona_key or artifact.persona_key == persona_key)
                    and (not situation_id or artifact.situation_id == situation_id)
                    and (
                        not relationship_scope
                        or artifact.relationship_scope == relationship_scope
                    )
                ),
                key=lambda item: item.artifact_id,
            )
        )

    def flush(self) -> None:
        if not self._dirty:
            return
        payload = {
            "version": ARTIFACT_SCHEMA_VERSION,
            "activation_policy": "candidate_only",
            "artifacts": [
                asdict(artifact)
                for artifact in sorted(
                    self.artifacts.values(),
                    key=lambda item: item.artifact_id,
                )
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
            self._dirty = False
        except Exception as exc:
            structured_log(
                self.logger,
                "warning",
                "learning.artifact_save_failed",
                failure_kind=safe_exception_kind(exc),
            )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("activation_policy") != "candidate_only":
                return
            rows = payload.get("artifacts", [])
            if not isinstance(rows, list):
                return
            loaded: dict[str, LearnedBehaviorArtifact] = {}
            for value in rows[:2048]:
                if not isinstance(value, dict):
                    continue
                try:
                    artifact = LearnedBehaviorArtifact(**value)
                    validate_learned_artifact(
                        artifact,
                        min_samples=self.min_samples,
                    )
                except (ArtifactValidationError, TypeError, ValueError):
                    continue
                if artifact.artifact_id in loaded:
                    continue
                loaded[artifact.artifact_id] = artifact
            self.artifacts = loaded
            self._artifact_snapshots = {
                artifact_id: (artifact, artifact_snapshot(artifact))
                for artifact_id, artifact in loaded.items()
            }
        except Exception as exc:
            structured_log(
                self.logger,
                "warning",
                "learning.artifact_load_failed",
                failure_kind=safe_exception_kind(exc),
            )
