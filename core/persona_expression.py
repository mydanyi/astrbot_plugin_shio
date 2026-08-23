from __future__ import annotations

from dataclasses import dataclass

from .affect import (
    AffectAppraisal,
    AffectTrigger,
    RelationshipDistance,
    trusted_relationship_distance,
)
from .identity import PrincipalContext
from .persona import (
    PersonaPackage,
    RelationshipExpressionRule,
    validate_persona_package,
)


@dataclass(frozen=True, slots=True)
class PersonaExpressionPlan:
    package_id: str
    package_version: str
    trigger: AffectTrigger
    relationship_distance: RelationshipDistance
    address_style: str
    warmth: float
    allowed_action_ids: tuple[str, ...]
    forbidden_action_ids: tuple[str, ...]
    boundary_style: str
    surface_behavior_ids: tuple[str, ...]
    hidden_reveal_behavior_ids: tuple[str, ...]
    avoid_behavior_ids: tuple[str, ...]
    material_ids: tuple[str, ...]
    material_instructions: tuple[str, ...]
    catchphrase_candidates: tuple[str, ...]
    trajectory_steps: tuple[str, ...]
    topic_return: str
    requires_replan: bool
    degradation_reasons: tuple[str, ...]

    @property
    def is_actionable(self) -> bool:
        return not self.requires_replan and not self.degradation_reasons


def _unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _relationship_rule(
    package: PersonaPackage,
    distance: RelationshipDistance,
) -> RelationshipExpressionRule | None:
    matches = tuple(
        rule for rule in package.relationship_rules if rule.distance == distance
    )
    return matches[0] if len(matches) == 1 else None


def _recent_use_count(text: str, recent_visible_replies: tuple[str, ...]) -> int:
    phrase = str(text or "").strip()
    if not phrase:
        return 0
    return sum(str(reply or "").count(phrase) for reply in recent_visible_replies)


def build_persona_expression_plan(
    package: PersonaPackage,
    appraisal: AffectAppraisal,
    *,
    principal: PrincipalContext | None,
    conversation_mode: str = "direct_reply",
    situational_tags: tuple[str, ...] = (),
    recent_visible_replies: tuple[str, ...] = (),
) -> PersonaExpressionPlan:
    """Resolve one appraisal through a persona asset without changing authority."""

    degradation: list[str] = list(appraisal.degradation_reasons)
    trusted_distance = trusted_relationship_distance(principal, conversation_mode)
    if appraisal.relationship_distance != trusted_distance:
        degradation.append("principal_relationship_mismatch")
    if (
        str(conversation_mode or "direct_reply").strip().lower() == "direct_reply"
        and appraisal.focus_sender_key
        and (principal is None or appraisal.focus_sender_key != principal.sender_key)
    ):
        degradation.append("principal_expression_target_mismatch")
    validation = validate_persona_package(package)
    if not validation.is_valid:
        degradation.append("invalid_persona_package")

    relationship = _relationship_rule(package, appraisal.relationship_distance)
    if relationship is None:
        degradation.append("missing_relationship_expression_rule")

    emotion_matches = tuple(
        rule for rule in package.emotion_rules if rule.trigger == appraisal.trigger
    )
    emotion = emotion_matches[0] if len(emotion_matches) == 1 else None
    if emotion is None:
        degradation.append("missing_emotion_expression_rule")

    if not appraisal.is_actionable:
        degradation.append("unactionable_affect_appraisal")

    degradation = list(dict.fromkeys(degradation))
    if degradation or relationship is None or emotion is None:
        return PersonaExpressionPlan(
            package_id=package.package_id,
            package_version=package.version,
            trigger=appraisal.trigger,
            relationship_distance=appraisal.relationship_distance,
            address_style="",
            warmth=0.0,
            allowed_action_ids=(),
            forbidden_action_ids=(),
            boundary_style="",
            surface_behavior_ids=(),
            hidden_reveal_behavior_ids=(),
            avoid_behavior_ids=(),
            material_ids=(),
            material_instructions=(),
            catchphrase_candidates=(),
            trajectory_steps=(),
            topic_return=appraisal.topic_return.value,
            requires_replan=True,
            degradation_reasons=tuple(degradation),
        )

    active_tags = {appraisal.trigger.value}
    active_tags.update(str(tag or "").strip() for tag in situational_tags)
    active_tags.discard("")
    materials = tuple(
        material
        for material in package.expression_materials
        if active_tags.intersection(material.trigger_ids)
        and (
            not material.relationship_distances
            or appraisal.relationship_distance in material.relationship_distances
        )
    )
    catchphrases = tuple(
        rule.text
        for rule in package.language.catchphrases
        if active_tags.intersection(rule.trigger_ids)
        and rule.max_uses_in_recent_replies > 0
        and _recent_use_count(rule.text, recent_visible_replies)
        < rule.max_uses_in_recent_replies
    )
    trajectory = _unique(
        [
            *emotion.surface_behavior_ids,
            *(
                behavior_id
                for material in materials
                for behavior_id in material.behavior_ids
            ),
            *emotion.hidden_reveal_behavior_ids,
            appraisal.topic_return.value,
        ]
    )
    return PersonaExpressionPlan(
        package_id=package.package_id,
        package_version=package.version,
        trigger=appraisal.trigger,
        relationship_distance=appraisal.relationship_distance,
        address_style=relationship.address_style,
        warmth=relationship.warmth,
        allowed_action_ids=relationship.allowed_action_ids,
        forbidden_action_ids=relationship.forbidden_action_ids,
        boundary_style=relationship.boundary_style,
        surface_behavior_ids=emotion.surface_behavior_ids,
        hidden_reveal_behavior_ids=emotion.hidden_reveal_behavior_ids,
        avoid_behavior_ids=emotion.avoid_behavior_ids,
        material_ids=tuple(material.id for material in materials),
        material_instructions=tuple(material.text for material in materials),
        catchphrase_candidates=catchphrases,
        trajectory_steps=trajectory,
        topic_return=appraisal.topic_return.value,
        requires_replan=False,
        degradation_reasons=(),
    )
