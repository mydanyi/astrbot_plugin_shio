from __future__ import annotations

import hashlib
import threading
import weakref
from dataclasses import dataclass, field

from .affect import RelationshipDistance
from .contracts import (
    AddressKind,
    AttentionDecision,
    AttentionLevel,
    ContractViolation,
    DecisionBinding,
    ParticipationDecision,
    ParticipationLevel,
)
from .group_scene import GroupSceneBook, GroupSceneSnapshot
from .opportunity_attention import (
    OpportunityAttentionAuthority,
    OpportunityAttentionDecision,
    OpportunityAttentionLevel,
)
from .persona import (
    ParticipationInterest,
    PersonaPackage,
    RelationshipExpressionRule,
    validate_persona_package,
)


_ASSESSMENT_SEAL = object()
_AUTHORITY_SEAL = object()
_DEFAULT_MAX_ASSESSMENTS = 256


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _score(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 3)


def _persona_snapshot(package: PersonaPackage) -> tuple[object, ...]:
    if type(package) is not PersonaPackage:
        raise ContractViolation("participation_persona_required")
    try:
        package_id = package.package_id
        version = package.version
        interests = package.participation_interests
        relationship_rules = package.relationship_rules
    except AttributeError as exc:
        raise ContractViolation("participation_persona_corrupt") from exc
    if (
        type(package_id) is not str
        or not package_id
        or type(version) is not str
        or not version
        or type(interests) is not tuple
        or type(relationship_rules) is not tuple
    ):
        raise ContractViolation("participation_persona_corrupt")
    interest_values: list[tuple[object, ...]] = []
    for interest in interests:
        if type(interest) is not ParticipationInterest:
            raise ContractViolation("participation_persona_corrupt")
        try:
            identifier = interest.id
            keywords = interest.keywords
            weight = interest.weight
        except AttributeError as exc:
            raise ContractViolation("participation_persona_corrupt") from exc
        if (
            type(identifier) is not str
            or not identifier
            or type(keywords) is not tuple
            or any(type(keyword) is not str for keyword in keywords)
            or type(weight) is not float
        ):
            raise ContractViolation("participation_persona_corrupt")
        interest_values.append((identifier, keywords, weight))
    relationship_values: list[tuple[object, ...]] = []
    for rule in relationship_rules:
        if type(rule) is not RelationshipExpressionRule:
            raise ContractViolation("participation_persona_corrupt")
        try:
            distance = rule.distance
            warmth = rule.warmth
        except AttributeError as exc:
            raise ContractViolation("participation_persona_corrupt") from exc
        if type(distance) is not RelationshipDistance or type(warmth) is not float:
            raise ContractViolation("participation_persona_corrupt")
        relationship_values.append((distance, warmth))
    return (
        package_id,
        version,
        tuple(interest_values),
        tuple(relationship_values),
    )


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ParticipationAssessment:
    binding: DecisionBinding = field(repr=False)
    opportunity: OpportunityAttentionDecision = field(repr=False)
    attention: AttentionDecision = field(repr=False)
    decision: ParticipationDecision = field(repr=False)
    persona_package_id: str
    self_relevance: float
    interest_relevance: float
    relationship_affinity: float
    rhythm_density: float
    interruption_cost: float
    response_value: float
    recent_presence: float
    reason_codes: tuple[str, ...]
    _authority_ref: weakref.ReferenceType[ParticipationAuthority] = field(
        repr=False,
        compare=False,
    )
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | float | bool]:
        authority_ref = getattr(self, "_authority_ref", None)
        authority = authority_ref() if type(authority_ref) is weakref.ReferenceType else None
        try:
            if type(authority) is not ParticipationAuthority:
                raise ContractViolation("participation_assessment_not_canonical")
            authority.inspect(self)
        except (ContractViolation, AttributeError, TypeError):
            return {
                "schema_version": 1,
                "participation_level": "invalid",
                "participation_canonical": False,
                "participation_reason_count": 0,
            }
        return {
            "schema_version": 1,
            "participation_level": self.decision.level.value,
            "participation_self_relevance": self.self_relevance,
            "participation_interest_relevance": self.interest_relevance,
            "participation_relationship_affinity": self.relationship_affinity,
            "participation_rhythm_density": self.rhythm_density,
            "participation_interruption_cost": self.interruption_cost,
            "participation_response_value": self.response_value,
            "participation_recent_presence": self.recent_presence,
            "participation_reason_count": len(self.reason_codes),
            "participation_canonical": True,
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "ParticipationAssessment("
            f"level={metadata['participation_level']!r}, "
            f"canonical={metadata['participation_canonical']!r})"
        )


