from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .contracts import ActionKind

if TYPE_CHECKING:
    from .action_planner import PlannedAction
    from .identity import PrincipalContext, TurnEnvelope


@dataclass(frozen=True, slots=True)
class TypedTurnGate:
    """Fail-closed decision for one trusted direct turn."""

    activation_ready: bool
    status: str
    reason_codes: tuple[str, ...]

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "typed_turn_ready": self.activation_ready,
            "typed_turn_status": self.status,
            "typed_turn_reason_count": len(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class TypedRuntimeDecision:
    """The only runtime contract supported by Shio."""

    activation_ready: bool
    activation_status: str
    action_kind: str = ""
    acquisition_call_budget: int = 0
    final_generation_budget: int = 0
    final_send_budget: int = 0

    @property
    def activate_typed_pipeline(self) -> bool:
        return self.activation_ready and bool(self.action_kind)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return {
            "typed_runtime": "typed_only",
            "typed_runtime_ready": self.activation_ready,
            "typed_runtime_status": self.activation_status,
            "typed_action_kind": self.action_kind,
            "typed_acquisition_call_budget": self.acquisition_call_budget,
            "typed_final_generation_budget": self.final_generation_budget,
            "typed_final_send_budget": self.final_send_budget,
        }


def validate_typed_turn(
    *,
    envelope: "TurnEnvelope",
    principal: "PrincipalContext",
) -> TypedTurnGate:
    """Validate that the current event can enter the typed direct pipeline."""

    reasons: list[str] = []
    if envelope.source_kind != "inbound":
        reasons.append("non_inbound_source_denied")
    if not all(
        (
            envelope.session_id,
            envelope.message_id,
            envelope.scope_key,
            envelope.sender_id,
            envelope.sender_key,
            envelope.platform_id,
            envelope.bot_id,
        )
    ):
        reasons.append("target_identity_incomplete")
    if (
        principal.sender_id != envelope.sender_id
        or principal.sender_key != envelope.sender_key
    ):
        reasons.append("principal_envelope_mismatch")
    if envelope.chat_type not in {"private", "group"}:
        reasons.append("direct_chat_type_required")
    expected_roles = {"owner"} if principal.is_owner else {"group_peer", "private_peer"}
    if principal.relationship_role not in expected_roles:
        reasons.append("verified_relationship_required")
    expected_source = (
        "astrbot_event_sender_id:configured_owner_id"
        if principal.is_owner
        else "astrbot_event_sender_id:owner_allowlist_miss"
    )
    if principal.verification_source != expected_source:
        reasons.append("trusted_sender_source_required")

    reason_codes = tuple(dict.fromkeys(reasons))
    ready = not reason_codes
    return TypedTurnGate(
        activation_ready=ready,
        status="typed_identity_ready" if ready else "typed_identity_denied",
        reason_codes=reason_codes,
    )


def typed_runtime_decision(
    *,
    activation_ready: bool = False,
    planned_action: "PlannedAction | None" = None,
) -> TypedRuntimeDecision:
    """Derive execution budgets from the one code-owned PlannedAction."""

    ready = bool(activation_ready)
    if not ready:
        return TypedRuntimeDecision(
            activation_ready=False,
            activation_status="typed_fail_closed",
        )
    if planned_action is None:
        return TypedRuntimeDecision(
            activation_ready=True,
            activation_status="typed_awaiting_action",
        )
    from .action_planner import PlannedAction

    if not isinstance(planned_action, PlannedAction):
        raise TypeError("planned_action_required")
    kind = planned_action.kind
    budgets = {
        ActionKind.REPLY: (0, 1, 1),
        ActionKind.USE_TOOL: (1, 1, 1),
        # Owner actions execute, if ever enabled, through the separately sealed
        # executor.  The final Persona renderer remains a zero-tool generation.
        ActionKind.EXECUTE_ACTION: (0, 1, 1),
        ActionKind.REACT: (0, 0, 0),
        ActionKind.WAIT: (0, 0, 0),
        ActionKind.NO_ACTION: (0, 0, 0),
        ActionKind.INITIATE: (0, 1, 1),
    }
    acquisition, generation, send = budgets[kind]
    return TypedRuntimeDecision(
        activation_ready=True,
        activation_status="typed_ready",
        action_kind=kind.value,
        acquisition_call_budget=acquisition,
        final_generation_budget=generation,
        final_send_budget=send,
    )
