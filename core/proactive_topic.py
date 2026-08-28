from __future__ import annotations

import hashlib
import math
import threading
import weakref
from dataclasses import dataclass, field
from enum import Enum

from .contracts import ContractViolation
from .group_scene import (
    GroupSceneBook,
    GroupSceneSnapshot,
    PublicTopic,
    SceneEntrySource,
)
from .conversation_ledger import identity_metadata_integrity
from .model_input_contract import (
    CanonicalModelMessage,
    canonical_model_messages_digest,
    project_group_scene_model_messages,
)
from .persona import (
    ParticipationInterest,
    PersonaPackage,
    validate_persona_package,
)
from .proactive_policy import (
    ProactivePolicyDecision,
    ProactivePolicyDecisionKind,
    ProactivePolicyState,
)
from .proactive_trigger import ProactiveGroupTarget


_PLAN_SEAL = object()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _persona_topic_snapshot(package: PersonaPackage) -> tuple[object, ...]:
    if type(package) is not PersonaPackage:
        raise ContractViolation("proactive_topic_persona_required")
    try:
        package_id = package.package_id
        version = package.version
        display_name = package.display_name
        interests = package.participation_interests
    except AttributeError as exc:
        raise ContractViolation("proactive_topic_persona_corrupt") from exc
    if (
        type(package_id) is not str
        or type(version) is not str
        or type(display_name) is not str
        or type(interests) is not tuple
    ):
        raise ContractViolation("proactive_topic_persona_corrupt")
    snapshots: list[tuple[object, ...]] = []
    for interest in interests:
        if type(interest) is not ParticipationInterest:
            raise ContractViolation("proactive_topic_persona_corrupt")
        try:
            values = (interest.id, interest.keywords, interest.weight)
        except AttributeError as exc:
            raise ContractViolation("proactive_topic_persona_corrupt") from exc
        if (
            type(values[0]) is not str
            or type(values[1]) is not tuple
            or not values[1]
            or any(type(keyword) is not str or not keyword for keyword in values[1])
            or type(values[2]) is not float
            or not math.isfinite(values[2])
        ):
            raise ContractViolation("proactive_topic_persona_corrupt")
        snapshots.append(values)
    return (package_id, version, display_name, tuple(snapshots))