def _assessment_snapshot(assessment: ParticipationAssessment) -> tuple[object, ...]:
    if type(assessment) is not ParticipationAssessment:
        raise ContractViolation("participation_assessment_required")
    try:
        values = (
            assessment.binding,
            assessment.opportunity,
            assessment.attention,
            assessment.decision,
            assessment.persona_package_id,
            assessment.self_relevance,
            assessment.interest_relevance,
            assessment.relationship_affinity,
            assessment.rhythm_density,
            assessment.interruption_cost,
            assessment.response_value,
            assessment.recent_presence,
            assessment.reason_codes,
            assessment._authority_ref,
            assessment._seal,
        )
    except AttributeError as exc:
        raise ContractViolation("participation_assessment_corrupt") from exc
    if (
        type(values[0]) is not DecisionBinding
        or type(values[1]) is not OpportunityAttentionDecision
        or type(values[2]) is not AttentionDecision
        or type(values[3]) is not ParticipationDecision
        or type(values[4]) is not str
        or any(type(value) is not float for value in values[5:12])
        or type(values[12]) is not tuple
        or any(type(value) is not str for value in values[12])
        or type(values[13]) is not weakref.ReferenceType
        or values[14] is not _ASSESSMENT_SEAL
    ):
        raise ContractViolation("participation_assessment_corrupt")
    attention = assessment.attention
    decision = assessment.decision
    if (
        type(attention.binding) is not DecisionBinding
        or attention.binding is not assessment.binding
        or type(attention.level) is not AttentionLevel
        or type(attention.self_relevance) is not float
        or type(attention.response_value) is not float
        or type(attention.reason_codes) is not tuple
        or type(decision.binding) is not DecisionBinding
        or decision.binding is not assessment.binding
        or type(decision.level) is not ParticipationLevel
        or type(decision.interruption_cost) is not float
        or type(decision.recent_presence) is not float
        or type(decision.cooldown_remaining_s) is not float
        or decision.cooldown_remaining_s != 0.0
        or type(decision.reason_codes) is not tuple
        or any(type(value) is not str for value in attention.reason_codes)
        or any(type(value) is not str for value in decision.reason_codes)
    ):
        raise ContractViolation("participation_assessment_corrupt")
    return (
        *values,
        (
            attention.binding,
            attention.level,
            attention.self_relevance,
            attention.response_value,
            attention.reason_codes,
        ),
        (
            decision.binding,
            decision.level,
            decision.interruption_cost,
            decision.recent_presence,
            decision.cooldown_remaining_s,
            decision.reason_codes,
        ),
    )


@dataclass(frozen=True, slots=True)
class _AssessmentRecord:
    opportunity: OpportunityAttentionDecision
    persona: PersonaPackage
    persona_snapshot: tuple[object, ...]
    scene: GroupSceneSnapshot | None
    snapshot: tuple[object, ...]


@dataclass(slots=True)
class _AuthorityState:
    opportunity_authority: OpportunityAttentionAuthority
    group_scenes: GroupSceneBook
    max_assessments: int
    records: weakref.WeakKeyDictionary[ParticipationAssessment, _AssessmentRecord]
    sources: weakref.WeakKeyDictionary[
        OpportunityAttentionDecision,
        weakref.ReferenceType[ParticipationAssessment],
    ]


