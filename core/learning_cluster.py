from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .feedback import FeedbackConfidence, FeedbackEvidence, FeedbackSignal
from .observability import safe_exception_kind, structured_log


@dataclass(frozen=True, slots=True)
class LearningContext:
    persona_key: str
    situation_id: str
    relationship_scope: str
    behavior_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SituationBehaviorResultSample:
    context: LearningContext
    signal: FeedbackSignal
    confidence: FeedbackConfidence
    evidence_source: str
    weight: float
    observed_at: float


@dataclass(slots=True)
class BehaviorOutcomeCluster:
    persona_key: str
    situation_id: str
    relationship_scope: str
    behavior_id: str
    positive_weight: float = 0.0
    negative_weight: float = 0.0
    sample_count: int = 0
    high_confidence_count: int = 0
    low_confidence_count: int = 0
    last_observed_at: float = 0.0
    reviewer_fingerprints: tuple[str, ...] = ()

    @property
    def reviewer_count(self) -> int:
        return len(self.reviewer_fingerprints)

    @property
    def score(self) -> float:
        total = self.positive_weight + self.negative_weight
        if total <= 0:
            return 0.0
        return max(-1.0, min(1.0, (self.positive_weight - self.negative_weight) / total))


def _cluster_snapshot(cluster: BehaviorOutcomeCluster) -> tuple[object, ...]:
    if type(cluster) is not BehaviorOutcomeCluster:
        raise ValueError("learning_cluster_not_canonical")
    try:
        values = (
            cluster.persona_key,
            cluster.situation_id,
            cluster.relationship_scope,
            cluster.behavior_id,
            cluster.positive_weight,
            cluster.negative_weight,
            cluster.sample_count,
            cluster.high_confidence_count,
            cluster.low_confidence_count,
            cluster.last_observed_at,
            cluster.reviewer_fingerprints,
        )
    except (AttributeError, TypeError) as exc:
        raise ValueError("learning_cluster_corrupt") from exc
    if (
        any(type(value) is not str or not value for value in values[:4])
        or any(type(value) is not float or not math.isfinite(value) for value in values[4:6])
        or any(type(value) is not int for value in values[6:9])
        or type(values[9]) is not float
        or not math.isfinite(values[9])
        or type(values[10]) is not tuple
        or any(
            type(value) is not str
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in values[10]
        )
        or tuple(sorted(set(values[10]))) != values[10]
        or not 0 <= len(values[10]) <= 64
        or values[6] != len(values[10])
        or values[7] != values[6]
        or values[8] != 0
        or values[4] < 0
        or values[5] < 0
        or not math.isclose(values[4] + values[5], float(values[6]), abs_tol=1e-9)
        or values[9] < 0
    ):
        raise ValueError("learning_cluster_corrupt")
    return values


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("learning_cluster_duplicate_key")
        result[key] = value
    return result


def persona_learning_key(persona_name: str, voice_card: str = "") -> str:
    material = f"{str(persona_name or '').strip()}\0{str(voice_card or '').strip()}"
    digest = hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"persona-{digest}"


def make_learning_context(
    *,
    persona_key: str,
    situation_id: str,
    relationship_scope: str,
    behavior_ids: Sequence[str],
) -> LearningContext:
    return LearningContext(
        persona_key=str(persona_key or "").strip(),
        situation_id=str(situation_id or "").strip(),
        relationship_scope=str(relationship_scope or "").strip(),
        behavior_ids=tuple(
            dict.fromkeys(str(value) for value in behavior_ids if str(value))
        )[:8],
    )


