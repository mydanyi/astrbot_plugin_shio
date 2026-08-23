from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass, field
from enum import Enum

from .learning_activation import (
    ActivationError,
    ActivationState,
    ArtifactActivation,
    LearningActivationStore,
)
from .learning_candidate import LearningCandidate, LearningCandidateAuthority


class LearningReviewKind(str, Enum):
    SHADOW = "shadow"
    ENABLE = "enable"
    REVOKE = "revoke"


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    eq=False,
    repr=False,
    weakref_slot=True,
)
class LearningReviewAuthority:
    _candidate_authority_ref: weakref.ReferenceType[LearningCandidateAuthority] = field(
        repr=False,
    )
    _activation_store_ref: weakref.ReferenceType[LearningActivationStore] = field(
        repr=False,
    )

    @classmethod
    def issue_for_runtime(
        cls,
        *,
        candidate_authority: LearningCandidateAuthority,
        activation_store: LearningActivationStore,
    ) -> "LearningReviewAuthority":
        return _issue_review_authority(candidate_authority, activation_store)

    def review(
        self,
        candidate: LearningCandidate,
        *,
        kind: LearningReviewKind,
        expected_revision: int,
        reviewed_at: float,
        reason_code: str,
        weight_cap: float | None = None,
    ) -> ArtifactActivation:
        return _review_candidate(
            self,
            candidate,
            kind=kind,
            expected_revision=expected_revision,
            reviewed_at=reviewed_at,
            reason_code=reason_code,
            weight_cap=weight_cap,
        )

    def adjustments_for(
        self,
        candidate: LearningCandidate,
    ) -> tuple[float, float]:
        return _candidate_adjustments(self, candidate)

    def trace_metadata(self) -> dict[str, bool | int]:
        return _review_authority_trace(self)

    def __repr__(self) -> str:
        try:
            metadata = self.trace_metadata()
        except Exception:
            return "LearningReviewAuthority(canonical=False)"
        return (
            "LearningReviewAuthority(canonical=True, candidate_only=True, "
            f"bounded={metadata['bounded']!r})"
        )


def _build_review_vault():
    lock = threading.RLock()
    records: tuple[
        tuple[
            weakref.ReferenceType[LearningReviewAuthority],
            weakref.ReferenceType[LearningCandidateAuthority],
            weakref.ReferenceType[LearningActivationStore],
        ],
        ...,
    ] = ()

    def prune_locked() -> None:
        nonlocal records
        records = tuple(
            record
            for record in records
            if record[0]() is not None
            and record[1]() is not None
            and record[2]() is not None
        )

    def inspect_authority(
        authority: LearningReviewAuthority,
    ) -> tuple[LearningCandidateAuthority, LearningActivationStore]:
        if type(authority) is not LearningReviewAuthority:
            raise ActivationError("review_authority_not_canonical")
        try:
            candidate_authority_ref = object.__getattribute__(
                authority,
                "_candidate_authority_ref",
            )
            activation_store_ref = object.__getattribute__(
                authority,
                "_activation_store_ref",
            )
        except (AttributeError, TypeError) as exc:
            raise ActivationError("review_authority_corrupt") from exc
        if (
            type(candidate_authority_ref) is not weakref.ReferenceType
            or type(activation_store_ref) is not weakref.ReferenceType
        ):
            raise ActivationError("review_authority_corrupt")
        candidate_authority = candidate_authority_ref()
        activation_store = activation_store_ref()
        if (
            type(candidate_authority) is not LearningCandidateAuthority
            or type(activation_store) is not LearningActivationStore
        ):
            raise ActivationError("review_authority_not_canonical")
        matches = tuple(
            record
            for record in records
            if record[0]() is authority
            and record[1]() is candidate_authority
            and record[2]() is activation_store
        )
        if len(matches) != 1:
            raise ActivationError("review_authority_not_canonical")
        return candidate_authority, activation_store

    def issue_authority(
        candidate_authority: LearningCandidateAuthority,
        activation_store: LearningActivationStore,
    ) -> LearningReviewAuthority:
        nonlocal records
        if type(candidate_authority) is not LearningCandidateAuthority:
            raise ActivationError("candidate_authority_not_canonical")
        if type(activation_store) is not LearningActivationStore:
            raise ActivationError("activation_store_required")
        candidate_authority.trace_metadata()
        with lock:
            prune_locked()
            matches = tuple(
                record[0]()
                for record in records
                if record[1]() is candidate_authority
                and record[2]() is activation_store
            )
            if len(matches) == 1 and matches[0] is not None:
                return matches[0]
            if matches:
                raise ActivationError("review_authority_registry_corrupt")
            authority = object.__new__(LearningReviewAuthority)
            object.__setattr__(
                authority,
                "_candidate_authority_ref",
                weakref.ref(candidate_authority),
            )
            object.__setattr__(
                authority,
                "_activation_store_ref",
                weakref.ref(activation_store),
            )
            records = (
                *records,
                (
                    weakref.ref(authority),
                    weakref.ref(candidate_authority),
                    weakref.ref(activation_store),
                ),
            )
            return authority

    def review_candidate(
        authority: LearningReviewAuthority,
        candidate: LearningCandidate,
        *,
        kind: LearningReviewKind,
        expected_revision: int,
        reviewed_at: float,
        reason_code: str,
        weight_cap: float | None,
    ) -> ArtifactActivation:
        if type(kind) is not LearningReviewKind:
            raise ActivationError("review_kind_invalid")
        state_by_kind = {
            LearningReviewKind.SHADOW: ActivationState.SHADOW,
            LearningReviewKind.ENABLE: ActivationState.ENABLED,
            LearningReviewKind.REVOKE: ActivationState.DISABLED,
        }
        with lock:
            prune_locked()
            candidate_authority, activation_store = inspect_authority(authority)
            candidate_authority.inspect_candidate(candidate)
            return activation_store._apply_candidate_review(
                authority,
                candidate,
                candidate_authority,
                state=state_by_kind[kind],
                expected_revision=expected_revision,
                updated_at=reviewed_at,
                reason_code=reason_code,
                weight_cap=weight_cap,
            )

    def adjustments(
        authority: LearningReviewAuthority,
        candidate: LearningCandidate,
    ) -> tuple[float, float]:
        with lock:
            prune_locked()
            candidate_authority, activation_store = inspect_authority(authority)
            return activation_store.candidate_adjustments(
                candidate,
                candidate_authority,
            )

    def trace(authority: LearningReviewAuthority) -> dict[str, bool | int]:
        with lock:
            prune_locked()
            inspect_authority(authority)
            return {
                "candidate_only": True,
                "default_disabled": True,
                "max_weight_cap_milli": 150,
                "bounded": len(records) <= 2048,
            }

    def inspect_for_store(
        authority: LearningReviewAuthority,
        candidate_authority: LearningCandidateAuthority,
        activation_store: LearningActivationStore,
    ) -> None:
        with lock:
            prune_locked()
            exact_candidate_authority, exact_activation_store = inspect_authority(
                authority,
            )
            if (
                exact_candidate_authority is not candidate_authority
                or exact_activation_store is not activation_store
            ):
                raise ActivationError("review_authority_cross_runtime")

    return issue_authority, review_candidate, adjustments, trace, inspect_for_store


(
    _issue_review_authority,
    _review_candidate,
    _candidate_adjustments,
    _review_authority_trace,
    _inspect_review_authority_for_store,
) = _build_review_vault()
del _build_review_vault


__all__ = [
    "LearningReviewAuthority",
    "LearningReviewKind",
]