def _build_participation_vault():
    lock = threading.RLock()
    authorities: weakref.WeakKeyDictionary[
        ParticipationAuthority,
        _AuthorityState,
    ] = weakref.WeakKeyDictionary()

    def state_for(authority: ParticipationAuthority) -> _AuthorityState:
        if type(authority) is not ParticipationAuthority:
            raise ContractViolation("participation_authority_required")
        state = authorities.get(authority)
        if (
            state is None
            or getattr(authority, "_seal", None) is not _AUTHORITY_SEAL
            or getattr(authority, "_opportunity_authority", None)
            is not state.opportunity_authority
            or getattr(authority, "_group_scenes", None) is not state.group_scenes
        ):
            raise ContractViolation("participation_authority_not_canonical")
        return state

    def register(
        authority: ParticipationAuthority,
        opportunity_authority: OpportunityAttentionAuthority,
        group_scenes: GroupSceneBook,
        max_assessments: int,
    ) -> None:
        with lock:
            if authority in authorities:
                raise ContractViolation("participation_authority_replayed")
            authorities[authority] = _AuthorityState(
                opportunity_authority=opportunity_authority,
                group_scenes=group_scenes,
                max_assessments=max_assessments,
                records=weakref.WeakKeyDictionary(),
                sources=weakref.WeakKeyDictionary(),
            )

    def issue(
        authority: ParticipationAuthority,
        opportunity: OpportunityAttentionDecision,
        *,
        persona: PersonaPackage,
        current_message: str,
        scene: GroupSceneSnapshot | None,
    ) -> ParticipationAssessment:
        if type(opportunity) is not OpportunityAttentionDecision:
            raise ContractViolation("participation_opportunity_required")
        if type(current_message) is not str or not current_message:
            raise ContractViolation("participation_message_required")
        with lock:
            state = state_for(authority)
            context = state.opportunity_authority.context_for(opportunity)
            if _digest(current_message) != context.binding.current_content_digest:
                raise ContractViolation("participation_message_binding_mismatch")
            report = validate_persona_package(persona)
            if not report.is_valid:
                raise ContractViolation("participation_persona_invalid")
            persona_snapshot = _persona_snapshot(persona)
            if context.envelope.chat_type == "group":
                if type(scene) is not GroupSceneSnapshot:
                    raise ContractViolation("participation_scene_required")
                state.group_scenes.inspect_current_human_scene(
                    context.conversation_event,
                    scene,
                )
            elif scene is not None:
                raise ContractViolation("participation_private_scene_forbidden")
            existing_ref = state.sources.get(opportunity)
            if existing_ref is not None and existing_ref() is not None:
                raise ContractViolation("participation_opportunity_replayed")
            if len(state.records) >= state.max_assessments:
                raise ContractViolation("participation_ledger_full")

            address_kind = opportunity.address.kind
            if address_kind is AddressKind.DIRECT_SELF:
                self_relevance = 1.0
            elif address_kind is AddressKind.ABOUT_SELF:
                self_relevance = 0.95
            elif address_kind is AddressKind.OPEN_GROUP:
                self_relevance = 0.2
            else:
                self_relevance = 0.0

            folded = current_message.casefold()
            interest_relevance = max(
                (
                    interest.weight
                    for interest in persona.participation_interests
                    if any(keyword.casefold() in folded for keyword in interest.keywords)
                ),
                default=0.0,
            )
            role = context.principal.relationship_role
            distance = (
                RelationshipDistance.PRIMARY_BOND
                if role == "owner"
                else (
                    RelationshipDistance.PEER
                    if role in {"group_peer", "private_peer"}
                    else RelationshipDistance.UNVERIFIED
                )
            )
            relationship_affinity = next(
                (
                    rule.warmth
                    for rule in persona.relationship_rules
                    if rule.distance is distance
                ),
                0.0,
            )
            rhythm_density = 0.0
            recent_presence = 0.0
            if scene is not None:
                rhythm_density = _score(len(scene.public_topics) / 8.0)
                participant = scene.participant(context.envelope.sender_key)
                if participant is not None:
                    recent_presence = _score(
                        participant.shio_reply_count
                        / max(1, participant.human_turn_count)
                    )

            if opportunity.level is OpportunityAttentionLevel.REQUIRED:
                interruption_cost = 0.0
                response_value = 1.0
                level = ParticipationLevel.MUST_REPLY
                reasons = ("direct_self_must_reply",)
            elif opportunity.level is OpportunityAttentionLevel.WAIT:
                interruption_cost = 1.0
                response_value = 0.0
                level = ParticipationLevel.NO_ACTION
                reasons = ("attention_wait_no_participation",)
            elif address_kind is AddressKind.ABOUT_SELF:
                interruption_cost = _score(
                    0.12 + recent_presence * 0.35 + rhythm_density * 0.08
                )
                response_value = _score(
                    0.72 + self_relevance * 0.15 + relationship_affinity * 0.1
                )
                level = (
                    ParticipationLevel.MAY_JOIN
                    if response_value >= interruption_cost + 0.15
                    else ParticipationLevel.NO_ACTION
                )
                reasons = (
                    "about_self_response_value_sufficient"
                    if level is ParticipationLevel.MAY_JOIN
                    else "about_self_interruption_cost_high",
                )
            elif address_kind is AddressKind.OPEN_GROUP:
                interruption_cost = _score(
                    0.38 + recent_presence * 0.35 + rhythm_density * 0.08
                )
                response_value = _score(
                    0.2 + interest_relevance * 0.65 + relationship_affinity * 0.1
                )
                level = (
                    ParticipationLevel.MAY_JOIN
                    if interest_relevance >= 0.65
                    and response_value >= interruption_cost + 0.15
                    else ParticipationLevel.NO_ACTION
                )
                reasons = (
                    "persona_interest_response_value_sufficient"
                    if level is ParticipationLevel.MAY_JOIN
                    else (
                        "persona_interest_not_matched"
                        if interest_relevance == 0.0
                        else "open_group_interruption_cost_high"
                    ),
                )
            else:
                interruption_cost = 1.0
                response_value = 0.0
                level = ParticipationLevel.NO_ACTION
                reasons = ("non_candidate_address_no_participation",)

            self_relevance = _score(self_relevance)
            interest_relevance = _score(interest_relevance)
            relationship_affinity = _score(relationship_affinity)
            interruption_cost = _score(interruption_cost)
            response_value = _score(response_value)
            attention = AttentionDecision(
                binding=context.binding,
                level=(
                    AttentionLevel.FORCE
                    if opportunity.level is OpportunityAttentionLevel.REQUIRED
                    else (
                        AttentionLevel.CONSIDER
                        if opportunity.level is OpportunityAttentionLevel.CANDIDATE
                        else AttentionLevel.IGNORE
                    )
                ),
                self_relevance=self_relevance,
                response_value=response_value,
                reason_codes=opportunity.reason_codes,
            )
            decision = ParticipationDecision(
                binding=context.binding,
                level=level,
                interruption_cost=interruption_cost,
                recent_presence=recent_presence,
                cooldown_remaining_s=0.0,
                reason_codes=reasons,
            )
            assessment = object.__new__(ParticipationAssessment)
            authority_ref = weakref.ref(authority)
            for name, value in (
                ("binding", context.binding),
                ("opportunity", opportunity),
                ("attention", attention),
                ("decision", decision),
                ("persona_package_id", persona.package_id),
                ("self_relevance", self_relevance),
                ("interest_relevance", interest_relevance),
                ("relationship_affinity", relationship_affinity),
                ("rhythm_density", rhythm_density),
                ("interruption_cost", interruption_cost),
                ("response_value", response_value),
                ("recent_presence", recent_presence),
                ("reason_codes", reasons),
                ("_authority_ref", authority_ref),
                ("_seal", _ASSESSMENT_SEAL),
            ):
                object.__setattr__(assessment, name, value)
            snapshot = _assessment_snapshot(assessment)
            state.records[assessment] = _AssessmentRecord(
                opportunity=opportunity,
                persona=persona,
                persona_snapshot=persona_snapshot,
                scene=scene,
                snapshot=snapshot,
            )
            state.sources[opportunity] = weakref.ref(assessment)
            return assessment

    def inspect(
        authority: ParticipationAuthority,
        assessment: ParticipationAssessment,
    ) -> ParticipationAssessment:
        if type(assessment) is not ParticipationAssessment:
            raise ContractViolation("participation_assessment_required")
        with lock:
            state = state_for(authority)
            record = state.records.get(assessment)
            if record is None:
                raise ContractViolation("participation_assessment_not_canonical")
            try:
                snapshot = _assessment_snapshot(assessment)
                persona_snapshot = _persona_snapshot(record.persona)
            except ContractViolation as exc:
                raise ContractViolation("participation_assessment_corrupt") from exc
            if (
                snapshot != record.snapshot
                or persona_snapshot != record.persona_snapshot
                or assessment.opportunity is not record.opportunity
                or assessment._authority_ref() is not authority
            ):
                raise ContractViolation("participation_assessment_corrupt")
            context = state.opportunity_authority.context_for(record.opportunity)
            if assessment.binding is not context.binding:
                raise ContractViolation("participation_assessment_corrupt")
            if record.scene is not None:
                state.group_scenes.inspect_recorded_human_scene(
                    context.conversation_event,
                    record.scene,
                )
            return assessment

    def metrics(authority: ParticipationAuthority) -> dict[str, int | bool]:
        with lock:
            state = state_for(authority)
            count = len(state.records)
            return {
                "schema_version": 1,
                "participation_assessment_count": count,
                "participation_ledger_bounded": count <= state.max_assessments,
            }

    return register, issue, inspect, metrics


