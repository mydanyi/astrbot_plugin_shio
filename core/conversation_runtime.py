from __future__ import annotations

import json
import math
import os
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .feedback import (
    FeedbackConfidence,
    FeedbackEvidence,
    FeedbackEvidenceSource,
    FeedbackSignal,
    classify_feedback_evidence,
)
from .identity import build_scope_key, build_sender_key
from .learning_activation import LearningActivationStore
from .learning_artifact import LearnedBehaviorArtifactStore
from .learning_candidate import LearningCandidate, LearningCandidateAuthority
from .learning_cluster import BehaviorOutcomeClusterStore, LearningContext
from .learning_poison_guard import LearningPoisoningGuard, LearningPoisoningRejected
from .learning_review import LearningReviewAuthority
from .observability import safe_exception_kind, structured_log


_NEGATIVE_RE = re.compile(
    r"(?:答非所问|认错人|又把.+当成|别说了|没问你|不对|错误|很机械|像复读机)"
)
_POSITIVE_RE = re.compile(
    r"(?:哈哈|笑死|好可爱|可爱|不错|说得对|对对对|有道理|可以的|好耶|太懂了|真棒)"
)


@dataclass(slots=True)
class ConversationMessage:
    scope_key: str
    sequence: int
    sender_id: str
    text: str
    created_at: float
    message_id: str = ""
    reply_to_message_id: str = ""


@dataclass(slots=True)
class InteractionProfile:
    interaction_count: int = 0
    positive_feedback: int = 0
    negative_feedback: int = 0
    last_interaction_at: float = 0.0

    @property
    def affinity(self) -> float:
        evidence = self.positive_feedback + self.negative_feedback
        if evidence <= 0:
            return 0.0
        return max(
            -1.0,
            min(1.0, (self.positive_feedback - self.negative_feedback) / evidence),
        )


@dataclass(slots=True)
class ReplyObservation:
    scope_key: str
    target_identity_key: str
    internal_reply_id: str
    reply_text: str
    expression_ids: list[str]
    sent_platform_message_ids: set[str]
    learning_context: LearningContext | None
    trace_id: str
    created_at: float
    expires_at: float
    feedback_seen_from: set[str] = field(default_factory=set)


@dataclass(slots=True)
class GroupExpressionFeedback:
    score: float = 0.0
    sample_count: int = 0
    last_observed_at: float = 0.0


@dataclass(slots=True)
class ConversationState:
    messages: deque[ConversationMessage]
    sequence: int = 0
    last_replied_sequence: int = 0


