from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass, field
from enum import Enum

from .learning_artifact import (
    ARTIFACT_SOURCE_SCHEMA,
    LearnedBehaviorArtifact,
    LearnedBehaviorArtifactStore,
    artifact_snapshot,
)


class LearningCandidateError(ValueError):
    pass


class CandidateStatus(str, Enum):
    CANDIDATE = "candidate"


class CandidateSourceKind(str, Enum):
    AGGREGATED_FEEDBACK_CLUSTER = "aggregated_feedback_cluster"


class CandidatePrivacyClass(str, Enum):
    AGGREGATED_ONLY = "aggregated_only"


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    eq=False,
    repr=False,
    weakref_slot=True,
)
class LearningCandidate:
    candidate_id: str = field(repr=False)
    source_kind: CandidateSourceKind
    source_schema: str
    privacy_class: CandidatePrivacyClass
    status: CandidateStatus
    persona_key: str = field(repr=False)
    situation_id: str = field(repr=False)
    relationship_scope: str = field(repr=False)
    behavior_id: str = field(repr=False)
    sample_count: int
    high_confidence_count: int
    low_confidence_count: int
    positive_weight: float
    negative_weight: float
    score: float
    confidence: float
    generated_at: float = field(repr=False)

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        try:
            _inspect_candidate_any(self)
        except Exception:
            return {"learning_candidate_canonical": False}
        return {
            "learning_candidate_canonical": True,
            "source_kind": self.source_kind.value,
            "privacy_class": self.privacy_class.value,
            "status": self.status.value,
            "sample_count": self.sample_count,
            "high_confidence_count": self.high_confidence_count,
            "low_confidence_count": self.low_confidence_count,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "has_persona_scope": True,
            "has_situation_scope": True,
            "has_relationship_scope": True,
            "has_behavior_scope": True,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        if not metadata.get("learning_candidate_canonical", False):
            return "LearningCandidate(canonical=False)"
        return (
            "LearningCandidate(canonical=True, status='candidate', "
            f"sample_count={metadata['sample_count']!r}, "
            f"confidence={metadata['confidence']!r}, content_visible=False)"
        )


def _candidate_snapshot(candidate: LearningCandidate) -> tuple[object, ...]:
    if type(candidate) is not LearningCandidate:
        raise LearningCandidateError("candidate_not_canonical")
    try:
        snapshot = (
            candidate.candidate_id,
            candidate.source_kind,
            candidate.source_schema,
            candidate.privacy_class,
            candidate.status,
            candidate.persona_key,
            candidate.situation_id,
            candidate.relationship_scope,
            candidate.behavior_id,
            candidate.sample_count,
            candidate.high_confidence_count,
            candidate.low_confidence_count,
            candidate.positive_weight,
            candidate.negative_weight,
            candidate.score,
            candidate.confidence,
            candidate.generated_at,
        )
    except (AttributeError, TypeError) as exc:
        raise LearningCandidateError("candidate_corrupt") from exc
    if (
        type(snapshot[0]) is not str
        or not snapshot[0].startswith("learned-")
        or type(snapshot[1]) is not CandidateSourceKind
        or snapshot[1] is not CandidateSourceKind.AGGREGATED_FEEDBACK_CLUSTER
        or type(snapshot[2]) is not str
        or snapshot[2] != ARTIFACT_SOURCE_SCHEMA
        or type(snapshot[3]) is not CandidatePrivacyClass
        or snapshot[3] is not CandidatePrivacyClass.AGGREGATED_ONLY
        or type(snapshot[4]) is not CandidateStatus
        or snapshot[4] is not CandidateStatus.CANDIDATE
        or any(type(value) is not str for value in snapshot[5:9])
        or any(type(value) is not int for value in snapshot[9:12])
        or any(type(value) is not float for value in snapshot[12:17])
        or snapshot[9] < 3
        or snapshot[10] < 0
        or snapshot[11] < 0
        or snapshot[10] + snapshot[11] != snapshot[9]
        or not 0.0 <= snapshot[15] <= 1.0
    ):
        raise LearningCandidateError("candidate_corrupt")
    return snapshot


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    eq=False,
    repr=False,
    weakref_slot=True,
)
class LearningCandidateAuthority:
    _store_ref: weakref.ReferenceType[LearnedBehaviorArtifactStore] = field(
        repr=False,
    )

    @classmethod
    def issue_for_store(
        cls,
        store: LearnedBehaviorArtifactStore,
    ) -> "LearningCandidateAuthority":
        return _issue_authority(store)

    def refresh_candidates(self) -> tuple[LearningCandidate, ...]:
        return _refresh_candidates(self)

    def inspect_candidate(
        self,
        candidate: LearningCandidate,
    ) -> LearningCandidate:
        return _inspect_candidate(self, candidate)

    def trace_metadata(self) -> dict[str, int | bool]:
        return _authority_trace(self)

    def __repr__(self) -> str:
        try:
            metadata = self.trace_metadata()
        except Exception:
            return "LearningCandidateAuthority(canonical=False)"
        return (
            "LearningCandidateAuthority(canonical=True, candidate_only=True, "
            f"candidate_count={metadata['candidate_count']!r})"
        )


