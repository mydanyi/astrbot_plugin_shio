from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from .persona import PersonaPackage
from .persona_expression import PersonaExpressionPlan


@dataclass(frozen=True, slots=True)
class LocalExpressionCandidate:
    material_id: str
    instruction: str
    behavior_ids: tuple[str, ...]
    matched_tags: tuple[str, ...]
    relationship_specific: bool
    score: float


@dataclass(frozen=True, slots=True)
class ExpressionRetrievalResult:
    candidates: tuple[LocalExpressionCandidate, ...]
    reason_codes: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.candidates


def retrieve_expression_candidates(
    package: PersonaPackage,
    expression_plan: PersonaExpressionPlan,
    *,
    situational_tags: tuple[str, ...] = (),
    recent_material_ids: tuple[str, ...] = (),
    feedback_scores: Mapping[str, float] | None = None,
    max_candidates: int = 3,
) -> ExpressionRetrievalResult:
    """Return zero to three local behavior hints; an empty result is valid."""

    if not expression_plan.is_actionable:
        return ExpressionRetrievalResult((), ("unactionable_expression_plan",))
    if expression_plan.package_id != package.package_id:
        return ExpressionRetrievalResult((), ("persona_package_mismatch",))

    limit = max(0, min(3, int(max_candidates)))
    if limit == 0:
        return ExpressionRetrievalResult((), ("candidate_limit_zero",))

    trigger = expression_plan.trigger.value
    situation = {
        str(tag or "").strip() for tag in situational_tags if str(tag or "").strip()
    }
    active_tags = {trigger, *situation}
    planned_ids = set(expression_plan.material_ids)
    recent_ids = {
        str(material_id or "").strip()
        for material_id in recent_material_ids
        if str(material_id or "").strip()
    }
    trajectory = set(expression_plan.trajectory_steps)
    ranked: list[tuple[float, int, LocalExpressionCandidate]] = []
    matching_count = 0
    for index, material in enumerate(package.expression_materials):
        if material.id not in planned_ids:
            continue
        matched = tuple(
            tag for tag in material.trigger_ids if tag in active_tags
        )
        if not matched:
            continue
        matching_count += 1
        if material.id in recent_ids:
            continue
        behavior_overlap = len(set(material.behavior_ids).intersection(trajectory))
        relationship_specific = bool(material.relationship_distances)
        feedback = dict(feedback_scores or {})
        material_feedback = max(
            -2.0,
            min(2.0, float(feedback.get(material.id, 0.0) or 0.0)),
        )
        behavior_feedback = sum(
            max(-2.0, min(2.0, float(feedback.get(value, 0.0) or 0.0)))
            for value in material.behavior_ids
        )
        feedback_adjustment = max(
            -2.0,
            min(2.0, material_feedback * 0.75 + behavior_feedback * 0.15),
        )
        score = (
            (4.0 if trigger in matched else 0.0)
            + 2.0 * len(set(matched).intersection(situation))
            + (1.5 if relationship_specific else 0.0)
            + 0.5 * behavior_overlap
            + feedback_adjustment
        )
        candidate = LocalExpressionCandidate(
            material_id=material.id,
            instruction=material.text,
            behavior_ids=material.behavior_ids,
            matched_tags=matched,
            relationship_specific=relationship_specific,
            score=score,
        )
        ranked.append((score, index, candidate))

    ranked.sort(key=lambda item: (-item[0], item[1], item[2].material_id))
    candidates = tuple(item[2] for item in ranked[:limit])
    if candidates:
        return ExpressionRetrievalResult(candidates, ())
    if matching_count and recent_ids:
        return ExpressionRetrievalResult((), ("all_matching_materials_recent",))
    return ExpressionRetrievalResult((), ("no_matching_expression_material",))
