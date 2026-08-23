from __future__ import annotations

import hashlib
import re
import secrets
import threading
from dataclasses import dataclass, field
from enum import Enum

from .accepted_turn_authority import (
    AcceptedTurnAuthority,
    AcceptedTurnConsumer,
    AcceptedTurnTicket,
)
from .capability_policy import CapabilityClass, build_owner_capability_policy
from .contracts import ContractViolation
from .contracts.owner_action import OwnerActionOperation, OwnerActionProposal
from .conversation_event import PluginSource, SenderKind


_ROUTE_SEAL = object()
_DEFAULT_MAX_ROUTES = 256


class OwnerActionRouteStatus(str, Enum):
    MATCHED = "matched"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    IDENTITY_REJECTED = "identity_rejected"
    PRIVATE_REQUIRED = "private_required"
    REFERENCE_REJECTED = "reference_rejected"


_OPERATION_CAPABILITY: dict[OwnerActionOperation, CapabilityClass] = {
    OwnerActionOperation.ARTIFACT_READ_EXACT: CapabilityClass.ARTIFACT_READ,
    OwnerActionOperation.ARTIFACT_GREP: CapabilityClass.ARTIFACT_READ,
    OwnerActionOperation.MEMORY_WRITE_LITERAL: CapabilityClass.MEMORY_WRITE,
    OwnerActionOperation.SANDBOX_SHELL_ONCE: CapabilityClass.SHELL_EXEC,
}

_NEGATION_OR_DISCUSSION_RE = re.compile(
    r"(?:不要|别(?:去|再|给我)?|不用|无需|不需要|不许|禁止|"
    r"示例|例子|教程|比如|例如|假如|如果|要是|我觉得|这个说法|这个称呼|怎么写|如何写)",
    re.IGNORECASE,
)
_ASSIGNMENT_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_-]*)\s*=")
_READ_RE = re.compile(r"(?:读取|读一下|查看文件|打开文件|\bread\s+file\b)", re.IGNORECASE)
_GREP_RE = re.compile(r"(?:搜索|查找|检索|\bgrep\b)", re.IGNORECASE)
_MEMORY_RE = re.compile(
    r"^\s*(?:(?:请(?:你)?|麻烦(?:你)?|帮我)\s*)?记住\s*[:：]\s*\S(?:[\s\S]*\S)?\s*$"
)
_FENCED_RE = re.compile(r"```[A-Za-z0-9_-]+[ \t]*\r?\n[\s\S]*?\r?\n```")
_EXECUTE_RE = re.compile(r"(?:执行|运行|\bexecute\b|\brun\b)", re.IGNORECASE)


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class OwnerActionRouteDecision:
    """Opaque module-issued route handle.

    The visible proposal remains a parameter-free value object so several later
    planning stages may inspect it.  It is never authority by itself: only the
    exact handle registered by its issuing :class:`OwnerActionRouter` may be
    inspected, and that handle may be claimed exactly once by the downstream
    authority bridge.
    """

    status: OwnerActionRouteStatus
    proposal: OwnerActionProposal | None = field(default=None, repr=False)
    _route_digest: str = field(repr=False, compare=False)
    _issuer_seal: object = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def trace_metadata(self) -> dict[str, str | int | bool]:
        status = (
            self.status
            if type(self.status) is OwnerActionRouteStatus
            else None
        )
        proposal = (
            self.proposal
            if type(self.proposal) is OwnerActionProposal
            else None
        )
        operation = (
            proposal.operation
            if proposal is not None
            and type(proposal.operation) is OwnerActionOperation
            else None
        )
        return {
            "schema_version": 1,
            "route_status": status.value if status is not None else "invalid",
            "proposal_present": proposal is not None,
            "operation": operation.value if operation is not None else "none",
            "parameter_count": 0,
            "model_used": False,
            "route_handle_bound": (
                getattr(self, "_seal", None) is _ROUTE_SEAL
                and bool(getattr(self, "_route_digest", ""))
            ),
        }

    def __repr__(self) -> str:
        metadata = self.trace_metadata()
        return (
            "OwnerActionRouteDecision("
            f"status={metadata['route_status']!r}, "
            f"operation={metadata['operation']!r}, "
            f"proposal_present={metadata['proposal_present']}, "
            "parameters_exposed=False)"
        )


