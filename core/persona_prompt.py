from __future__ import annotations

from .affect import RelationshipDistance
from .contracts import ContractViolation
from .persona import (
    PersonaPackage,
    RelationshipExpressionRule,
    validate_persona_package,
)


def project_public_persona_prompt_data(
    persona: PersonaPackage,
    *,
    relationship_distance: RelationshipDistance,
) -> dict[str, object]:
    """Project the complete public Persona surface without granting authority."""

    if type(persona) is not PersonaPackage:
        raise ContractViolation("persona_prompt_package_required")
    if type(relationship_distance) is not RelationshipDistance:
        raise ContractViolation("persona_prompt_relationship_required")
    report = validate_persona_package(persona)
    if not report.is_valid:
        raise ContractViolation("persona_prompt_package_invalid")
    matching = tuple(
        rule
        for rule in persona.relationship_rules
        if type(rule) is RelationshipExpressionRule
        and rule.distance is relationship_distance
    )
    if len(matching) != 1:
        raise ContractViolation("persona_prompt_relationship_ambiguous")
    relationship = matching[0]
    return {
        "display_name": persona.display_name,
        "identity_summary": persona.identity_summary,
        "value_guides": [
            {
                "id": trait.id,
                "guidance": trait.description,
                "weight": round(trait.weight, 3),
            }
            for trait in persona.core_traits
        ],
        "character_facts": [
            {
                "id": fact.id,
                "content": fact.content,
                "source_kind": fact.source_kind,
            }
            for fact in persona.character_facts
        ],
        "primary_locale": persona.language.primary_locale,
        "default_reply_length": persona.language.default_reply_length,
        "formatting_style": persona.language.formatting_style,
        "relationship": {
            "distance": relationship.distance.value,
            "address_style": relationship.address_style,
            "boundary_style": relationship.boundary_style,
            "warmth": relationship.warmth,
            "allowed_action_ids": list(relationship.allowed_action_ids),
            "forbidden_action_ids": list(relationship.forbidden_action_ids),
            "expression_only": True,
        },
    }


__all__ = ["project_public_persona_prompt_data"]