class ConversationRuntime:
    """Identity-scoped feedback runtime with no reply or participation generator."""

    def __init__(
        self,
        data_dir: Path,
        logger: Any,
        *,
        max_messages_per_group: int = 80,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.logger = logger
        self.max_messages_per_group = max(20, int(max_messages_per_group))
        self.now_fn = now_fn or time.time
        self.groups: dict[str, ConversationState] = {}
        self.profiles: dict[str, InteractionProfile] = {}
        self.expression_feedback: dict[str, float] = {}
        self.group_expression_feedback: dict[str, dict[str, GroupExpressionFeedback]] = {}
        self.observations: dict[str, ReplyObservation] = {}
        self.feedback_evidence: dict[str, FeedbackEvidence] = {}
        self.learning_poison_guard = LearningPoisoningGuard.issue_for_root(
            data_dir,
            logger,
        )
        self.behavior_learning = BehaviorOutcomeClusterStore(
            data_dir / "behavior_outcomes.json",
            logger,
            poison_guard=self.learning_poison_guard,
            now_fn=self.now_fn,
        )
        self.learning_artifacts = LearnedBehaviorArtifactStore(
            data_dir / "learned_behavior_artifacts.json",
            logger,
            min_samples=self.behavior_learning.min_samples,
        )
        self.learning_artifacts.refresh(self.behavior_learning.eligible_clusters())
        self.learning_activation = LearningActivationStore(
            data_dir / "learned_behavior_activation.json",
            logger,
        )
        self.learning_activation.register_candidates(self.learning_artifacts.candidates())
        self.learning_candidate_authority = LearningCandidateAuthority.issue_for_store(
            self.learning_artifacts,
        )
        self.learning_review_authority = LearningReviewAuthority.issue_for_runtime(
            candidate_authority=self.learning_candidate_authority,
            activation_store=self.learning_activation,
        )
        self.state_path = data_dir / "social_state.json"
        self._dirty = False
        self._load_state()

    @staticmethod
    def group_scope(platform_id: str, bot_id: str, group_id: str) -> str:
        return build_scope_key(
            platform_id=platform_id,
            bot_id=bot_id,
            chat_type="group",
            group_id=group_id,
        )

    @staticmethod
    def identity_key(scope_key: str, sender_id: str) -> str:
        return build_sender_key(scope_key, sender_id)

    def ingest(
        self,
        *,
        platform_id: str,
        bot_id: str,
        group_id: str,
        unified_msg_origin: str,
        sender_id: str,
        sender_name: str,
        text: str,
        is_owner: bool,
        is_direct_wake: bool,
        message_id: str = "",
        reply_to_message_id: str = "",
        observe_feedback: bool = True,
        created_at: float | None = None,
    ) -> ConversationMessage | None:
        del unified_msg_origin, sender_name, is_owner, is_direct_wake
        clean_text = str(text or "").strip()
        if not group_id or not sender_id or not clean_text or sender_id == bot_id:
            return None
        current = float(created_at if created_at is not None else self.now_fn())
        scope_key = self.group_scope(platform_id, bot_id, group_id)
        state = self.groups.setdefault(
            scope_key,
            ConversationState(messages=deque(maxlen=self.max_messages_per_group)),
        )
        state.sequence += 1
        message = ConversationMessage(
            scope_key=scope_key,
            sequence=state.sequence,
            sender_id=sender_id,
            text=clean_text[:2000],
            created_at=current,
            message_id=str(message_id or "").strip(),
            reply_to_message_id=str(reply_to_message_id or "").strip(),
        )
        state.messages.append(message)
        if observe_feedback:
            self._observe_feedback(message)
        return message

    def record_bot_reply(
        self,
        *,
        scope_key: str,
        target_sender_id: str,
        reply_text: str,
        expression_ids: list[str] | None = None,
        target_sequence: int = 0,
        feedback_window_seconds: float = 600,
        internal_reply_id: str = "",
        sent_platform_message_ids: list[str] | tuple[str, ...] = (),
        learning_context: LearningContext | None = None,
        trace_id: str = "",
        now: float | None = None,
    ) -> None:
        current = float(now if now is not None else self.now_fn())
        state = self.groups.get(scope_key)
        if state is None:
            return
        state.last_replied_sequence = max(
            state.last_replied_sequence,
            int(target_sequence or state.sequence),
        )
        target_key = self.identity_key(scope_key, target_sender_id)
        reply_key = str(internal_reply_id or "").strip()
        existing = self.observations.get(scope_key)
        if reply_key and existing is not None and existing.internal_reply_id == reply_key:
            existing.reply_text = str(reply_text or "")[:500]
            existing.expression_ids = list(
                dict.fromkeys((*existing.expression_ids, *(expression_ids or [])))
            )[:3]
            existing.sent_platform_message_ids.update(
                str(value or "").strip()
                for value in sent_platform_message_ids
                if str(value or "").strip()
            )
            existing.expires_at = current + max(60, feedback_window_seconds)
            if existing.learning_context is None and learning_context is not None:
                existing.learning_context = learning_context
            if not existing.trace_id and trace_id:
                existing.trace_id = str(trace_id)
            self._dirty = True
            return

        profile = self.profiles.setdefault(target_key, InteractionProfile())
        profile.interaction_count += 1
        profile.last_interaction_at = current
        self.observations[scope_key] = ReplyObservation(
            scope_key=scope_key,
            target_identity_key=target_key,
            internal_reply_id=reply_key,
            reply_text=str(reply_text or "")[:500],
            expression_ids=list(dict.fromkeys(expression_ids or []))[:3],
            sent_platform_message_ids={
                str(value or "").strip()
                for value in sent_platform_message_ids
                if str(value or "").strip()
            },
            learning_context=learning_context,
            trace_id=str(trace_id or "").strip(),
            created_at=current,
            expires_at=current + max(60, feedback_window_seconds),
        )
        self._dirty = True

    def _observe_feedback(self, message: ConversationMessage) -> None:
        observation = self.observations.get(message.scope_key)
        if observation is None or message.created_at > observation.expires_at:
            self.observations.pop(message.scope_key, None)
            return
        reviewer_key = self.identity_key(message.scope_key, message.sender_id)
        if reviewer_key in observation.feedback_seen_from:
            return
        positive = bool(_POSITIVE_RE.search(message.text))
        negative = bool(_NEGATIVE_RE.search(message.text))
        if positive == negative:
            return
        signal = FeedbackSignal.POSITIVE if positive else FeedbackSignal.NEGATIVE
        evidence = classify_feedback_evidence(
            reviewer_key=reviewer_key,
            target_identity_key=observation.target_identity_key,
            reply_id=observation.internal_reply_id,
            signal=signal,
            reply_to_message_id=message.reply_to_message_id,
            sent_platform_message_ids=observation.sent_platform_message_ids,
            reply_trace_id=observation.trace_id,
        )
        self.feedback_evidence[message.scope_key] = evidence
        if (
            message.reply_to_message_id
            and evidence.source == FeedbackEvidenceSource.ADJACENT_GROUP_MESSAGE
        ):
            return
        observation.feedback_seen_from.add(reviewer_key)
        if (
            evidence.confidence != FeedbackConfidence.HIGH
            or not evidence.affects_target_profile
        ):
            self._record_group_feedback(
                scope_key=message.scope_key,
                expression_ids=observation.expression_ids,
                signal=signal,
                observed_at=message.created_at,
                evidence_weight=(
                    0.12 if evidence.confidence == FeedbackConfidence.HIGH else 0.08
                ),
            )
            self._observe_behavior_learning(
                observation,
                evidence,
                message.text,
                message.created_at,
            )
            return

        target_profile = self.profiles.setdefault(
            observation.target_identity_key,
            InteractionProfile(),
        )
        delta = 1.0 if positive else -1.0
        if positive:
            target_profile.positive_feedback += 1
        else:
            target_profile.negative_feedback += 1
        for expression_id in observation.expression_ids:
            previous = float(self.expression_feedback.get(expression_id, 0.0))
            self.expression_feedback[expression_id] = max(
                -2.0,
                min(2.0, previous * 0.85 + delta * 0.35),
            )
        self._observe_behavior_learning(
            observation,
            evidence,
            message.text,
            message.created_at,
        )
        self._dirty = True

    def _observe_behavior_learning(
        self,
        observation: ReplyObservation,
        evidence: FeedbackEvidence,
        feedback_text: str,
        observed_at: float,
    ) -> None:
        if observation.learning_context is not None:
            try:
                admission = self.learning_poison_guard.admit(
                    cluster_store=self.behavior_learning,
                    context=observation.learning_context,
                    evidence=evidence,
                    feedback_text=feedback_text,
                    observed_at=observed_at,
                )
                self.behavior_learning.observe_admission(
                    admission,
                    self.learning_poison_guard,
                )
            except LearningPoisoningRejected:
                return

    @staticmethod
    def _decayed_group_score(
        signal: GroupExpressionFeedback,
        now: float,
        *,
        half_life_seconds: float = 21600.0,
    ) -> float:
        elapsed = max(0.0, float(now) - float(signal.last_observed_at))
        half_life = max(60.0, float(half_life_seconds))
        return float(signal.score) * math.pow(0.5, elapsed / half_life)

    def _record_group_feedback(
        self,
        *,
        scope_key: str,
        expression_ids: list[str] | tuple[str, ...],
        signal: FeedbackSignal,
        observed_at: float,
        evidence_weight: float,
    ) -> None:
        expression_keys = tuple(
            dict.fromkeys(str(value) for value in expression_ids if str(value))
        )[:3]
        if not scope_key or not expression_keys:
            return
        scoped = self.group_expression_feedback.setdefault(scope_key, {})
        direction = 1.0 if signal == FeedbackSignal.POSITIVE else -1.0
        for expression_id in expression_keys:
            existing = scoped.setdefault(expression_id, GroupExpressionFeedback())
            existing.score = max(
                -0.5,
                min(
                    0.5,
                    self._decayed_group_score(existing, observed_at)
                    + direction * max(0.01, evidence_weight),
                ),
            )
            existing.sample_count = min(1000, existing.sample_count + 1)
            existing.last_observed_at = float(observed_at)
        self._dirty = True

    def expression_feedback_scores(
        self,
        *,
        scope_key: str,
        persona_key: str,
        situation_id: str,
        relationship_scope: str,
        now: float | None = None,
    ) -> dict[str, float]:
        del scope_key, now
        scores: dict[str, float] = {}
        try:
            candidates = self.learning_candidate_authority.refresh_candidates()
        except Exception:
            candidates = ()
        for candidate in candidates:
            if (
                candidate.persona_key != persona_key
                or candidate.situation_id != situation_id
                or candidate.relationship_scope != relationship_scope
            ):
                continue
            _proposed, adjustment = self.learning_review_authority.adjustments_for(
                candidate,
            )
            if adjustment:
                scores[candidate.behavior_id] = max(
                    -2.0,
                    min(2.0, scores.get(candidate.behavior_id, 0.0) + adjustment),
                )
        return scores

    def learning_candidates(self) -> tuple[LearningCandidate, ...]:
        """Return exact candidate-only projections; never enable or alter Persona."""

        return self.learning_candidate_authority.refresh_candidates()

    def flush(self) -> None:
        self.behavior_learning.flush()
        self.learning_artifacts.refresh(self.behavior_learning.eligible_clusters())
        self.learning_artifacts.flush()
        self.learning_candidate_authority.refresh_candidates()
        self.learning_activation.register_candidates(self.learning_artifacts.candidates())
        self.learning_activation.flush()
        if not self._dirty:
            return
        current = self.now_fn()
        payload = {
            "version": 3,
            "profiles": {
                key: asdict(value)
                for key, value in self.profiles.items()
                if value.interaction_count > 0
                or value.positive_feedback > 0
                or value.negative_feedback > 0
            },
            "expression_feedback": {
                key: round(float(value), 4)
                for key, value in self.expression_feedback.items()
                if math.isfinite(float(value)) and abs(float(value)) >= 0.01
            },
            "group_expression_feedback": {
                scope_key: {
                    expression_id: {
                        "score": round(self._decayed_group_score(signal, current), 4),
                        "sample_count": signal.sample_count,
                        "last_observed_at": signal.last_observed_at,
                    }
                    for expression_id, signal in expressions.items()
                    if abs(self._decayed_group_score(signal, current)) >= 0.01
                }
                for scope_key, expressions in self.group_expression_feedback.items()
                if expressions
            },
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
            self._dirty = False
        except Exception as exc:
            structured_log(
                self.logger,
                "warning",
                "learning.state_save_failed",
                failure_kind=safe_exception_kind(exc),
            )

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            profiles = payload.get("profiles", {}) if isinstance(payload, dict) else {}
            if isinstance(profiles, dict):
                for key, value in profiles.items():
                    if not isinstance(value, dict):
                        continue
                    self.profiles[str(key)] = InteractionProfile(
                        interaction_count=max(0, int(value.get("interaction_count", 0))),
                        positive_feedback=max(0, int(value.get("positive_feedback", 0))),
                        negative_feedback=max(0, int(value.get("negative_feedback", 0))),
                        last_interaction_at=float(value.get("last_interaction_at", 0.0)),
                    )
            feedback = payload.get("expression_feedback", {}) if isinstance(payload, dict) else {}
            if isinstance(feedback, dict):
                for key, value in feedback.items():
                    score = float(value)
                    if math.isfinite(score):
                        self.expression_feedback[str(key)] = max(-2.0, min(2.0, score))
            group_feedback = (
                payload.get("group_expression_feedback", {})
                if isinstance(payload, dict)
                else {}
            )
            if isinstance(group_feedback, dict):
                for scope_key, expressions in list(group_feedback.items())[:128]:
                    if not isinstance(expressions, dict):
                        continue
                    scoped: dict[str, GroupExpressionFeedback] = {}
                    for expression_id, value in list(expressions.items())[:64]:
                        if not isinstance(value, dict):
                            continue
                        score = float(value.get("score", 0.0))
                        if not math.isfinite(score) or abs(score) < 0.01:
                            continue
                        scoped[str(expression_id)] = GroupExpressionFeedback(
                            score=max(-0.5, min(0.5, score)),
                            sample_count=max(
                                0,
                                min(1000, int(value.get("sample_count", 0))),
                            ),
                            last_observed_at=max(
                                0.0,
                                float(value.get("last_observed_at", 0.0)),
                            ),
                        )
                    if scoped:
                        self.group_expression_feedback[str(scope_key)] = scoped
        except Exception as exc:
            structured_log(
                self.logger,
                "warning",
                "learning.state_load_failed",
                failure_kind=safe_exception_kind(exc),
            )
