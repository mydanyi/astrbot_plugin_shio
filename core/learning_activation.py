from __future__ import annotations

import json
import math
import os
import re
import threading
import weakref
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .learning_artifact import LearnedBehaviorArtifact, validate_learned_artifact
from .observability import safe_exception_kind, structured_log


class ActivationState(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ENABLED = "enabled"


class ActivationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactActivation:
    artifact_id: str
    state: ActivationState
    weight_cap: float
    revision: int
    updated_at: float
    reason_code: str


_REASON_CODE_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


def _activation_snapshot(activation: ArtifactActivation) -> tuple[object, ...]:
    if type(activation) is not ArtifactActivation:
        raise ActivationError("activation_not_canonical")
    try:
        snapshot = (
            activation.artifact_id,
            activation.state,
            activation.weight_cap,
            activation.revision,
            activation.updated_at,
            activation.reason_code,
        )
    except (AttributeError, TypeError) as exc:
        raise ActivationError("activation_corrupt") from exc
    if (
        type(snapshot[0]) is not str
        or not snapshot[0].startswith("learned-")
        or type(snapshot[1]) is not ActivationState
        or type(snapshot[2]) is not float
        or not math.isfinite(snapshot[2])
        or not 0.0 <= snapshot[2] <= 0.15
        or type(snapshot[3]) is not int
        or snapshot[3] < 0
        or type(snapshot[4]) is not float
        or not math.isfinite(snapshot[4])
        or snapshot[4] <= 0
        or type(snapshot[5]) is not str
        or not _REASON_CODE_RE.fullmatch(snapshot[5])
    ):
        raise ActivationError("activation_corrupt")
    return snapshot


def _build_activation_vault():
    lock = threading.RLock()
    records: tuple[
        tuple[
            weakref.ReferenceType[LearningActivationStore],
            str,
            ArtifactActivation,
            tuple[object, ...],
        ],
        ...,
    ] = ()

    def prune_locked() -> None:
        nonlocal records
        records = tuple(record for record in records if record[0]() is not None)

    def inspect_locked(
        store: LearningActivationStore,
        artifact_id: str,
    ) -> ArtifactActivation | None:
        if type(store) is not LearningActivationStore or type(artifact_id) is not str:
            raise ActivationError("activation_corrupt")
        current = store.activations.get(artifact_id)
        matches = tuple(
            record
            for record in records
            if record[0]() is store and record[1] == artifact_id
        )
        if current is None:
            if matches:
                raise ActivationError("activation_registry_corrupt")
            return None
        if len(matches) != 1 or matches[0][2] is not current:
            raise ActivationError("activation_not_canonical")
        if matches[0][3] != _activation_snapshot(current):
            raise ActivationError("activation_corrupt")
        return current

    def inspect(
        store: LearningActivationStore,
        artifact_id: str,
    ) -> ArtifactActivation | None:
        with lock:
            prune_locked()
            return inspect_locked(store, artifact_id)

    def issue_default(
        store: LearningActivationStore,
        artifact_id: str,
        generated_at: float,
    ) -> ArtifactActivation:
        nonlocal records
        if (
            type(store) is not LearningActivationStore
            or type(artifact_id) is not str
            or not artifact_id.startswith("learned-")
            or type(generated_at) is not float
            or not math.isfinite(generated_at)
            or generated_at <= 0
        ):
            raise ActivationError("activation_default_invalid")
        with lock:
            prune_locked()
            if inspect_locked(store, artifact_id) is not None:
                raise ActivationError("activation_already_registered")
            if len(records) >= 2048:
                raise ActivationError("activation_ledger_full")
            activation = ArtifactActivation(
                artifact_id=artifact_id,
                state=ActivationState.DISABLED,
                weight_cap=0.15,
                revision=0,
                updated_at=generated_at,
                reason_code="candidate_default_disabled",
            )
            snapshot = _activation_snapshot(activation)
            store.activations[artifact_id] = activation
            records = (
                *records,
                (weakref.ref(store), artifact_id, activation, snapshot),
            )
            return activation

    def restore_shadow(
        store: LearningActivationStore,
        activation: ArtifactActivation,
    ) -> None:
        nonlocal records
        snapshot = _activation_snapshot(activation)
        if activation.state not in {ActivationState.DISABLED, ActivationState.SHADOW}:
            raise ActivationError("activation_restart_review_required")
        with lock:
            prune_locked()
            if inspect_locked(store, activation.artifact_id) is not None:
                raise ActivationError("activation_already_registered")
            if len(records) >= 2048:
                raise ActivationError("activation_ledger_full")
            store.activations[activation.artifact_id] = activation
            records = (
                *records,
                (
                    weakref.ref(store),
                    activation.artifact_id,
                    activation,
                    snapshot,
                ),
            )

    def replace_reviewed(
        store: LearningActivationStore,
        review_authority,
        candidate_authority,
        previous: ArtifactActivation,
        updated: ArtifactActivation,
    ) -> None:
        nonlocal records
        from .learning_review import _inspect_review_authority_for_store

        _inspect_review_authority_for_store(
            review_authority,
            candidate_authority,
            store,
        )
        updated_snapshot = _activation_snapshot(updated)
        with lock:
            prune_locked()
            current = inspect_locked(store, previous.artifact_id)
            if current is not previous or updated.artifact_id != previous.artifact_id:
                raise ActivationError("activation_not_canonical")
            matching_indexes = tuple(
                index
                for index, record in enumerate(records)
                if record[0]() is store and record[1] == previous.artifact_id
            )
            if len(matching_indexes) != 1:
                raise ActivationError("activation_registry_corrupt")
            index = matching_indexes[0]
            next_records = list(records)
            next_records[index] = (
                weakref.ref(store),
                updated.artifact_id,
                updated,
                updated_snapshot,
            )
            store.activations[updated.artifact_id] = updated
            records = tuple(next_records)

    def for_flush(
        store: LearningActivationStore,
    ) -> tuple[ArtifactActivation, ...]:
        with lock:
            prune_locked()
            if type(store) is not LearningActivationStore:
                raise ActivationError("activation_store_required")
            if len(store.activations) > 2048:
                raise ActivationError("activation_ledger_full")
            result = tuple(
                inspect_locked(store, artifact_id)
                for artifact_id in sorted(store.activations)
            )
            if any(activation is None for activation in result):
                raise ActivationError("activation_registry_corrupt")
            return result

    return inspect, issue_default, restore_shadow, replace_reviewed, for_flush


class LearningActivationStore:
    def __init__(self, path: Path, logger: Any) -> None:
        self.path = Path(path)
        self.logger = logger
        self.activations: dict[str, ArtifactActivation] = {}
        self._known_artifact_ids: set[str] = set()
        self._dirty = False
        self._load()

    def register_candidates(
        self,
        artifacts: Iterable[LearnedBehaviorArtifact],
    ) -> None:
        known: set[str] = set()
        for artifact in artifacts:
            validate_learned_artifact(artifact)
            known.add(artifact.artifact_id)
            if artifact.artifact_id not in self.activations:
                _issue_default_activation(
                    self,
                    artifact.artifact_id,
                    artifact.generated_at,
                )
                self._dirty = True
        self._known_artifact_ids = known

    def set_state(
        self,
        artifact_id: str,
        *,
        state: ActivationState,
        expected_revision: int,
        updated_at: float,
        reason_code: str,
        weight_cap: float | None = None,
    ) -> ArtifactActivation:
        del (
            artifact_id,
            state,
            expected_revision,
            updated_at,
            reason_code,
            weight_cap,
        )
        raise ActivationError("review_authority_required")

    def _inspect_activation(self, artifact_id: str) -> ArtifactActivation | None:
        return _inspect_activation_record(self, artifact_id)

    def _apply_candidate_review(
        self,
        review_authority,
        candidate,
        candidate_authority,
        *,
        state: ActivationState,
        expected_revision: int,
        updated_at: float,
        reason_code: str,
        weight_cap: float | None = None,
    ) -> ArtifactActivation:
        from .learning_candidate import LearningCandidate, LearningCandidateAuthority
        from .learning_review import _inspect_review_authority_for_store

        _inspect_review_authority_for_store(
            review_authority,
            candidate_authority,
            self,
        )
        if type(candidate) is not LearningCandidate:
            raise ActivationError("candidate_not_canonical")
        if type(candidate_authority) is not LearningCandidateAuthority:
            raise ActivationError("candidate_authority_not_canonical")
        candidate_authority.inspect_candidate(candidate)
        if type(state) is not ActivationState:
            raise ActivationError("activation state is invalid")
        if type(expected_revision) is not int:
            raise ActivationError("stale activation revision")
        key = candidate.candidate_id
        existing = self.activations.get(key)
        if existing is None:
            raise ActivationError("unknown learned artifact")
        existing = self._inspect_activation(key)
        assert existing is not None
        if state != ActivationState.DISABLED and key not in self._known_artifact_ids:
            raise ActivationError("orphaned learned artifact cannot be enabled")
        if existing.revision != expected_revision:
            raise ActivationError("stale activation revision")
        if type(reason_code) is not str:
            raise ActivationError("activation requires a structured reason code")
        reason = reason_code.strip().lower()
        if not _REASON_CODE_RE.fullmatch(reason):
            raise ActivationError("activation requires a structured reason code")
        if type(updated_at) not in {int, float}:
            raise ActivationError("activation update time is invalid")
        timestamp = float(updated_at)
        if not math.isfinite(timestamp) or timestamp <= 0:
            raise ActivationError("activation update time is invalid")
        if weight_cap is not None and type(weight_cap) not in {int, float}:
            raise ActivationError("learned weight cap must be between 0 and 0.15")
        cap = existing.weight_cap if weight_cap is None else float(weight_cap)
        if not math.isfinite(cap) or not 0.0 <= cap <= 0.15:
            raise ActivationError("learned weight cap must be between 0 and 0.15")
        updated = replace(
            existing,
            state=ActivationState(state),
            weight_cap=cap,
            revision=existing.revision + 1,
            updated_at=timestamp,
            reason_code=reason,
        )
        _replace_reviewed_activation(
            self,
            review_authority,
            candidate_authority,
            existing,
            updated,
        )
        self._dirty = True
        return updated

    def candidate_adjustments(self, candidate, candidate_authority) -> tuple[float, float]:
        from .learning_candidate import LearningCandidate, LearningCandidateAuthority

        if type(candidate) is not LearningCandidate:
            raise ActivationError("candidate_not_canonical")
        if type(candidate_authority) is not LearningCandidateAuthority:
            raise ActivationError("candidate_authority_not_canonical")
        candidate_authority.inspect_candidate(candidate)
        try:
            activation = self._inspect_activation(candidate.candidate_id)
        except ActivationError:
            return (0.0, 0.0)
        if activation is None or activation.state is ActivationState.DISABLED:
            return (0.0, 0.0)
        proposed = max(
            -activation.weight_cap,
            min(
                activation.weight_cap,
                candidate.score * candidate.confidence * activation.weight_cap,
            ),
        )
        effective = proposed if activation.state is ActivationState.ENABLED else 0.0
        return (proposed, effective)

    def proposed_adjustment(
        self,
        artifact: LearnedBehaviorArtifact,
        *,
        persona_key: str,
        situation_id: str,
        relationship_scope: str,
        behavior_id: str,
    ) -> float:
        try:
            activation = self._inspect_activation(artifact.artifact_id)
        except ActivationError:
            return 0.0
        if activation is None:
            return 0.0
        if (
            artifact.persona_key != persona_key
            or artifact.situation_id != situation_id
            or artifact.relationship_scope != relationship_scope
            or artifact.behavior_id != behavior_id
        ):
            return 0.0
        validate_learned_artifact(artifact)
        return max(
            -activation.weight_cap,
            min(
                activation.weight_cap,
                artifact.score * artifact.confidence * activation.weight_cap,
            ),
        )

    def effective_adjustment(
        self,
        artifact: LearnedBehaviorArtifact,
        *,
        persona_key: str,
        situation_id: str,
        relationship_scope: str,
        behavior_id: str,
    ) -> float:
        try:
            activation = self._inspect_activation(artifact.artifact_id)
        except ActivationError:
            return 0.0
        if activation is None or activation.state != ActivationState.ENABLED:
            return 0.0
        return self.proposed_adjustment(
            artifact,
            persona_key=persona_key,
            situation_id=situation_id,
            relationship_scope=relationship_scope,
            behavior_id=behavior_id,
        )

    def flush(self) -> None:
        if not self._dirty:
            return
        payload = {
            "version": 1,
            "default_state": "disabled",
            "activations": [
                {
                    **asdict(activation),
                    "state": activation.state.value,
                }
                for activation in _activation_records_for_flush(self)
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
                "learning.activation_save_failed",
                failure_kind=safe_exception_kind(exc),
            )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("default_state") != "disabled":
                return
            rows = payload.get("activations", [])
            if not isinstance(rows, list):
                return
            for value in rows[:2048]:
                if not isinstance(value, dict):
                    continue
                try:
                    if (
                        type(value.get("artifact_id")) is not str
                        or type(value.get("state")) is not str
                        or type(value.get("weight_cap")) is not float
                        or type(value.get("revision")) is not int
                        or type(value.get("updated_at")) is not float
                        or type(value.get("reason_code")) is not str
                    ):
                        continue
                    state = ActivationState(value["state"])
                    activation = ArtifactActivation(
                        artifact_id=value["artifact_id"].strip(),
                        state=state,
                        weight_cap=value["weight_cap"],
                        revision=value["revision"],
                        updated_at=value["updated_at"],
                        reason_code=value["reason_code"].strip(),
                    )
                except (TypeError, ValueError):
                    continue
                if (
                    not activation.artifact_id.startswith("learned-")
                    or not 0.0 <= activation.weight_cap <= 0.15
                    or not math.isfinite(activation.updated_at)
                    or not _REASON_CODE_RE.fullmatch(activation.reason_code)
                ):
                    continue
                if activation.state is ActivationState.ENABLED:
                    continue
                _restore_shadow_activation(self, activation)
        except Exception as exc:
            structured_log(
                self.logger,
                "warning",
                "learning.activation_load_failed",
                failure_kind=safe_exception_kind(exc),
            )


(
    _inspect_activation_record,
    _issue_default_activation,
    _restore_shadow_activation,
    _replace_reviewed_activation,
    _activation_records_for_flush,
) = _build_activation_vault()
del _build_activation_vault
