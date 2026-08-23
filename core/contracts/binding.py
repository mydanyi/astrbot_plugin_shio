from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..observability import diagnostic_digest
from ._validation import ContractViolation, require_hex, require_text


@dataclass(frozen=True, slots=True)
class DecisionBinding:
    """Immutable identity shared by every decision for one conversation revision."""

    scope_key: str
    session_id: str
    current_message_id: str
    current_sender_key: str
    current_content_digest: str
    conversation_revision: int
    generation_epoch: int
    trace_id: str

    def __post_init__(self) -> None:
        for field in (
            "scope_key",
            "session_id",
            "current_message_id",
            "current_sender_key",
        ):
            object.__setattr__(self, field, require_text(getattr(self, field), field))
        object.__setattr__(
            self,
            "current_content_digest",
            require_hex(self.current_content_digest, "current_content_digest", lengths=(64,)),
        )
        object.__setattr__(
            self,
            "trace_id",
            require_hex(self.trace_id, "trace_id", lengths=(32,)),
        )
        if not isinstance(self.conversation_revision, int) or self.conversation_revision < 1:
            raise ContractViolation("conversation_revision_invalid")
        if not isinstance(self.generation_epoch, int) or self.generation_epoch < 1:
            raise ContractViolation("generation_epoch_invalid")

    def trace_metadata(self) -> dict[str, str | int]:
        return {
            "schema_version": 1,
            "message_digest": diagnostic_digest(self.current_message_id),
            "sender_digest": diagnostic_digest(self.current_sender_key),
            "scope_digest": diagnostic_digest(self.scope_key),
            "conversation_revision": self.conversation_revision,
            "generation_epoch": self.generation_epoch,
        }


class BoundDecision(Protocol):
    binding: DecisionBinding


def require_same_binding(*values: BoundDecision) -> DecisionBinding:
    if not values:
        raise ContractViolation("binding_values_required")
    expected = values[0].binding
    if any(value.binding != expected for value in values[1:]):
        raise ContractViolation("decision_binding_mismatch")
    return expected