(
    _register_participation_authority,
    _issue_participation,
    _inspect_participation,
    _participation_metrics,
) = _build_participation_vault()


class ParticipationAuthority:
    """P5-02 code-owned participation authority over exact attention + scene."""

    __slots__ = (
        "_opportunity_authority",
        "_group_scenes",
        "_seal",
        "__weakref__",
    )

    def __init__(
        self,
        opportunity_authority: OpportunityAttentionAuthority,
        group_scenes: GroupSceneBook,
        *,
        max_assessments: int = _DEFAULT_MAX_ASSESSMENTS,
    ) -> None:
        if type(opportunity_authority) is not OpportunityAttentionAuthority:
            raise ContractViolation("participation_opportunity_authority_required")
        if type(group_scenes) is not GroupSceneBook:
            raise ContractViolation("participation_group_scene_book_required")
        if type(max_assessments) is not int or max_assessments < 1:
            raise ContractViolation("participation_ledger_limit_invalid")
        self._opportunity_authority = opportunity_authority
        self._group_scenes = group_scenes
        self._seal = _AUTHORITY_SEAL
        _register_participation_authority(
            self,
            opportunity_authority,
            group_scenes,
            max_assessments,
        )

    def issue(
        self,
        opportunity: OpportunityAttentionDecision,
        *,
        persona: PersonaPackage,
        current_message: str,
        scene: GroupSceneSnapshot | None,
    ) -> ParticipationAssessment:
        return _issue_participation(
            self,
            opportunity,
            persona=persona,
            current_message=current_message,
            scene=scene,
        )

    def inspect(
        self,
        assessment: ParticipationAssessment,
    ) -> ParticipationAssessment:
        return _inspect_participation(self, assessment)

    def trace_metadata(self) -> dict[str, int | bool]:
        return _participation_metrics(self)


__all__ = ["ParticipationAssessment", "ParticipationAuthority"]