@dataclass(slots=True, repr=False)
class _RouteRecord:
    decision: OwnerActionRouteDecision
    ticket: AcceptedTurnTicket
    context: object
    status: OwnerActionRouteStatus
    proposal: OwnerActionProposal | None
    proposal_snapshot: tuple[object, ...]
    route_digest: str
    claimed: bool = False


def _message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _binding_parts(binding: object) -> tuple[object, ...]:
    try:
        return (
            binding.scope_key,
            binding.session_id,
            binding.current_message_id,
            binding.current_sender_key,
            binding.current_content_digest,
            binding.conversation_revision,
            binding.generation_epoch,
            binding.trace_id,
        )
    except AttributeError as exc:
        raise ContractViolation("owner_action_route_binding_invalid") from exc


def _proposal_snapshot(proposal: OwnerActionProposal | None) -> tuple[object, ...]:
    if proposal is None:
        return ()
    if type(proposal) is not OwnerActionProposal:
        raise ContractViolation("owner_action_route_proposal_invalid")
    return (
        proposal.binding,
        *_binding_parts(proposal.binding),
        proposal.capability,
        proposal.operation,
        proposal.confidence,
        proposal.reason_codes,
    )


def _digest_parts(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _candidate_operations(message: str) -> tuple[OwnerActionOperation, ...]:
    if _NEGATION_OR_DISCUSSION_RE.search(message):
        return ()
    assignments = tuple(match.group(1).casefold() for match in _ASSIGNMENT_RE.finditer(message))
    path_count = assignments.count("path")
    literal_count = assignments.count("literal")
    candidates: list[OwnerActionOperation] = []
    if path_count == 1 and _READ_RE.search(message):
        candidates.append(OwnerActionOperation.ARTIFACT_READ_EXACT)
    if path_count == 1 and literal_count == 1 and _GREP_RE.search(message):
        candidates.append(OwnerActionOperation.ARTIFACT_GREP)
    if _MEMORY_RE.fullmatch(message):
        candidates.append(OwnerActionOperation.MEMORY_WRITE_LITERAL)
    fenced = tuple(_FENCED_RE.finditer(message))
    if len(fenced) == 1 and message.count("```") == 2:
        outside = (message[: fenced[0].start()] + message[fenced[0].end() :]).strip()
        if _EXECUTE_RE.search(outside):
            candidates.append(OwnerActionOperation.SANDBOX_SHELL_ONCE)
    return tuple(dict.fromkeys(candidates))


class OwnerActionRouter:
    """Issue, inspect and once-claim exact current-message route handles."""

    __slots__ = (
        "_authority",
        "_issuer_seal",
        "_lock",
        "_max_routes",
        "_nonce",
        "_records",
        "_routes_by_ticket",
    )

    def __init__(
        self,
        authority: AcceptedTurnAuthority,
        *,
        max_routes: int = _DEFAULT_MAX_ROUTES,
    ) -> None:
        if type(authority) is not AcceptedTurnAuthority:
            raise ContractViolation("accepted_turn_authority_required")
        if (
            isinstance(max_routes, bool)
            or not isinstance(max_routes, int)
            or max_routes < 1
        ):
            raise ContractViolation("owner_action_route_ledger_limit_invalid")
        self._authority = authority
        self._max_routes = max_routes
        self._issuer_seal = object()
        self._nonce = secrets.token_hex(32)
        self._records: dict[int, _RouteRecord] = {}
        self._routes_by_ticket: dict[int, tuple[AcceptedTurnTicket, _RouteRecord]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _decision_shape(
        status: OwnerActionRouteStatus,
        proposal: OwnerActionProposal | None,
    ) -> None:
        if type(status) is not OwnerActionRouteStatus:
            raise ContractViolation("owner_action_route_status_invalid")
        if status is OwnerActionRouteStatus.MATCHED:
            if type(proposal) is not OwnerActionProposal:
                raise ContractViolation("owner_action_route_proposal_required")
        elif proposal is not None:
            raise ContractViolation("owner_action_route_proposal_forbidden")

    def _evict_claimed_for_new_route(self) -> None:
        if len(self._records) < self._max_routes:
            return
        for key, record in tuple(self._records.items()):
            if not record.claimed:
                continue
            self._records.pop(key, None)
            entry = self._routes_by_ticket.get(id(record.ticket))
            if entry is not None and entry[0] is record.ticket and entry[1] is record:
                self._routes_by_ticket.pop(id(record.ticket), None)
            if len(self._records) < self._max_routes:
                return
        raise ContractViolation("owner_action_route_ledger_full")

    def _context_for(self, ticket: AcceptedTurnTicket):
        if type(ticket) is not AcceptedTurnTicket:
            raise ContractViolation("accepted_turn_ticket_required")
        return self._authority.context_for(
            ticket,
            consumer=AcceptedTurnConsumer.OWNER_ACTION,
            binding=ticket.binding,
        )

    def _canonical_record(
        self,
        decision: OwnerActionRouteDecision,
        *,
        ticket: AcceptedTurnTicket,
    ) -> _RouteRecord:
        if type(decision) is not OwnerActionRouteDecision:
            raise ContractViolation("owner_action_route_required")
        record = self._records.get(id(decision))
        if record is None or record.decision is not decision:
            raise ContractViolation("owner_action_route_not_canonical")
        if record.ticket is not ticket:
            raise ContractViolation("owner_action_route_ticket_mismatch")
        context = self._context_for(ticket)
        try:
            current_snapshot = _proposal_snapshot(decision.proposal)
        except ContractViolation as exc:
            raise ContractViolation("owner_action_route_corrupt") from exc
        if (
            getattr(decision, "_issuer_seal", None) is not self._issuer_seal
            or getattr(decision, "_seal", None) is not _ROUTE_SEAL
            or decision.status is not record.status
            or decision.proposal is not record.proposal
            or current_snapshot != record.proposal_snapshot
            or decision._route_digest != record.route_digest
            or context is not record.context
            or (
                decision.proposal is not None
                and (
                    decision.proposal.binding is not context.binding
                    or type(decision.proposal.capability) is not CapabilityClass
                    or type(decision.proposal.operation) is not OwnerActionOperation
                    or type(decision.proposal.confidence) is not float
                    or type(decision.proposal.reason_codes) is not tuple
                    or any(
                        type(reason) is not str
                        for reason in decision.proposal.reason_codes
                    )
                )
            )
        ):
            raise ContractViolation("owner_action_route_corrupt")
        self._decision_shape(decision.status, decision.proposal)
        return record

    def _issue(
        self,
        *,
        ticket: AcceptedTurnTicket,
        context: object,
        status: OwnerActionRouteStatus,
        proposal: OwnerActionProposal | None,
    ) -> OwnerActionRouteDecision:
        self._decision_shape(status, proposal)
        proposal_snapshot = _proposal_snapshot(proposal)
        route_digest = _digest_parts(
            "owner-action-current-message-route-v1",
            self._nonce,
            ticket.ticket_digest,
            *_binding_parts(ticket.binding),
            status.value,
            *(str(part) for part in proposal_snapshot[9:]),
        )
        decision = object.__new__(OwnerActionRouteDecision)
        object.__setattr__(decision, "status", status)
        object.__setattr__(decision, "proposal", proposal)
        object.__setattr__(decision, "_route_digest", route_digest)
        object.__setattr__(decision, "_issuer_seal", self._issuer_seal)
        object.__setattr__(decision, "_seal", _ROUTE_SEAL)
        record = _RouteRecord(
            decision=decision,
            ticket=ticket,
            context=context,
            status=status,
            proposal=proposal,
            proposal_snapshot=proposal_snapshot,
            route_digest=route_digest,
        )
        self._records[id(decision)] = record
        self._routes_by_ticket[id(ticket)] = (ticket, record)
        return decision

    def route(
        self,
        ticket: AcceptedTurnTicket,
        *,
        current_message: str,
    ) -> OwnerActionRouteDecision:
        """Classify only one exact current owner message into one closed operation."""

        if type(current_message) is not str or not current_message:
            raise ContractViolation("current_message_required")
        context = self._context_for(ticket)
        if _message_digest(current_message) != context.content_digest:
            raise ContractViolation("current_message_binding_mismatch")
        with self._lock:
            existing = self._routes_by_ticket.get(id(ticket))
            if existing is not None and existing[0] is ticket:
                record = self._canonical_record(existing[1].decision, ticket=ticket)
                if record.claimed:
                    raise ContractViolation("owner_action_route_already_claimed")
                return record.decision

            self._evict_claimed_for_new_route()
            status: OwnerActionRouteStatus
            proposal: OwnerActionProposal | None = None
            if (
                context.sender_kind is not SenderKind.HUMAN
                or context.plugin_source is not PluginSource.NONE
            ):
                status = OwnerActionRouteStatus.IDENTITY_REJECTED
            else:
                policy = build_owner_capability_policy(
                    context.principal,
                    conversation_mode="direct_reply",
                )
                if (
                    not policy.is_owner
                    or not policy.agent_full
                    or policy.degradation_reasons
                ):
                    status = OwnerActionRouteStatus.IDENTITY_REJECTED
                elif context.envelope.chat_type != "private":
                    status = OwnerActionRouteStatus.PRIVATE_REQUIRED
                elif (
                    context.envelope.reply_to_message_id
                    or context.envelope.reply_to_sender_id
                ):
                    status = OwnerActionRouteStatus.REFERENCE_REJECTED
                else:
                    candidates = _candidate_operations(current_message)
                    if not candidates:
                        status = OwnerActionRouteStatus.NO_MATCH
                    elif len(candidates) != 1:
                        status = OwnerActionRouteStatus.AMBIGUOUS
                    else:
                        status = OwnerActionRouteStatus.MATCHED
                        operation = candidates[0]
                        proposal = OwnerActionProposal(
                            binding=context.binding,
                            capability=_OPERATION_CAPABILITY[operation],
                            operation=operation,
                            confidence=1.0,
                            reason_codes=("code_owned_current_message_route",),
                        )
            return self._issue(
                ticket=ticket,
                context=context,
                status=status,
                proposal=proposal,
            )

    def inspect_route(
        self,
        decision: OwnerActionRouteDecision,
        *,
        ticket: AcceptedTurnTicket,
    ) -> OwnerActionRouteDecision:
        """Non-consuming exact-object inspection for planning/adapter readers."""

        with self._lock:
            self._canonical_record(decision, ticket=ticket)
            return decision

    def claim_route_and_ticket(
        self,
        decision: OwnerActionRouteDecision,
        *,
        ticket: AcceptedTurnTicket,
    ) -> OwnerActionRouteDecision:
        """Atomically terminalize one route and its exact owner-action ticket.

        The router owns both the exact route record and the long-lived accepted-turn
        authority.  It therefore provides the only public commit boundary: while the
        router lock is held it validates the route, claims the exact ticket through
        the authority's public API, then performs the infallible local state flip.
        If the authority rejects the claim, the route remains active.
        """

        with self._lock:
            record = self._canonical_record(decision, ticket=ticket)
            if record.claimed:
                raise ContractViolation("owner_action_route_replayed")
            self._authority.claim_ticket(
                ticket,
                consumer=AcceptedTurnConsumer.OWNER_ACTION,
                binding=record.ticket.binding,
                context=record.context,
            )
            record.claimed = True
            return decision

    def finalize_noop_route(
        self,
        decision: OwnerActionRouteDecision,
        *,
        ticket: AcceptedTurnTicket,
    ) -> None:
        """Terminalize a route that intentionally produces no owner-action output.

        This covers ordinary owner chat, rejected/ambiguous routes, and a matched
        route that downstream policy declines.  It creates neither a request nor a
        receipt; it only closes both authority ledgers through the same composite.
        """

        self.claim_route_and_ticket(decision, ticket=ticket)

    def trace_metadata(self) -> dict[str, int | bool]:
        with self._lock:
            claimed = sum(record.claimed for record in self._records.values())
            return {
                "schema_version": 1,
                "max_routes": self._max_routes,
                "route_count": len(self._records),
                "claimed_route_count": claimed,
                "active_route_count": len(self._records) - claimed,
            }


def route_owner_action(
    router: OwnerActionRouter,
    ticket: AcceptedTurnTicket,
    *,
    current_message: str,
) -> OwnerActionRouteDecision:
    """Compatibility entry point requiring an explicit long-lived router."""

    if type(router) is not OwnerActionRouter:
        raise ContractViolation("owner_action_router_required")
    return router.route(ticket, current_message=current_message)


__all__ = [
    "OwnerActionRouteDecision",
    "OwnerActionRouteStatus",
    "OwnerActionRouter",
    "route_owner_action",
]