class BehaviorOutcomeClusterStore:
    def __init__(
        self,
        path: Path,
        logger: Any,
        *,
        poison_guard,
        min_samples: int = 3,
        max_clusters: int = 2048,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        self.logger = logger
        from .learning_poison_guard import LearningPoisoningGuard

        if type(poison_guard) is not LearningPoisoningGuard:
            raise ValueError("learning_poison_guard_required")
        poison_guard.trace_metadata()
        self._poison_guard = poison_guard
        self.min_samples = max(2, int(min_samples))
        self.max_clusters = max(64, int(max_clusters))
        self.now_fn = now_fn
        self.clusters: dict[tuple[str, str, str, str], BehaviorOutcomeCluster] = {}
        self._cluster_snapshots: dict[
            tuple[str, str, str, str],
            tuple[BehaviorOutcomeCluster, tuple[object, ...]],
        ] = {}
        self._dirty = False
        self._load()

    @staticmethod
    def _valid_context(context: LearningContext) -> bool:
        return bool(
            context.persona_key
            and context.situation_id
            and context.relationship_scope
            and context.behavior_ids
        )

    def observe(self, sample: SituationBehaviorResultSample) -> None:
        del sample
        raise ValueError("learning_admission_required")

    def observe_admission(self, admission, poison_guard) -> None:
        from .learning_poison_guard import _claim_learning_admission

        context, reviewer_fingerprint, signal, observed_at = _claim_learning_admission(
            admission,
            poison_guard,
            self,
        )
        if not self._valid_context(context):
            raise ValueError("learning_context_invalid")
        for behavior_id in context.behavior_ids:
            key = (
                context.persona_key,
                context.situation_id,
                context.relationship_scope,
                behavior_id,
            )
            cluster = self.clusters.get(key)
            if cluster is None:
                if len(self.clusters) >= self.max_clusters:
                    oldest = min(
                        self.clusters,
                        key=lambda item: self.clusters[item].last_observed_at,
                    )
                    self.clusters.pop(oldest, None)
                cluster = BehaviorOutcomeCluster(
                    persona_key=key[0],
                    situation_id=key[1],
                    relationship_scope=key[2],
                    behavior_id=key[3],
                )
                self.clusters[key] = cluster
                self._cluster_snapshots[key] = (
                    cluster,
                    _cluster_snapshot(cluster),
                )
            else:
                cluster = self.inspect_cluster(cluster)
            if reviewer_fingerprint in cluster.reviewer_fingerprints:
                continue
            if len(cluster.reviewer_fingerprints) >= 64:
                continue
            if signal is FeedbackSignal.POSITIVE:
                cluster.positive_weight = min(64.0, cluster.positive_weight + 1.0)
            else:
                cluster.negative_weight = min(64.0, cluster.negative_weight + 1.0)
            cluster.reviewer_fingerprints = tuple(
                sorted((*cluster.reviewer_fingerprints, reviewer_fingerprint))
            )
            cluster.sample_count = len(cluster.reviewer_fingerprints)
            cluster.high_confidence_count = cluster.sample_count
            cluster.low_confidence_count = 0
            cluster.last_observed_at = max(
                cluster.last_observed_at,
                observed_at,
            )
            self._cluster_snapshots[key] = (
                cluster,
                _cluster_snapshot(cluster),
            )
            self._dirty = True

    def observe_feedback(
        self,
        *,
        context: LearningContext,
        evidence: FeedbackEvidence,
        observed_at: float,
    ) -> None:
        del context, evidence, observed_at
        raise ValueError("learning_admission_required")

    def inspect_cluster(
        self,
        cluster: BehaviorOutcomeCluster,
    ) -> BehaviorOutcomeCluster:
        snapshot = _cluster_snapshot(cluster)
        key = snapshot[:4]
        record = self._cluster_snapshots.get(key)
        if (
            record is None
            or self.clusters.get(key) is not cluster
            or record[0] is not cluster
        ):
            raise ValueError("learning_cluster_not_canonical")
        if record[1] != snapshot:
            raise ValueError("learning_cluster_corrupt")
        return cluster

    def eligible_clusters(
        self,
        *,
        persona_key: str = "",
    ) -> tuple[BehaviorOutcomeCluster, ...]:
        result = tuple(
            self.inspect_cluster(cluster)
            for cluster in self.clusters.values()
            if cluster.sample_count >= self.min_samples
            and cluster.reviewer_count >= self.min_samples
            and cluster.high_confidence_count == cluster.sample_count
            and cluster.low_confidence_count == 0
            and (not persona_key or cluster.persona_key == persona_key)
        )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.persona_key,
                    item.situation_id,
                    item.relationship_scope,
                    item.behavior_id,
                ),
            )
        )

    def flush(self) -> None:
        if not self._dirty:
            return
        rows = [
            asdict(self.inspect_cluster(cluster))
            for cluster in sorted(
                self.clusters.values(),
                key=lambda item: (
                    item.persona_key,
                    item.situation_id,
                    item.relationship_scope,
                    item.behavior_id,
                ),
            )
        ]
        from .learning_poison_guard import _learning_cluster_state_mac

        payload = {
            "version": 2,
            "min_samples": self.min_samples,
            "clusters": rows,
            "state_mac": _learning_cluster_state_mac(self._poison_guard, rows),
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
                "learning.cluster_save_failed",
                failure_kind=safe_exception_kind(exc),
            )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                return
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
            if (
                type(payload) is not dict
                or set(payload) != {"version", "min_samples", "clusters", "state_mac"}
                or type(payload["version"]) is not int
                or payload["version"] != 2
                or type(payload["min_samples"]) is not int
                or payload["min_samples"] != self.min_samples
                or type(payload["clusters"]) is not list
                or type(payload["state_mac"]) is not str
            ):
                return
            rows = payload["clusters"]
            from .learning_poison_guard import _learning_cluster_state_mac

            expected_mac = _learning_cluster_state_mac(self._poison_guard, rows)
            if not hmac.compare_digest(payload["state_mac"], expected_mac):
                return
            for value in rows[: self.max_clusters]:
                if type(value) is not dict or set(value) != {
                    "persona_key",
                    "situation_id",
                    "relationship_scope",
                    "behavior_id",
                    "positive_weight",
                    "negative_weight",
                    "sample_count",
                    "high_confidence_count",
                    "low_confidence_count",
                    "last_observed_at",
                    "reviewer_fingerprints",
                }:
                    continue
                if (
                    any(type(value[name]) is not str for name in (
                        "persona_key",
                        "situation_id",
                        "relationship_scope",
                        "behavior_id",
                    ))
                    or type(value["positive_weight"]) is not float
                    or type(value["negative_weight"]) is not float
                    or type(value["sample_count"]) is not int
                    or type(value["high_confidence_count"]) is not int
                    or type(value["low_confidence_count"]) is not int
                    or type(value["last_observed_at"]) is not float
                    or type(value["reviewer_fingerprints"]) is not list
                ):
                    continue
                cluster = BehaviorOutcomeCluster(
                    persona_key=value["persona_key"],
                    situation_id=value["situation_id"],
                    relationship_scope=value["relationship_scope"],
                    behavior_id=value["behavior_id"],
                    positive_weight=value["positive_weight"],
                    negative_weight=value["negative_weight"],
                    sample_count=value["sample_count"],
                    high_confidence_count=value["high_confidence_count"],
                    low_confidence_count=value["low_confidence_count"],
                    last_observed_at=value["last_observed_at"],
                    reviewer_fingerprints=tuple(value["reviewer_fingerprints"]),
                )
                try:
                    snapshot = _cluster_snapshot(cluster)
                except ValueError:
                    continue
                key = (
                    cluster.persona_key,
                    cluster.situation_id,
                    cluster.relationship_scope,
                    cluster.behavior_id,
                )
                if all(key):
                    self.clusters[key] = cluster
                    self._cluster_snapshots[key] = (cluster, snapshot)
        except Exception as exc:
            structured_log(
                self.logger,
                "warning",
                "learning.cluster_load_failed",
                failure_kind=safe_exception_kind(exc),
            )
