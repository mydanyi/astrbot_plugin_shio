from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .feedback import (
    FeedbackConfidence,
    FeedbackEvidence,
    FeedbackEvidenceSource,
    FeedbackSignal,
    inspect_feedback_evidence,
)
from .learning_cluster import LearningContext


class LearningPoisoningRejected(ValueError):
    pass


_SECRET_FILENAME = ".learning_install_secret"
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PERSONA_RE = re.compile(r"^persona-[0-9a-f]{16}$")
_STRIP_FEEDBACK_RE = re.compile(r"[\s!！。,.，~～…]+")
_ALLOWED_POSITIVE = frozenset(
    {
        "哈哈",
        "笑死",
        "好可爱",
        "可爱",
        "不错",
        "说得对",
        "对对对",
        "有道理",
        "可以的",
        "好耶",
        "太懂了",
        "真棒",
    }
)
_ALLOWED_NEGATIVE = frozenset(
    {
        "答非所问",
        "认错人",
        "别说了",
        "没问你",
        "不对",
        "错误",
        "很机械",
        "像复读机",
    }
)


def _context_snapshot(context: LearningContext) -> tuple[object, ...]:
    if type(context) is not LearningContext:
        raise LearningPoisoningRejected("learning_context_not_canonical")
    try:
        values = (
            context.persona_key,
            context.situation_id,
            context.relationship_scope,
            context.behavior_ids,
        )
    except (AttributeError, TypeError) as exc:
        raise LearningPoisoningRejected("learning_context_corrupt") from exc
    if (
        type(values[0]) is not str
        or not _PERSONA_RE.fullmatch(values[0])
        or type(values[1]) is not str
        or not _TOKEN_RE.fullmatch(values[1])
        or type(values[2]) is not str
        or not _TOKEN_RE.fullmatch(values[2])
        or type(values[3]) is not tuple
        or not 1 <= len(values[3]) <= 8
        or any(type(value) is not str or not _TOKEN_RE.fullmatch(value) for value in values[3])
        or len(set(values[3])) != len(values[3])
    ):
        raise LearningPoisoningRejected("learning_context_unsafe")
    return values


def _safe_feedback_text(text: object, signal: FeedbackSignal) -> None:
    if (
        type(text) is not str
        or not text
        or text != text.strip()
        or len(text) > 48
        or any(ord(character) < 32 for character in text)
    ):
        raise LearningPoisoningRejected("learning_feedback_text_unsafe")
    normalized = _STRIP_FEEDBACK_RE.sub("", text)
    allowed = _ALLOWED_POSITIVE if signal is FeedbackSignal.POSITIVE else _ALLOWED_NEGATIVE
    if normalized not in allowed:
        raise LearningPoisoningRejected("learning_feedback_text_unsafe")


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    eq=False,
    repr=False,
    weakref_slot=True,
)
class LearningFeedbackAdmission:
    context: LearningContext = field(repr=False)
    signal: FeedbackSignal
    observed_at: float = field(repr=False)
    _reviewer_fingerprint: str = field(repr=False)

    def trace_metadata(self) -> dict[str, str | bool]:
        try:
            _inspect_admission_any(self)
        except Exception:
            return {"learning_admission_canonical": False}
        return {
            "learning_admission_canonical": True,
            "signal": self.signal.value,
            "target_high_confidence_only": True,
            "feedback_text_visible": False,
            "reviewer_visible": False,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        if not metadata.get("learning_admission_canonical", False):
            return "LearningFeedbackAdmission(canonical=False)"
        return (
            "LearningFeedbackAdmission(canonical=True, "
            f"signal={metadata['signal']!r}, reviewer_visible=False, "
            "feedback_text_visible=False)"
        )


def _admission_snapshot(admission: LearningFeedbackAdmission) -> tuple[object, ...]:
    if type(admission) is not LearningFeedbackAdmission:
        raise LearningPoisoningRejected("learning_admission_not_canonical")
    try:
        values = (
            admission.context,
            admission.signal,
            admission.observed_at,
            admission._reviewer_fingerprint,
        )
    except (AttributeError, TypeError) as exc:
        raise LearningPoisoningRejected("learning_admission_corrupt") from exc
    if (
        type(values[0]) is not LearningContext
        or type(values[1]) is not FeedbackSignal
        or type(values[2]) is not float
        or not math.isfinite(values[2])
        or values[2] <= 0
        or type(values[3]) is not str
        or len(values[3]) != 64
        or any(character not in "0123456789abcdef" for character in values[3])
    ):
        raise LearningPoisoningRejected("learning_admission_corrupt")
    return values


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    eq=False,
    repr=False,
    weakref_slot=True,
)
class LearningPoisoningGuard:
    _root: Path = field(repr=False)

    @classmethod
    def issue_for_root(
        cls,
        root: Path,
        logger: Any,
    ) -> "LearningPoisoningGuard":
        return _issue_guard(root, logger)

    def admit(
        self,
        *,
        cluster_store,
        context: LearningContext,
        evidence: FeedbackEvidence,
        feedback_text: str,
        observed_at: float,
    ) -> LearningFeedbackAdmission:
        return _issue_admission(
            self,
            cluster_store,
            context,
            evidence,
            feedback_text,
            observed_at,
        )

    def trace_metadata(self) -> dict[str, int | bool]:
        return _guard_trace(self)

    def __repr__(self) -> str:
        try:
            metadata = self.trace_metadata()
        except Exception:
            return "LearningPoisoningGuard(canonical=False)"
        return (
            "LearningPoisoningGuard(canonical=True, "
            f"admission_count={metadata['admission_count']!r}, "
            "secret_visible=False, reviewer_visible=False)"
        )