class ProactiveTopicSource(str, Enum):
    GROUP_PUBLIC_TOPIC = "group_public_topic"
    GROUP_RECENT_TOPIC = "group_recent_topic"
    PERSONA_INTEREST = "persona_interest"


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ProactiveTopicPlan:
    policy_decision: ProactivePolicyDecision = field(repr=False)
    target: ProactiveGroupTarget = field(repr=False)
    source: ProactiveTopicSource
    topic_text: str = field(repr=False)
    topic_digest: str = field(repr=False)
    recent_public_context: tuple[CanonicalModelMessage, ...] = field(repr=False)
    public_context_digest: str = field(repr=False)
    interest_id: str
    scene_revision: int
    model_authorized: bool
    send_authorized: bool
    _authority_ref: weakref.ReferenceType[ProactiveTopicAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def __new__(cls):
        raise TypeError("ProactiveTopicPlan is issuer-owned")

    def trace_metadata(self) -> dict[str, str | int | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = authority_ref() if type(authority_ref) is weakref.ReferenceType else None
        try:
            record = authority._inspect(self)  # type: ignore[union-attr]
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "proactive_topic_canonical": False,
                "proactive_topic_source": "invalid",
                "proactive_topic_scene_revision": 0,
                "proactive_topic_bound": False,
                "proactive_model_authorized": False,
                "proactive_send_authorized": False,
            }
        return {
            "schema_version": 1,
            "proactive_topic_canonical": True,
            "proactive_topic_source": record.source.value,
            "proactive_topic_scene_revision": record.scene_revision,
            "proactive_topic_context_message_count": len(
                record.recent_public_context
            ),
            "proactive_topic_bound": True,
            "proactive_model_authorized": False,
            "proactive_send_authorized": False,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ProactiveTopicPlan("
            f"canonical={metadata['proactive_topic_canonical']!r}, "
            f"source={metadata['proactive_topic_source']!r}, "
            f"scene_revision={metadata['proactive_topic_scene_revision']}, "
            "model_authorized=False, send_authorized=False)"
        )


@dataclass(frozen=True, slots=True)
class _PlanRecord:
    policy_decision: ProactivePolicyDecision
    target: ProactiveGroupTarget
    scene: GroupSceneSnapshot
    scene_snapshot: tuple[object, ...]
    persona: PersonaPackage
    persona_snapshot: tuple[object, ...]
    source: ProactiveTopicSource
    public_topic: PublicTopic | None
    interest: ParticipationInterest | None
    topic_text: str
    topic_digest: str
    recent_public_context: tuple[CanonicalModelMessage, ...]
    public_context_digest: str
    interest_id: str
    scene_revision: int
    snapshot: tuple[object, ...]


def _scene_topic_snapshot(scene: GroupSceneSnapshot) -> tuple[object, ...]:
    if type(scene) is not GroupSceneSnapshot:
        raise ContractViolation("proactive_topic_scene_invalid")
    try:
        scope_key = scene.scope_key
        revision = scene.conversation_revision
        public_topics = scene.public_topics
    except AttributeError as exc:
        raise ContractViolation("proactive_topic_scene_corrupt") from exc
    if (
        type(scope_key) is not str
        or not scope_key
        or type(revision) is not int
        or revision < 1
        or type(public_topics) is not tuple
    ):
        raise ContractViolation("proactive_topic_scene_corrupt")
    topics: list[tuple[object, ...]] = []
    for topic in public_topics:
        if type(topic) is not PublicTopic:
            raise ContractViolation("proactive_topic_scene_corrupt")
        try:
            values = (
                topic.source,
                topic.conversation_revision,
                topic.author_sender_key,
                topic.target_sender_key,
                topic.message_id,
                topic.reply_to_message_id,
                topic.content,
                topic.content_digest,
                identity_metadata_integrity(topic.identity_metadata),
            )
        except AttributeError as exc:
            raise ContractViolation("proactive_topic_scene_corrupt") from exc
        if (
            type(values[1]) is not int
            or values[1] < 1
            or any(type(value) is not str for value in values[2:8])
            or not values[6]
            or _digest_text(values[6]) != values[7]
            or type(values[8]) is not tuple
        ):
            raise ContractViolation("proactive_topic_scene_corrupt")
        topics.append(values)
    return (scope_key, revision, tuple(topics))


def _plan_snapshot(plan: ProactiveTopicPlan) -> tuple[object, ...]:
    if type(plan) is not ProactiveTopicPlan:
        raise ContractViolation("proactive_topic_plan_not_canonical")
    try:
        values = (
            plan.policy_decision,
            plan.target,
            plan.source,
            plan.topic_text,
            plan.topic_digest,
            plan.recent_public_context,
            plan.public_context_digest,
            plan.interest_id,
            plan.scene_revision,
            plan.model_authorized,
            plan.send_authorized,
            plan._authority_ref,
            plan._seal,
        )
    except AttributeError as exc:
        raise ContractViolation("proactive_topic_plan_corrupt") from exc
    if (
        type(values[0]) is not ProactivePolicyDecision
        or type(values[1]) is not ProactiveGroupTarget
        or type(values[2]) is not ProactiveTopicSource
        or type(values[3]) is not str
        or not values[3]
        or type(values[4]) is not str
        or _digest_text(values[3]) != values[4]
        or type(values[5]) is not tuple
        or len(values[5]) < 2
        or any(type(value) is not CanonicalModelMessage for value in values[5])
        or type(values[6]) is not str
        or canonical_model_messages_digest(values[5]) != values[6]
        or type(values[7]) is not str
        or not values[7]
        or type(values[8]) is not int
        or values[8] < 1
        or type(values[9]) is not bool
        or values[9]
        or type(values[10]) is not bool
        or values[10]
        or type(values[11]) is not weakref.ReferenceType
        or values[12] is not _PLAN_SEAL
    ):
        raise ContractViolation("proactive_topic_plan_corrupt")
    return values


class ProactiveTopicAuthority:
    __slots__ = (
        "_consumed",
        "_group_scenes",
        "_lock",
        "_persona_snapshots",
        "_personas",
        "_plans",
        "_policy_state",
        "__weakref__",
    )

    def __init__(
        self,
        policy_state: ProactivePolicyState,
        group_scenes: GroupSceneBook,
        personas: tuple[PersonaPackage, ...],
    ) -> None:
        if type(policy_state) is not ProactivePolicyState:
            raise ContractViolation("proactive_policy_state_required")
        if type(group_scenes) is not GroupSceneBook:
            raise ContractViolation("proactive_group_scene_book_required")
        if type(personas) is not tuple or not personas:
            raise ContractViolation("proactive_topic_personas_required")
        unique: list[PersonaPackage] = []
        snapshots: dict[int, tuple[object, ...]] = {}
        for persona in personas:
            if type(persona) is not PersonaPackage or any(
                persona is registered for registered in unique
            ):
                raise ContractViolation("proactive_topic_personas_invalid")
            if not validate_persona_package(persona).is_valid:
                raise ContractViolation("proactive_topic_persona_invalid")
            snapshot = _persona_topic_snapshot(persona)
            unique.append(persona)
            snapshots[id(persona)] = snapshot
        self._policy_state = policy_state
        self._group_scenes = group_scenes
        self._personas = tuple(unique)
        self._persona_snapshots = snapshots
        self._plans: weakref.WeakKeyDictionary[
            ProactiveTopicPlan,
            _PlanRecord,
        ] = weakref.WeakKeyDictionary()
        self._consumed: weakref.WeakKeyDictionary[
            ProactivePolicyDecision,
            bool,
        ] = weakref.WeakKeyDictionary()
        self._lock = threading.RLock()

    def _inspect_persona(self, persona: PersonaPackage) -> tuple[object, ...]:
        if type(persona) is not PersonaPackage or not any(
            persona is value for value in self._personas
        ):
            raise ContractViolation("proactive_topic_persona_not_canonical")
        expected = self._persona_snapshots.get(id(persona))
        current = _persona_topic_snapshot(persona)
        if expected is None or current != expected:
            raise ContractViolation("proactive_topic_persona_corrupt")
        return current

    def has_grounded_context(
        self,
        scene: GroupSceneSnapshot,
        *,
        persona: PersonaPackage,
        excluded_topic_digests: tuple[str, ...] = (),
    ) -> bool:
        """Preflight topic availability without consuming a policy decision.

        This keeps an ungrounded or too-shallow scene silent without spending
        cooldown/daily quota. Corrupt or cross-authority inputs still raise.
        """

        return self.grounded_context_reason(
            scene,
            persona=persona,
            excluded_topic_digests=excluded_topic_digests,
        ) == "grounded_context_ready"

    def grounded_context_reason(
        self,
        scene: GroupSceneSnapshot,
        *,
        persona: PersonaPackage,
        excluded_topic_digests: tuple[str, ...] = (),
    ) -> str:
        """Return an exact content-free preflight reason for scheduler traces."""

        with self._lock:
            self._inspect_persona(persona)
            self._group_scenes.inspect_current_snapshot(scene)
            try:
                self._selection(
                    scene,
                    persona,
                    excluded_topic_digests=excluded_topic_digests,
                )
                self._recent_public_context(scene, persona)
            except ContractViolation as exc:
                if str(exc) in {
                    "proactive_topic_context_too_shallow",
                    "proactive_topic_no_grounded_match",
                    "proactive_topic_recently_used",
                    "proactive_topic_unavailable",
                }:
                    return str(exc)
                raise
            return "grounded_context_ready"

    @staticmethod
    def _selection(
        scene: GroupSceneSnapshot,
        persona: PersonaPackage,
        *,
        excluded_topic_digests: tuple[str, ...] = (),
    ) -> tuple[
        ProactiveTopicSource,
        PublicTopic | None,
        ParticipationInterest | None,
        str,
    ]:
        if (
            type(excluded_topic_digests) is not tuple
            or len(excluded_topic_digests) > 4
            or any(
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in excluded_topic_digests
            )
        ):
            raise ContractViolation("proactive_topic_exclusions_invalid")
        excluded = frozenset(excluded_topic_digests)
        interests = persona.participation_interests
        if not interests:
            raise ContractViolation("proactive_topic_unavailable")
        recent_topics = scene.public_topics[-12:]
        recent_human_topics = tuple(
            topic
            for topic in recent_topics
            if topic.source is SceneEntrySource.HUMAN_INBOUND
        )
        if len(recent_human_topics) < 2:
            raise ContractViolation("proactive_topic_context_too_shallow")
        matches: list[
            tuple[float, int, int, PublicTopic, ParticipationInterest]
        ] = []
        for topic_index, topic in enumerate(recent_human_topics):
            if topic.content_digest in excluded:
                continue
            folded = topic.content.casefold()
            for interest in interests:
                if any(keyword.casefold() in folded for keyword in interest.keywords):
                    matches.append(
                        (
                            interest.weight,
                            topic.conversation_revision,
                            topic_index,
                            topic,
                            interest,
                        )
                    )
        if matches:
            _, _, _, topic, interest = max(matches, key=lambda value: value[:3])
            return (
                ProactiveTopicSource.GROUP_PUBLIC_TOPIC,
                topic,
                interest,
                topic.content,
            )
        fallback = next(
            (
                topic
                for topic in reversed(recent_human_topics)
                if topic.content_digest not in excluded
                and len("".join(topic.content.split())) >= 4
            ),
            None,
        )
        if fallback is not None:
            return (
                ProactiveTopicSource.GROUP_RECENT_TOPIC,
                fallback,
                None,
                fallback.content,
            )
        if any(topic.content_digest in excluded for topic in recent_human_topics):
            raise ContractViolation("proactive_topic_recently_used")
        raise ContractViolation("proactive_topic_no_grounded_match")

    @staticmethod
    def _recent_public_context(
        scene: GroupSceneSnapshot,
        persona: PersonaPackage,
    ) -> tuple[CanonicalModelMessage, ...]:
        messages = project_group_scene_model_messages(
            scene,
            assistant_display_name=persona.display_name,
            source_kind="proactive_public_scene",
            max_messages=8,
            max_chars=4000,
            max_message_chars=600,
            selection_order="chronological",
        )
        if len(messages) < 2:
            raise ContractViolation("proactive_topic_context_too_shallow")
        return messages

    def select(
        self,
        policy_decision: ProactivePolicyDecision,
        *,
        scene: GroupSceneSnapshot,
        persona: PersonaPackage,
    ) -> ProactiveTopicPlan:
        with self._lock:
            self._policy_state.inspect(policy_decision)
            if (
                policy_decision.kind is not ProactivePolicyDecisionKind.ADMITTED
                or not policy_decision.admitted
            ):
                raise ContractViolation("proactive_topic_policy_not_admitted")
            if self._consumed.get(policy_decision) is True:
                raise ContractViolation("proactive_topic_policy_consumed")
            persona_snapshot = self._inspect_persona(persona)
            self._group_scenes.inspect_current_snapshot(scene)
            target = policy_decision.candidate.target
            if scene.scope_key != target.scope_key or scene.conversation_revision < 1:
                raise ContractViolation("proactive_topic_scene_mismatch")
            recent_topic_digests, _ = self._policy_state.recent_delivery_digests(
                platform_id=target.platform_id,
                bot_id=target.bot_id,
                group_id=target.group_id,
            )
            source, public_topic, interest, topic_text = self._selection(
                scene,
                persona,
                excluded_topic_digests=recent_topic_digests,
            )
            topic_digest = _digest_text(topic_text)
            recent_public_context = self._recent_public_context(scene, persona)
            public_context_digest = canonical_model_messages_digest(
                recent_public_context
            )
            plan = object.__new__(ProactiveTopicPlan)
            for name, value in (
                ("policy_decision", policy_decision),
                ("target", target),
                ("source", source),
                ("topic_text", topic_text),
                ("topic_digest", topic_digest),
                ("recent_public_context", recent_public_context),
                ("public_context_digest", public_context_digest),
                (
                    "interest_id",
                    interest.id if interest is not None else "general_public_topic",
                ),
                ("scene_revision", scene.conversation_revision),
                ("model_authorized", False),
                ("send_authorized", False),
                ("_authority_ref", weakref.ref(self)),
                ("_seal", _PLAN_SEAL),
            ):
                object.__setattr__(plan, name, value)
            snapshot = _plan_snapshot(plan)
            record = _PlanRecord(
                policy_decision=policy_decision,
                target=target,
                scene=scene,
                scene_snapshot=_scene_topic_snapshot(scene),
                persona=persona,
                persona_snapshot=persona_snapshot,
                source=source,
                public_topic=public_topic,
                interest=interest,
                topic_text=topic_text,
                topic_digest=topic_digest,
                recent_public_context=recent_public_context,
                public_context_digest=public_context_digest,
                interest_id=(
                    interest.id if interest is not None else "general_public_topic"
                ),
                scene_revision=scene.conversation_revision,
                snapshot=snapshot,
            )
            self._plans[plan] = record
            self._consumed[policy_decision] = True
            return plan

    def _inspect(
        self,
        plan: ProactiveTopicPlan,
        *,
        require_current: bool = True,
    ) -> _PlanRecord:
        if type(plan) is not ProactiveTopicPlan:
            raise ContractViolation("proactive_topic_plan_not_canonical")
        with self._lock:
            record = self._plans.get(plan)
            if type(record) is not _PlanRecord:
                raise ContractViolation("proactive_topic_plan_not_canonical")
            try:
                snapshot = _plan_snapshot(plan)
            except ContractViolation as exc:
                raise ContractViolation("proactive_topic_plan_corrupt") from exc
            if (
                snapshot != record.snapshot
                or plan.policy_decision is not record.policy_decision
                or plan.target is not record.target
                or plan._authority_ref() is not self
            ):
                raise ContractViolation("proactive_topic_plan_corrupt")
            if require_current:
                self._policy_state.inspect(record.policy_decision)
                self._group_scenes.inspect_current_snapshot(record.scene)
            else:
                self._policy_state.inspect_integrity(record.policy_decision)
            if _scene_topic_snapshot(record.scene) != record.scene_snapshot:
                raise ContractViolation("proactive_topic_scene_corrupt")
            if _persona_topic_snapshot(record.persona) != record.persona_snapshot:
                raise ContractViolation("proactive_topic_persona_corrupt")
            if record.public_topic is not None and not any(
                record.public_topic is topic for topic in record.scene.public_topics
            ):
                raise ContractViolation("proactive_topic_plan_corrupt")
            if record.interest is not None and not any(
                record.interest is interest
                for interest in record.persona.participation_interests
            ):
                raise ContractViolation("proactive_topic_plan_corrupt")
            return record

    def inspect(self, plan: ProactiveTopicPlan) -> ProactiveTopicPlan:
        self._inspect(plan)
        return plan

    def inspect_scene(self, scene: GroupSceneSnapshot) -> GroupSceneSnapshot:
        return self._group_scenes.inspect_current_snapshot(scene)

    def recent_delivery_digests(
        self,
        plan: ProactiveTopicPlan,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        record = self._inspect(plan, require_current=False)
        target = record.target
        return self._policy_state.recent_delivery_digests(
            platform_id=target.platform_id,
            bot_id=target.bot_id,
            group_id=target.group_id,
        )

    def _restart_groups(self) -> tuple[object, ...]:
        """Return code-owned restart targets from the bound canonical scene book."""

        return self._group_scenes._proactive_restart_groups()

    def inspect_for_execution(
        self,
        plan: ProactiveTopicPlan,
        *,
        persona: PersonaPackage,
    ) -> ProactiveTopicPlan:
        record = self._inspect(plan)
        if type(persona) is not PersonaPackage or persona is not record.persona:
            raise ContractViolation("proactive_topic_persona_not_canonical")
        self._inspect_persona(persona)
        return plan

    def inspect_integrity_for_execution(
        self,
        plan: ProactiveTopicPlan,
        *,
        persona: PersonaPackage,
    ) -> ProactiveTopicPlan:
        record = self._inspect(plan, require_current=False)
        if type(persona) is not PersonaPackage or persona is not record.persona:
            raise ContractViolation("proactive_topic_persona_not_canonical")
        self._inspect_persona(persona)
        return plan

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "schema_version": 1,
                "proactive_topic_plan_count": len(self._plans),
                "proactive_topic_consumed_count": len(self._consumed),
                "proactive_topic_model_authorized": False,
                "proactive_topic_send_authorized": False,
            }


__all__ = [
    "ProactiveTopicAuthority",
    "ProactiveTopicPlan",
    "ProactiveTopicSource",
]