def _build_candidate_vault():
    lock = threading.RLock()
    authorities: tuple[
        tuple[
            weakref.ReferenceType[LearnedBehaviorArtifactStore],
            weakref.ReferenceType[LearningCandidateAuthority],
        ],
        ...,
    ] = ()
    candidates: tuple[
        tuple[
            weakref.ReferenceType[LearningCandidateAuthority],
            weakref.ReferenceType[LearningCandidate],
            LearnedBehaviorArtifact,
            tuple[object, ...],
            tuple[object, ...],
        ],
        ...,
    ] = ()

    def prune_locked() -> None:
        nonlocal authorities, candidates
        authorities = tuple(
            record
            for record in authorities
            if record[0]() is not None and record[1]() is not None
        )
        candidates = tuple(
            record
            for record in candidates
            if record[0]() is not None and record[1]() is not None
        )

    def inspect_authority(
        authority: LearningCandidateAuthority,
    ) -> LearnedBehaviorArtifactStore:
        if type(authority) is not LearningCandidateAuthority:
            raise LearningCandidateError("candidate_authority_not_canonical")
        try:
            store_ref = object.__getattribute__(authority, "_store_ref")
        except AttributeError as exc:
            raise LearningCandidateError("candidate_authority_corrupt") from exc
        if type(store_ref) is not weakref.ReferenceType:
            raise LearningCandidateError("candidate_authority_corrupt")
        store = store_ref()
        if type(store) is not LearnedBehaviorArtifactStore:
            raise LearningCandidateError("candidate_authority_not_canonical")
        matches = tuple(
            record
            for record in authorities
            if record[0]() is store and record[1]() is authority
        )
        if len(matches) != 1:
            raise LearningCandidateError("candidate_authority_not_canonical")
        return store

    def issue_authority(
        store: LearnedBehaviorArtifactStore,
    ) -> LearningCandidateAuthority:
        nonlocal authorities
        if type(store) is not LearnedBehaviorArtifactStore:
            raise LearningCandidateError("candidate_store_required")
        with lock:
            prune_locked()
            matches = tuple(record[1]() for record in authorities if record[0]() is store)
            if len(matches) == 1 and matches[0] is not None:
                return matches[0]
            if matches:
                raise LearningCandidateError("candidate_authority_registry_corrupt")
            authority = object.__new__(LearningCandidateAuthority)
            object.__setattr__(authority, "_store_ref", weakref.ref(store))
            authorities = (
                *authorities,
                (weakref.ref(store), weakref.ref(authority)),
            )
            return authority

    def inspect_candidate(
        authority: LearningCandidateAuthority,
        candidate: LearningCandidate,
    ) -> LearningCandidate:
        with lock:
            prune_locked()
            store = inspect_authority(authority)
            current = _candidate_snapshot(candidate)
            matches = tuple(
                record
                for record in candidates
                if record[0]() is authority and record[1]() is candidate
            )
            if len(matches) != 1:
                raise LearningCandidateError("candidate_not_canonical")
            record = matches[0]
            try:
                store.inspect_candidate(record[2])
            except Exception as exc:
                raise LearningCandidateError("candidate_source_not_canonical") from exc
            if record[3] != artifact_snapshot(record[2]) or record[4] != current:
                raise LearningCandidateError("candidate_corrupt")
            return candidate

    def inspect_any(candidate: LearningCandidate) -> LearningCandidate:
        with lock:
            prune_locked()
            matching_authorities = tuple(
                record[0]()
                for record in candidates
                if record[1]() is candidate and record[0]() is not None
            )
            if len(matching_authorities) != 1:
                raise LearningCandidateError("candidate_not_canonical")
            return inspect_candidate(matching_authorities[0], candidate)

    def refresh(
        authority: LearningCandidateAuthority,
    ) -> tuple[LearningCandidate, ...]:
        nonlocal candidates
        with lock:
            prune_locked()
            store = inspect_authority(authority)
            result: list[LearningCandidate] = []
            for artifact in store.candidates():
                try:
                    store.inspect_candidate(artifact)
                    source_snapshot = artifact_snapshot(artifact)
                except Exception:
                    if type(artifact) is LearnedBehaviorArtifact:
                        raise LearningCandidateError("artifact_not_canonical")
                    continue
                matches = tuple(
                    record
                    for record in candidates
                    if record[0]() is authority and record[2] is artifact
                )
                if len(matches) == 1 and matches[0][1]() is not None:
                    candidate = matches[0][1]()
                    inspect_candidate(authority, candidate)
                    result.append(candidate)
                    continue
                if matches:
                    raise LearningCandidateError("candidate_registry_corrupt")
                if len(candidates) >= 2048:
                    raise LearningCandidateError("candidate_ledger_full")
                candidate = object.__new__(LearningCandidate)
                values = {
                    "candidate_id": artifact.artifact_id,
                    "source_kind": CandidateSourceKind.AGGREGATED_FEEDBACK_CLUSTER,
                    "source_schema": artifact.source_schema,
                    "privacy_class": CandidatePrivacyClass.AGGREGATED_ONLY,
                    "status": CandidateStatus.CANDIDATE,
                    "persona_key": artifact.persona_key,
                    "situation_id": artifact.situation_id,
                    "relationship_scope": artifact.relationship_scope,
                    "behavior_id": artifact.behavior_id,
                    "sample_count": artifact.sample_count,
                    "high_confidence_count": artifact.high_confidence_count,
                    "low_confidence_count": artifact.low_confidence_count,
                    "positive_weight": artifact.positive_weight,
                    "negative_weight": artifact.negative_weight,
                    "score": artifact.score,
                    "confidence": artifact.confidence,
                    "generated_at": artifact.generated_at,
                }
                for name, value in values.items():
                    object.__setattr__(candidate, name, value)
                snapshot = _candidate_snapshot(candidate)
                candidates = (
                    *candidates,
                    (
                        weakref.ref(authority),
                        weakref.ref(candidate),
                        artifact,
                        source_snapshot,
                        snapshot,
                    ),
                )
                result.append(candidate)
            return tuple(result)

    def trace(authority: LearningCandidateAuthority) -> dict[str, int | bool]:
        with lock:
            prune_locked()
            inspect_authority(authority)
            count = sum(record[0]() is authority for record in candidates)
            return {
                "candidate_count": count,
                "candidate_only": True,
                "bounded": count <= 2048,
            }

    return issue_authority, refresh, inspect_candidate, inspect_any, trace


(
    _issue_authority,
    _refresh_candidates,
    _inspect_candidate,
    _inspect_candidate_any,
    _authority_trace,
) = _build_candidate_vault()
del _build_candidate_vault


__all__ = [
    "CandidatePrivacyClass",
    "CandidateSourceKind",
    "CandidateStatus",
    "LearningCandidate",
    "LearningCandidateAuthority",
    "LearningCandidateError",
]