def _load_or_create_secret(root: Path) -> bytes:
    if type(root) is not type(Path()):
        raise LearningPoisoningRejected("learning_guard_root_invalid")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_path = root / _SECRET_FILENAME
    try:
        secret = secret_path.read_bytes()
    except FileNotFoundError:
        secret = secrets.token_bytes(32)
        descriptor = os.open(
            secret_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(secret)
            stream.flush()
            os.fsync(stream.fileno())
    if type(secret) is not bytes or len(secret) != 32:
        raise LearningPoisoningRejected("learning_guard_secret_invalid")
    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        pass
    return secret


def _build_learning_guard_vault():
    lock = threading.RLock()
    guards: tuple[
        tuple[weakref.ReferenceType[LearningPoisoningGuard], Path, bytes],
        ...,
    ] = ()
    admissions: tuple[
        tuple[
            weakref.ReferenceType[LearningPoisoningGuard],
            weakref.ReferenceType[LearningFeedbackAdmission],
            weakref.ReferenceType[object],
            LearningContext,
            tuple[object, ...],
            FeedbackEvidence,
            str,
            tuple[object, ...],
            bool,
        ],
        ...,
    ] = ()

    def prune_locked() -> None:
        nonlocal guards, admissions
        guards = tuple(record for record in guards if record[0]() is not None)
        admissions = tuple(
            record
            for record in admissions
            if record[0]() is not None
            and record[1]() is not None
            and record[2]() is not None
        )

    def inspect_guard(guard: LearningPoisoningGuard) -> tuple[Path, bytes]:
        if type(guard) is not LearningPoisoningGuard:
            raise LearningPoisoningRejected("learning_guard_not_canonical")
        try:
            root = object.__getattribute__(guard, "_root")
        except (AttributeError, TypeError) as exc:
            raise LearningPoisoningRejected("learning_guard_corrupt") from exc
        matches = tuple(record for record in guards if record[0]() is guard)
        if len(matches) != 1 or matches[0][1] != root:
            raise LearningPoisoningRejected("learning_guard_not_canonical")
        return matches[0][1], matches[0][2]

    def issue_guard(root: Path, _logger: Any) -> LearningPoisoningGuard:
        nonlocal guards
        if type(root) is not type(Path()):
            raise LearningPoisoningRejected("learning_guard_root_invalid")
        resolved = root.resolve()
        with lock:
            prune_locked()
            matches = tuple(record[0]() for record in guards if record[1] == resolved)
            if len(matches) == 1 and matches[0] is not None:
                return matches[0]
            if matches:
                raise LearningPoisoningRejected("learning_guard_registry_corrupt")
            secret = _load_or_create_secret(resolved)
            guard = object.__new__(LearningPoisoningGuard)
            object.__setattr__(guard, "_root", resolved)
            guards = (*guards, (weakref.ref(guard), resolved, secret))
            return guard

    def issue_admission(
        guard: LearningPoisoningGuard,
        cluster_store,
        context: LearningContext,
        evidence: FeedbackEvidence,
        feedback_text: str,
        observed_at: float,
    ) -> LearningFeedbackAdmission:
        nonlocal admissions
        from .learning_cluster import BehaviorOutcomeClusterStore

        if type(cluster_store) is not BehaviorOutcomeClusterStore:
            raise LearningPoisoningRejected("learning_cluster_store_required")
        with lock:
            prune_locked()
            _root, secret = inspect_guard(guard)
            context_snapshot = _context_snapshot(context)
            inspect_feedback_evidence(evidence)
            if (
                evidence.confidence is not FeedbackConfidence.HIGH
                or evidence.is_target_user is not True
                or evidence.affects_target_profile is not True
                or evidence.source
                not in {
                    FeedbackEvidenceSource.TARGET_FOLLOWUP,
                    FeedbackEvidenceSource.EXPLICIT_REPLY_REFERENCE,
                    FeedbackEvidenceSource.PLATFORM_REACTION,
                }
            ):
                raise LearningPoisoningRejected(
                    "learning_feedback_not_target_high_confidence"
                )
            _safe_feedback_text(feedback_text, evidence.signal)
            if (
                type(observed_at) is not float
                or not math.isfinite(observed_at)
                or observed_at <= 0
            ):
                raise LearningPoisoningRejected("learning_feedback_time_invalid")
            if len(admissions) >= 8192:
                raise LearningPoisoningRejected("learning_admission_ledger_full")
            reviewer_fingerprint = hmac.new(
                secret,
                b"learning-reviewer-v1\x00" + evidence.reviewer_key.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            admission = object.__new__(LearningFeedbackAdmission)
            values = {
                "context": context,
                "signal": evidence.signal,
                "observed_at": observed_at,
                "_reviewer_fingerprint": reviewer_fingerprint,
            }
            for name, value in values.items():
                object.__setattr__(admission, name, value)
            snapshot = _admission_snapshot(admission)
            admissions = (
                *admissions,
                (
                    weakref.ref(guard),
                    weakref.ref(admission),
                    weakref.ref(cluster_store),
                    context,
                    context_snapshot,
                    evidence,
                    reviewer_fingerprint,
                    snapshot,
                    False,
                ),
            )
            return admission

    def inspect_any(admission: LearningFeedbackAdmission) -> None:
        with lock:
            prune_locked()
            snapshot = _admission_snapshot(admission)
            matches = tuple(record for record in admissions if record[1]() is admission)
            if len(matches) != 1 or matches[0][7] != snapshot:
                raise LearningPoisoningRejected("learning_admission_not_canonical")
            inspect_feedback_evidence(matches[0][5])
            if matches[0][4] != _context_snapshot(matches[0][3]):
                raise LearningPoisoningRejected("learning_admission_corrupt")

    def claim_for_store(
        admission: LearningFeedbackAdmission,
        guard: LearningPoisoningGuard,
        cluster_store,
    ) -> tuple[LearningContext, str, FeedbackSignal, float]:
        nonlocal admissions
        with lock:
            prune_locked()
            inspect_guard(guard)
            snapshot = _admission_snapshot(admission)
            matches = tuple(
                (index, record)
                for index, record in enumerate(admissions)
                if record[1]() is admission
            )
            if len(matches) != 1 or matches[0][1][7] != snapshot:
                raise LearningPoisoningRejected("learning_admission_not_canonical")
            index, record = matches[0]
            if record[0]() is not guard or record[2]() is not cluster_store:
                raise LearningPoisoningRejected("learning_admission_cross_guard")
            if record[8]:
                raise LearningPoisoningRejected("learning_admission_replayed")
            inspect_feedback_evidence(record[5])
            if record[4] != _context_snapshot(record[3]):
                raise LearningPoisoningRejected("learning_admission_corrupt")
            next_records = list(admissions)
            next_records[index] = (*record[:8], True)
            admissions = tuple(next_records)
            return (
                admission.context,
                admission._reviewer_fingerprint,
                admission.signal,
                admission.observed_at,
            )

    def trace(guard: LearningPoisoningGuard) -> dict[str, int | bool]:
        with lock:
            prune_locked()
            inspect_guard(guard)
            count = sum(record[0]() is guard for record in admissions)
            return {
                "admission_count": count,
                "bounded": count <= 8192,
                "target_high_confidence_only": True,
                "feedback_text_visible": False,
                "reviewer_visible": False,
                "secret_visible": False,
            }

    def state_mac(guard: LearningPoisoningGuard, rows: list[dict[str, object]]) -> str:
        with lock:
            prune_locked()
            _root, secret = inspect_guard(guard)
            encoded = json.dumps(
                rows,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            return hmac.new(
                secret,
                b"learning-clusters-v2\x00" + encoded,
                hashlib.sha256,
            ).hexdigest()

    return issue_guard, issue_admission, inspect_any, claim_for_store, trace, state_mac


(
    _issue_guard,
    _issue_admission,
    _inspect_admission_any,
    _claim_learning_admission,
    _guard_trace,
    _learning_cluster_state_mac,
) = _build_learning_guard_vault()
del _build_learning_guard_vault


__all__ = [
    "LearningFeedbackAdmission",
    "LearningPoisoningGuard",
    "LearningPoisoningRejected",
]
