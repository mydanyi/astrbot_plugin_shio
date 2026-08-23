from __future__ import annotations

import threading
import weakref
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple

from .action_outcome import ActionOutcomeAuthority, ActionOutcomeIntent
from .action_planner import PlannedAction
from .contracts import (
    ActionEffectState,
    ActionReceipt,
    ActionReceiptStatus,
    ContractViolation,
    OwnerActionOperation,
    OwnerActionRequest,
)
from .owner_action_controller import (
    OwnerActionContinuationLineage,
    OwnerActionController,
    OwnerActionDenial,
    OwnerActionLedgerState,
)
from .owner_action_lifecycle import (
    LifecycleHandle,
    LifecyclePhase,
    OwnerActionLifecycleStore,
)
from .send_receipt import (
    InternalSendReceiptLedger,
    OwnerActionSendTerminalEvidence,
    _inspect_owner_action_send_terminal_evidence,
)


class DurableFinalizeConsumer(str, Enum):
    CONTROLLER_RELEASE = "controller_release"
    OUTCOME_RETIRE = "outcome_retire"
    PENDING_OUTCOME_RETIRE = "pending_outcome_retire"
    PREPARED_ABORT = "prepared_abort"


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class DurableFinalizeTicket:
    consumer: DurableFinalizeConsumer

    def trace_metadata(self) -> dict[str, str | int | bool]:
        return _durable_ticket_trace(self)

    def __repr__(self) -> str:
        try:
            metadata = self.trace_metadata()
        except Exception:
            return "DurableFinalizeTicket(canonical=False)"
        return (
            "DurableFinalizeTicket("
            f"consumer={metadata['consumer']!r}, canonical=True, "
            "source_visible=False)"
        )


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class DurableFinalizeDispatch:
    controller_ticket: DurableFinalizeTicket = field(repr=False)
    outcome_ticket: DurableFinalizeTicket = field(repr=False)

    def trace_metadata(self) -> dict[str, int | bool]:
        return _durable_dispatch_trace(self)

    def __repr__(self) -> str:
        try:
            metadata = self.trace_metadata()
        except Exception:
            return "DurableFinalizeDispatch(canonical=False)"
        return (
            "DurableFinalizeDispatch("
            f"ticket_count={metadata['ticket_count']!r}, "
            f"consumed_count={metadata['consumed_count']!r}, "
            "source_visible=False)"
        )


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class OwnerActionDurableFinalizeAuthority:
    store: OwnerActionLifecycleStore = field(repr=False)
    controller: OwnerActionController = field(repr=False)
    outcome_authority: ActionOutcomeAuthority = field(repr=False)
    max_dispatches: int

    @classmethod
    def issue_for_runtime(
        cls,
        store: OwnerActionLifecycleStore,
        controller: OwnerActionController,
        outcome_authority: ActionOutcomeAuthority,
        *,
        max_dispatches: int = 256,
    ) -> OwnerActionDurableFinalizeAuthority:
        return _issue_durable_authority(
            store,
            controller,
            outcome_authority,
            max_dispatches=max_dispatches,
        )

    def acknowledge_delivered(
        self,
        *,
        lifecycle_handle: LifecycleHandle,
        source: ActionReceipt | OwnerActionDenial | OwnerActionContinuationLineage,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        composer_request: object,
        send_evidence: OwnerActionSendTerminalEvidence,
        send_ledger: InternalSendReceiptLedger,
        presentation: object,
        now: float,
    ) -> DurableFinalizeDispatch:
        return _acknowledge_exact_delivery(
            self,
            mode="delivery_terminal",
            lifecycle_handle=lifecycle_handle,
            source=source,
            outcome=outcome,
            current_planned_action=current_planned_action,
            composer_request=composer_request,
            send_evidence=send_evidence,
            send_ledger=send_ledger,
            presentation=presentation,
            now=now,
        )

    def acknowledge_abandoned(
        self,
        *,
        lifecycle_handle: LifecycleHandle,
        source: ActionReceipt | OwnerActionDenial | OwnerActionContinuationLineage,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        now: float,
    ) -> DurableFinalizeDispatch:
        """Persist an internal terminal for an outcome never claimed by Composer."""

        return _acknowledge_exact_delivery(
            self,
            mode="abandoned_before_composer",
            lifecycle_handle=lifecycle_handle,
            source=source,
            outcome=outcome,
            current_planned_action=current_planned_action,
            composer_request=None,
            send_evidence=None,
            send_ledger=None,
            presentation=None,
            now=now,
        )

    def ticket_for(
        self,
        dispatch: DurableFinalizeDispatch,
        consumer: DurableFinalizeConsumer,
    ) -> DurableFinalizeTicket:
        return _ticket_for_consumer(self, dispatch, consumer)

    def complete(self, ticket: DurableFinalizeTicket) -> None:
        """Purge the Controller tombstone and release the lifecycle hold."""

        _complete_finalize_dispatch(self, ticket)

    def prepare_abort(
        self,
        *,
        lifecycle_handle: LifecycleHandle,
        request: object,
        now: float,
    ) -> DurableFinalizeTicket:
        return _prepare_durable_abort(
            self,
            lifecycle_handle=lifecycle_handle,
            request=request,
            now=now,
        )

    def complete_prepared_abort(
        self,
        ticket: DurableFinalizeTicket,
        *,
        now: float,
    ) -> None:
        _complete_durable_abort(self, ticket, now=now)

    def acknowledge_pending_delivered(
        self,
        *,
        source: ActionReceipt,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        composer_request: object,
        send_evidence: OwnerActionSendTerminalEvidence,
        send_ledger: InternalSendReceiptLedger,
        presentation: object,
    ) -> DurableFinalizeTicket:
        return _acknowledge_pending_delivery(
            self,
            source=source,
            outcome=outcome,
            current_planned_action=current_planned_action,
            composer_request=composer_request,
            send_evidence=send_evidence,
            send_ledger=send_ledger,
            presentation=presentation,
        )

    def trace_metadata(self) -> dict[str, int | bool]:
        return _durable_authority_trace(self)

    def __repr__(self) -> str:
        try:
            metadata = self.trace_metadata()
        except Exception:
            return "OwnerActionDurableFinalizeAuthority(canonical=False)"
        return (
            "OwnerActionDurableFinalizeAuthority("
            f"active_dispatches={metadata['active_dispatches']!r}, "
            f"max_dispatches={metadata['max_dispatches']!r}, "
            "source_visible=False)"
        )


class _AuthorityRegistration(NamedTuple):
    authority_ref: weakref.ReferenceType
    store: OwnerActionLifecycleStore
    controller_ref: weakref.ReferenceType
    outcome_authority_ref: weakref.ReferenceType
    max_dispatches: int


class _TerminalProjection(NamedTuple):
    operation: OwnerActionOperation
    status: ActionReceiptStatus
    effect_state: ActionEffectState
    attempted: bool
    idempotency_material: str


class _FinalizeRecord(NamedTuple):
    authority_ref: weakref.ReferenceType
    store: OwnerActionLifecycleStore
    controller_ref: weakref.ReferenceType
    outcome_authority_ref: weakref.ReferenceType
    lifecycle_handle: LifecycleHandle
    lifecycle_fingerprint: str
    source: object
    outcome: ActionOutcomeIntent
    current_planned_action: PlannedAction
    composer_request: object | None
    send_evidence: OwnerActionSendTerminalEvidence | None
    send_ledger_ref: weakref.ReferenceType | None
    presentation_ref: weakref.ReferenceType | None
    projection: _TerminalProjection
    dispatch: DurableFinalizeDispatch | None
    controller_ticket: DurableFinalizeTicket | None
    outcome_ticket: DurableFinalizeTicket | None
    controller_tombstone: object | None
    consumed: frozenset[DurableFinalizeConsumer]


class _PreparedAbortRecord(NamedTuple):
    authority_ref: weakref.ReferenceType
    store: OwnerActionLifecycleStore
    controller_ref: weakref.ReferenceType
    lifecycle_handle: LifecycleHandle
    lifecycle_fingerprint: str
    request: OwnerActionRequest
    request_operation: OwnerActionOperation
    request_digest: str
    idempotency_key: str
    action_id: str
    ticket: DurableFinalizeTicket
    tombstone: object | None
    controller_cleaned: bool


class _PendingDeliveryRecord(NamedTuple):
    authority_ref: weakref.ReferenceType
    controller_ref: weakref.ReferenceType
    outcome_authority_ref: weakref.ReferenceType
    source: ActionReceipt
    outcome: ActionOutcomeIntent
    current_planned_action: PlannedAction
    composer_request: object
    send_evidence: OwnerActionSendTerminalEvidence
    send_ledger_ref: weakref.ReferenceType
    presentation_ref: weakref.ReferenceType
    ticket: DurableFinalizeTicket
    consumed: bool


def _terminal_projection(
    controller: OwnerActionController,
    source: object,
    current_planned_action: PlannedAction,
) -> _TerminalProjection:
    if type(source) is ActionReceipt:
        inspection = controller.inspect_receipt_lineage(source)
        if inspection.planned_action is not current_planned_action:
            raise ContractViolation("durable_finalize_current_plan_mismatch")
        if source.status is ActionReceiptStatus.CONFIRMATION_REQUIRED:
            raise ContractViolation("durable_finalize_source_not_terminal")
        return _TerminalProjection(
            source.operation,
            source.status,
            source.effect_state,
            source.attempt_count == 1,
            inspection.request.idempotency_key,
        )
    if type(source) is OwnerActionDenial:
        inspection = controller.inspect_denial_lineage(source)
        if inspection.planned_action is not current_planned_action:
            raise ContractViolation("durable_finalize_current_plan_mismatch")
        return _TerminalProjection(
            inspection.operation,
            source.status,
            source.effect_state,
            False,
            source.denial_digest,
        )
    if type(source) is OwnerActionContinuationLineage:
        inspection = controller.inspect_continuation_lineage(source)
        if inspection.current_planned_action is not current_planned_action:
            raise ContractViolation("durable_finalize_current_plan_mismatch")
        receipt = inspection.receipt
        if receipt is None or receipt.status is ActionReceiptStatus.CONFIRMATION_REQUIRED:
            raise ContractViolation("durable_finalize_source_not_terminal")
        return _TerminalProjection(
            source.operation,
            receipt.status,
            receipt.effect_state,
            receipt.attempt_count == 1,
            inspection.origin_request.idempotency_key,
        )
    raise ContractViolation("durable_finalize_terminal_source_required")


def _build_durable_finalize_vault():
    lock = threading.RLock()
    registrations: tuple[_AuthorityRegistration, ...] = ()
    records: tuple[_FinalizeRecord, ...] = ()
    abort_records: tuple[_PreparedAbortRecord, ...] = ()
    pending_records: tuple[_PendingDeliveryRecord, ...] = ()

    def prune_locked() -> None:
        nonlocal registrations, records, abort_records, pending_records
        registrations = tuple(
            item
            for item in registrations
            if item.authority_ref() is not None
            and item.controller_ref() is not None
            and item.outcome_authority_ref() is not None
        )
        records = tuple(
            item
            for item in records
            if item.authority_ref() is not None
            and item.controller_ref() is not None
            and item.outcome_authority_ref() is not None
            and (
                item.send_ledger_ref is None
                or item.send_ledger_ref() is not None
            )
            and (
                item.presentation_ref is None
                or item.presentation_ref() is not None
            )
        )
        abort_records = tuple(
            item
            for item in abort_records
            if item.authority_ref() is not None
            and item.controller_ref() is not None
        )
        pending_records = tuple(
            item
            for item in pending_records
            if item.authority_ref() is not None
            and item.controller_ref() is not None
            and item.outcome_authority_ref() is not None
            and item.send_ledger_ref() is not None
            and item.presentation_ref() is not None
        )

    def registration_for(
        authority: object,
    ) -> _AuthorityRegistration:
        if type(authority) is not OwnerActionDurableFinalizeAuthority:
            raise ContractViolation("durable_finalize_authority_not_canonical")
        with lock:
            prune_locked()
            matches = tuple(
                item for item in registrations if item.authority_ref() is authority
            )
            if len(matches) != 1:
                raise ContractViolation("durable_finalize_authority_not_canonical")
            item = matches[0]
            if (
                item.store is not authority.store
                or item.controller_ref() is not authority.controller
                or item.outcome_authority_ref() is not authority.outcome_authority
                or item.max_dispatches != authority.max_dispatches
            ):
                raise ContractViolation("durable_finalize_authority_corrupt")
            return item

    def issue_authority(
        store: OwnerActionLifecycleStore,
        controller: OwnerActionController,
        outcome_authority: ActionOutcomeAuthority,
        *,
        max_dispatches: int,
    ) -> OwnerActionDurableFinalizeAuthority:
        nonlocal registrations
        if type(store) is not OwnerActionLifecycleStore:
            raise ContractViolation("durable_finalize_store_required")
        if type(controller) is not OwnerActionController:
            raise ContractViolation("durable_finalize_controller_required")
        if type(outcome_authority) is not ActionOutcomeAuthority:
            raise ContractViolation("durable_finalize_outcome_authority_required")
        if (
            type(max_dispatches) is not int
            or max_dispatches < 1
            or max_dispatches > 4096
        ):
            raise ContractViolation("durable_finalize_limit_invalid")
        with lock:
            prune_locked()
            existing = tuple(
                item
                for item in registrations
                if item.store is store
                and item.controller_ref() is controller
                and item.outcome_authority_ref() is outcome_authority
            )
            if existing:
                authority = existing[0].authority_ref()
                if authority is None or existing[0].max_dispatches != max_dispatches:
                    raise ContractViolation("durable_finalize_runtime_already_registered")
                return authority
            authority = object.__new__(OwnerActionDurableFinalizeAuthority)
            object.__setattr__(authority, "store", store)
            object.__setattr__(authority, "controller", controller)
            object.__setattr__(authority, "outcome_authority", outcome_authority)
            object.__setattr__(authority, "max_dispatches", max_dispatches)
            registrations = (
                *registrations,
                _AuthorityRegistration(
                    weakref.ref(authority),
                    store,
                    weakref.ref(controller),
                    weakref.ref(outcome_authority),
                    max_dispatches,
                ),
            )
            return authority

    def record_for_dispatch_locked(
        authority: OwnerActionDurableFinalizeAuthority,
        dispatch: DurableFinalizeDispatch,
    ) -> _FinalizeRecord:
        matches = tuple(
            record
            for record in records
            if record.authority_ref() is authority
            and record.dispatch is dispatch
        )
        if len(matches) != 1:
            raise ContractViolation("durable_finalize_dispatch_not_canonical")
        return matches[0]

    def acknowledge(
        authority: OwnerActionDurableFinalizeAuthority,
        *,
        mode: str,
        lifecycle_handle: LifecycleHandle,
        source: object,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        composer_request: object | None,
        send_evidence: OwnerActionSendTerminalEvidence | None,
        send_ledger: InternalSendReceiptLedger | None,
        presentation: object | None,
        now: float,
    ) -> DurableFinalizeDispatch:
        nonlocal records
        registration = registration_for(authority)
        if type(now) not in {int, float} or not math.isfinite(float(now)):
            raise ContractViolation("durable_abort_time_invalid")
        if mode == "delivery_terminal":
            if (
                type(send_evidence) is not OwnerActionSendTerminalEvidence
                or type(send_ledger) is not InternalSendReceiptLedger
                or composer_request is None
                or presentation is None
            ):
                raise ContractViolation("durable_finalize_send_evidence_required")
            send_inspection = _inspect_owner_action_send_terminal_evidence(
                send_evidence,
                ledger=send_ledger,
                presentation=presentation,
            )
            if (
                send_inspection.composer_request is not composer_request
                or send_inspection.action_outcome is not outcome
                or send_inspection.action_outcome_authority
                is not registration.outcome_authority_ref()
                or composer_request.planned_action is not current_planned_action
            ):
                raise ContractViolation("durable_finalize_send_lineage_mismatch")
        elif mode == "abandoned_before_composer":
            if any(
                value is not None
                for value in (
                    composer_request,
                    send_evidence,
                    send_ledger,
                    presentation,
                )
            ):
                raise ContractViolation("durable_finalize_send_evidence_forbidden")
        else:
            raise ContractViolation("durable_finalize_mode_invalid")
        projection = _terminal_projection(
            registration.controller_ref(),
            source,
            current_planned_action,
        )
        if (
            registration.outcome_authority_ref()._inspect_reclaimed_source(
                outcome,
                source,
            )
            != mode
        ):
            raise ContractViolation("durable_finalize_outcome_not_delivered")
        snapshot = registration.store.inspect_handle(lifecycle_handle)
        if (
            snapshot.phase not in {LifecyclePhase.TERMINAL, LifecyclePhase.DELIVERY_ACK}
            or snapshot.operation is not projection.operation
            or snapshot.result_status is not projection.status
            or snapshot.effect_state is not projection.effect_state
            or snapshot.attempted is not projection.attempted
        ):
            raise ContractViolation("durable_finalize_lifecycle_mismatch")
        try:
            fingerprint = object.__getattribute__(
                lifecycle_handle,
                "_request_fingerprint",
            )
        except AttributeError as exc:
            raise ContractViolation("durable_finalize_handle_corrupt") from exc
        if type(fingerprint) is not str or len(fingerprint) != 64:
            raise ContractViolation("durable_finalize_handle_corrupt")

        with lock:
            prune_locked()
            active = tuple(
                record
                for record in records
                if record.authority_ref() is authority
                and record.lifecycle_handle is lifecycle_handle
                and record.source is source
                and record.outcome is outcome
            )
            if active:
                current = active[0]
                if type(current.dispatch) is DurableFinalizeDispatch:
                    return current.dispatch
            else:
                if sum(record.authority_ref() is authority for record in records) >= (
                    registration.max_dispatches
                ):
                    raise ContractViolation("durable_finalize_dispatch_limit_reached")
                current = _FinalizeRecord(
                    authority_ref=weakref.ref(authority),
                    store=registration.store,
                    controller_ref=registration.controller_ref,
                    outcome_authority_ref=registration.outcome_authority_ref,
                    lifecycle_handle=lifecycle_handle,
                    lifecycle_fingerprint=fingerprint,
                    source=source,
                    outcome=outcome,
                    current_planned_action=current_planned_action,
                    composer_request=composer_request,
                    send_evidence=send_evidence,
                    send_ledger_ref=(
                        weakref.ref(send_ledger) if send_ledger is not None else None
                    ),
                    presentation_ref=(
                        weakref.ref(presentation) if presentation is not None else None
                    ),
                    projection=projection,
                    dispatch=None,
                    controller_ticket=None,
                    outcome_ticket=None,
                    controller_tombstone=None,
                    consumed=frozenset(),
                )
                records = (*records, current)

        if snapshot.phase is LifecyclePhase.TERMINAL:
            registration.store.acknowledge_delivery(
                lifecycle_handle,
                now=now,
            )

        with lock:
            prune_locked()
            matches = tuple(item for item in records if item is current)
            if len(matches) != 1:
                raise ContractViolation("durable_finalize_dispatch_corrupt")
            controller_ticket = object.__new__(DurableFinalizeTicket)
            object.__setattr__(
                controller_ticket,
                "consumer",
                DurableFinalizeConsumer.CONTROLLER_RELEASE,
            )
            outcome_ticket = object.__new__(DurableFinalizeTicket)
            object.__setattr__(
                outcome_ticket,
                "consumer",
                DurableFinalizeConsumer.OUTCOME_RETIRE,
            )
            dispatch = object.__new__(DurableFinalizeDispatch)
            object.__setattr__(dispatch, "controller_ticket", controller_ticket)
            object.__setattr__(dispatch, "outcome_ticket", outcome_ticket)
            replacement = current._replace(
                dispatch=dispatch,
                controller_ticket=controller_ticket,
                outcome_ticket=outcome_ticket,
            )
            records = tuple(replacement if item is current else item for item in records)
            return dispatch

    def abort_record_for_ticket_locked(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
    ) -> _PreparedAbortRecord:
        matches = tuple(
            record
            for record in abort_records
            if record.authority_ref() is authority and record.ticket is ticket
        )
        if len(matches) != 1:
            raise ContractViolation("durable_abort_ticket_not_canonical")
        record = matches[0]
        if (
            type(ticket) is not DurableFinalizeTicket
            or ticket.consumer is not DurableFinalizeConsumer.PREPARED_ABORT
            or type(record.request) is not OwnerActionRequest
            or record.request.operation is not record.request_operation
            or record.request.request_digest != record.request_digest
            or record.request.idempotency_key != record.idempotency_key
            or record.request.action_id != record.action_id
        ):
            raise ContractViolation("durable_abort_ticket_corrupt")
        return record

    def prepare_abort(
        authority: OwnerActionDurableFinalizeAuthority,
        *,
        lifecycle_handle: LifecycleHandle,
        request: object,
        now: float,
    ) -> DurableFinalizeTicket:
        nonlocal abort_records
        registration = registration_for(authority)
        if type(request) is not OwnerActionRequest:
            raise ContractViolation("owner_action_request_required")
        inspection = registration.controller_ref().inspect_request(request)
        if (
            inspection.state is not OwnerActionLedgerState.PREPARED
            or inspection.has_pending
            or inspection.has_lease
            or inspection.has_receipt
        ):
            raise ContractViolation("request_not_prepared")
        with lock:
            prune_locked()
            existing = tuple(
                record
                for record in abort_records
                if record.authority_ref() is authority
                and record.lifecycle_handle is lifecycle_handle
                and record.request is request
            )
            if existing:
                return abort_record_for_ticket_locked(
                    authority,
                    existing[0].ticket,
                ).ticket
        snapshot = registration.store.inspect_handle(lifecycle_handle)
        reserved = (
            snapshot.phase is LifecyclePhase.RESERVED
            and snapshot.result_status is None
            and snapshot.effect_state is ActionEffectState.NOT_STARTED
            and not snapshot.attempted
        )
        recovered_terminal = (
            snapshot.phase is LifecyclePhase.TERMINAL
            and snapshot.result_status is ActionReceiptStatus.CANCELLED
            and snapshot.effect_state is ActionEffectState.NOT_STARTED
            and not snapshot.attempted
        )
        if (
            snapshot.operation is not request.operation
            or not (reserved or recovered_terminal)
        ):
            raise ContractViolation("durable_abort_lifecycle_mismatch")
        try:
            fingerprint = object.__getattribute__(
                lifecycle_handle,
                "_request_fingerprint",
            )
        except AttributeError as exc:
            raise ContractViolation("durable_abort_handle_corrupt") from exc
        if type(fingerprint) is not str or len(fingerprint) != 64:
            raise ContractViolation("durable_abort_handle_corrupt")

        with lock:
            prune_locked()
            existing = tuple(
                record
                for record in abort_records
                if record.authority_ref() is authority
                and record.lifecycle_handle is lifecycle_handle
                and record.request is request
            )
            if existing:
                return abort_record_for_ticket_locked(authority, existing[0].ticket).ticket
            active_count = sum(
                record.authority_ref() is authority for record in records
            ) + sum(
                record.authority_ref() is authority for record in abort_records
            )
            if active_count >= registration.max_dispatches:
                raise ContractViolation("durable_finalize_dispatch_limit_reached")
            ticket = object.__new__(DurableFinalizeTicket)
            object.__setattr__(
                ticket,
                "consumer",
                DurableFinalizeConsumer.PREPARED_ABORT,
            )
            current = _PreparedAbortRecord(
                authority_ref=weakref.ref(authority),
                store=registration.store,
                controller_ref=registration.controller_ref,
                lifecycle_handle=lifecycle_handle,
                lifecycle_fingerprint=fingerprint,
                request=request,
                request_operation=request.operation,
                request_digest=request.request_digest,
                idempotency_key=request.idempotency_key,
                action_id=request.action_id,
                ticket=ticket,
                tombstone=None,
                controller_cleaned=False,
            )
            abort_records = (*abort_records, current)

        if reserved:
            try:
                registration.store.mark_terminal(
                    lifecycle_handle,
                    status=ActionReceiptStatus.CANCELLED,
                    effect_state=ActionEffectState.NOT_STARTED,
                    attempted=False,
                    now=now,
                )
            except BaseException:
                with lock:
                    abort_records = tuple(
                        record for record in abort_records if record is not current
                    )
                raise
        return ticket

    def inspect_abort_ticket(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        *,
        controller: OwnerActionController,
        request: OwnerActionRequest,
        require_cleaned: bool = False,
    ) -> _PreparedAbortRecord:
        registration = registration_for(authority)
        if registration.controller_ref() is not controller:
            raise ContractViolation("durable_abort_ticket_lineage_mismatch")
        if type(require_cleaned) is not bool:
            raise ContractViolation("durable_abort_ticket_state_invalid")
        with lock:
            prune_locked()
            record = abort_record_for_ticket_locked(authority, ticket)
            if record.controller_ref() is not controller or record.request is not request:
                raise ContractViolation("durable_abort_ticket_lineage_mismatch")
            if record.controller_cleaned is not require_cleaned:
                raise ContractViolation("durable_abort_ticket_state_invalid")
            if not require_cleaned:
                controller.inspect_request(request)
            return record

    def claim_abort_ticket(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        *,
        controller: OwnerActionController,
        request: OwnerActionRequest,
        tombstone: object,
    ) -> None:
        nonlocal abort_records
        registration = registration_for(authority)
        if registration.controller_ref() is not controller:
            raise ContractViolation("durable_abort_ticket_lineage_mismatch")
        with lock:
            prune_locked()
            record = abort_record_for_ticket_locked(authority, ticket)
            if (
                record.controller_ref() is not controller
                or record.request is not request
                or record.controller_cleaned
            ):
                raise ContractViolation("durable_abort_ticket_lineage_mismatch")
        controller.inspect_lifecycle_tombstone(tombstone)
        with lock:
            prune_locked()
            if record not in abort_records:
                raise ContractViolation("durable_abort_ticket_not_canonical")
            replacement = record._replace(
                tombstone=tombstone,
                controller_cleaned=True,
            )
            abort_records = tuple(
                replacement if item is record else item for item in abort_records
            )

    def inspect_completed_abort(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
    ) -> _PreparedAbortRecord:
        registration = registration_for(authority)
        with lock:
            prune_locked()
            record = abort_record_for_ticket_locked(authority, ticket)
            if not record.controller_cleaned or record.tombstone is None:
                raise ContractViolation("durable_abort_not_complete")
        snapshot = registration.store.inspect_handle(record.lifecycle_handle)
        if (
            snapshot.phase is not LifecyclePhase.DELIVERY_ACK
            or snapshot.operation is not record.request_operation
            or snapshot.result_status is not ActionReceiptStatus.CANCELLED
            or snapshot.effect_state is not ActionEffectState.NOT_STARTED
            or snapshot.attempted
        ):
            raise ContractViolation("durable_abort_lifecycle_mismatch")
        return record

    def remove_completed_abort(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        *,
        controller: OwnerActionController,
        tombstone: object,
    ) -> None:
        nonlocal abort_records
        record = inspect_completed_abort(authority, ticket)
        if (
            record.controller_ref() is not controller
            or record.tombstone is not tombstone
        ):
            raise ContractViolation("durable_abort_tombstone_mismatch")
        with lock:
            prune_locked()
            if record not in abort_records:
                raise ContractViolation("durable_abort_not_complete")
            abort_records = tuple(item for item in abort_records if item is not record)

    def complete_abort(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        *,
        now: float,
    ) -> None:
        registration = registration_for(authority)
        with lock:
            prune_locked()
            record = abort_record_for_ticket_locked(authority, ticket)
            if not record.controller_cleaned or record.tombstone is None:
                raise ContractViolation("durable_abort_not_complete")
        snapshot = registration.store.inspect_handle(record.lifecycle_handle)
        if snapshot.phase is LifecyclePhase.TERMINAL:
            registration.store.acknowledge_delivery(
                record.lifecycle_handle,
                now=now,
            )
        elif snapshot.phase is not LifecyclePhase.DELIVERY_ACK:
            raise ContractViolation("durable_abort_lifecycle_mismatch")
        record.controller_ref().purge_prepared_abort_tombstone(
            record.tombstone,
            ticket=ticket,
            durable_authority=authority,
        )

    def acknowledge_pending(
        authority: OwnerActionDurableFinalizeAuthority,
        *,
        source: ActionReceipt,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        composer_request: object,
        send_evidence: OwnerActionSendTerminalEvidence,
        send_ledger: InternalSendReceiptLedger,
        presentation: object,
    ) -> DurableFinalizeTicket:
        nonlocal pending_records
        registration = registration_for(authority)
        if type(source) is not ActionReceipt:
            raise ContractViolation("durable_pending_receipt_required")
        receipt_inspection = registration.controller_ref().inspect_receipt_lineage(
            source
        )
        request_inspection = registration.controller_ref().inspect_request(
            receipt_inspection.request
        )
        if (
            source.status is not ActionReceiptStatus.CONFIRMATION_REQUIRED
            or source.effect_state is not ActionEffectState.NOT_STARTED
            or source.attempt_count != 0
            or receipt_inspection.planned_action is not current_planned_action
            or request_inspection.state
            is not OwnerActionLedgerState.CONFIRMATION_REQUIRED
            or not request_inspection.has_pending
        ):
            raise ContractViolation("durable_pending_source_invalid")
        send_inspection = _inspect_owner_action_send_terminal_evidence(
            send_evidence,
            ledger=send_ledger,
            presentation=presentation,
        )
        if (
            send_inspection.composer_request is not composer_request
            or send_inspection.action_outcome is not outcome
            or send_inspection.action_outcome_authority
            is not registration.outcome_authority_ref()
            or composer_request.planned_action is not current_planned_action
        ):
            raise ContractViolation("durable_pending_send_lineage_mismatch")
        if (
            registration.outcome_authority_ref()._inspect_reclaimed_source(
                outcome,
                source,
            )
            != "delivery_terminal"
        ):
            raise ContractViolation("durable_pending_outcome_not_delivered")
        with lock:
            prune_locked()
            existing = tuple(
                record
                for record in pending_records
                if record.authority_ref() is authority
                and record.source is source
                and record.outcome is outcome
            )
            if existing:
                if existing[0].consumed:
                    raise ContractViolation("durable_finalize_ticket_consumed")
                return existing[0].ticket
            active_count = sum(
                record.authority_ref() is authority for record in records
            ) + sum(
                record.authority_ref() is authority for record in abort_records
            ) + sum(
                record.authority_ref() is authority for record in pending_records
            )
            if active_count >= registration.max_dispatches:
                raise ContractViolation("durable_finalize_dispatch_limit_reached")
            ticket = object.__new__(DurableFinalizeTicket)
            object.__setattr__(
                ticket,
                "consumer",
                DurableFinalizeConsumer.PENDING_OUTCOME_RETIRE,
            )
            pending_records = (
                *pending_records,
                _PendingDeliveryRecord(
                    authority_ref=weakref.ref(authority),
                    controller_ref=registration.controller_ref,
                    outcome_authority_ref=registration.outcome_authority_ref,
                    source=source,
                    outcome=outcome,
                    current_planned_action=current_planned_action,
                    composer_request=composer_request,
                    send_evidence=send_evidence,
                    send_ledger_ref=weakref.ref(send_ledger),
                    presentation_ref=weakref.ref(presentation),
                    ticket=ticket,
                    consumed=False,
                ),
            )
            return ticket

    def pending_record_for_ticket_locked(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
    ) -> _PendingDeliveryRecord:
        matches = tuple(
            record
            for record in pending_records
            if record.authority_ref() is authority and record.ticket is ticket
        )
        if len(matches) != 1:
            raise ContractViolation("durable_finalize_ticket_not_canonical")
        record = matches[0]
        if (
            ticket.consumer is not DurableFinalizeConsumer.PENDING_OUTCOME_RETIRE
            or record.consumed
        ):
            raise ContractViolation("durable_finalize_ticket_consumed")
        return record

    def inspect_pending_outcome_ticket(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        *,
        outcome_authority: ActionOutcomeAuthority,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
    ) -> None:
        registration_for(authority)
        with lock:
            prune_locked()
            record = pending_record_for_ticket_locked(authority, ticket)
            if (
                record.outcome_authority_ref() is not outcome_authority
                or record.outcome is not outcome
                or record.current_planned_action is not current_planned_action
            ):
                raise ContractViolation("durable_finalize_ticket_lineage_mismatch")

    def claim_pending_outcome_ticket(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        **kwargs: object,
    ) -> None:
        nonlocal pending_records
        inspect_pending_outcome_ticket(authority, ticket, **kwargs)
        with lock:
            prune_locked()
            record = pending_record_for_ticket_locked(authority, ticket)
            replacement = record._replace(consumed=True)
            pending_records = tuple(
                replacement if item is record else item for item in pending_records
            )

    def remove_completed_pending(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
    ) -> None:
        nonlocal pending_records
        registration_for(authority)
        with lock:
            prune_locked()
            matches = tuple(
                record
                for record in pending_records
                if record.authority_ref() is authority and record.ticket is ticket
            )
            if len(matches) != 1 or not matches[0].consumed:
                raise ContractViolation("durable_finalize_not_complete")
            pending_records = tuple(
                record for record in pending_records if record is not matches[0]
            )

    def ticket_for(
        authority: OwnerActionDurableFinalizeAuthority,
        dispatch: DurableFinalizeDispatch,
        consumer: DurableFinalizeConsumer,
    ) -> DurableFinalizeTicket:
        registration_for(authority)
        if type(dispatch) is not DurableFinalizeDispatch:
            raise ContractViolation("durable_finalize_dispatch_required")
        if (
            type(consumer) is not DurableFinalizeConsumer
            or consumer
            in {
                DurableFinalizeConsumer.PREPARED_ABORT,
                DurableFinalizeConsumer.PENDING_OUTCOME_RETIRE,
            }
        ):
            raise ContractViolation("durable_finalize_consumer_invalid")
        with lock:
            prune_locked()
            record = record_for_dispatch_locked(authority, dispatch)
            ticket = (
                record.controller_ticket
                if consumer is DurableFinalizeConsumer.CONTROLLER_RELEASE
                else record.outcome_ticket
            )
            if type(ticket) is not DurableFinalizeTicket or ticket.consumer is not consumer:
                raise ContractViolation("durable_finalize_ticket_corrupt")
            return ticket

    def inspect_ticket(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        consumer: DurableFinalizeConsumer,
        *,
        controller: OwnerActionController | None = None,
        source: object | None = None,
        outcome_authority: ActionOutcomeAuthority | None = None,
        outcome: ActionOutcomeIntent | None = None,
        current_planned_action: PlannedAction | None = None,
    ) -> _FinalizeRecord:
        registration_for(authority)
        if type(ticket) is not DurableFinalizeTicket or ticket.consumer is not consumer:
            raise ContractViolation("durable_finalize_ticket_not_canonical")
        with lock:
            prune_locked()
            matches = tuple(
                record
                for record in records
                if record.authority_ref() is authority
                and (
                    record.controller_ticket is ticket
                    if consumer is DurableFinalizeConsumer.CONTROLLER_RELEASE
                    else record.outcome_ticket is ticket
                )
            )
            if len(matches) != 1:
                raise ContractViolation("durable_finalize_ticket_not_canonical")
            record = matches[0]
            if consumer in record.consumed:
                raise ContractViolation("durable_finalize_ticket_consumed")
            if (
                (controller is not None and record.controller_ref() is not controller)
                or (source is not None and record.source is not source)
                or (
                    outcome_authority is not None
                    and record.outcome_authority_ref() is not outcome_authority
                )
                or (outcome is not None and record.outcome is not outcome)
                or (
                    current_planned_action is not None
                    and record.current_planned_action is not current_planned_action
                )
            ):
                raise ContractViolation("durable_finalize_ticket_lineage_mismatch")
            return record

    def claim_ticket(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        consumer: DurableFinalizeConsumer,
        **kwargs: object,
    ) -> None:
        nonlocal records
        record = inspect_ticket(authority, ticket, consumer, **kwargs)
        with lock:
            prune_locked()
            if record not in records or consumer in record.consumed:
                raise ContractViolation("durable_finalize_ticket_consumed")
            consumed = record.consumed | {consumer}
            replacement = record._replace(consumed=consumed)
            records = tuple(
                replacement if item is record else item for item in records
            )

    def attach_tombstone(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        *,
        controller: OwnerActionController,
        source: object,
        tombstone: object,
    ) -> None:
        nonlocal records
        record = inspect_ticket(
            authority,
            ticket,
            DurableFinalizeConsumer.CONTROLLER_RELEASE,
            controller=controller,
            source=source,
        )
        controller.inspect_lifecycle_tombstone(tombstone)
        with lock:
            prune_locked()
            if record not in records or record.controller_tombstone is not None:
                raise ContractViolation("durable_finalize_tombstone_corrupt")
            replacement = record._replace(controller_tombstone=tombstone)
            records = tuple(
                replacement if item is record else item for item in records
            )

    def completed_record(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
    ) -> _FinalizeRecord:
        registration_for(authority)
        if type(ticket) is not DurableFinalizeTicket:
            raise ContractViolation("durable_finalize_ticket_not_canonical")
        with lock:
            prune_locked()
            matches = tuple(
                record
                for record in records
                if record.authority_ref() is authority
                and record.outcome_ticket is ticket
            )
            if len(matches) != 1:
                raise ContractViolation("durable_finalize_ticket_not_canonical")
            record = matches[0]
            if (
                record.consumed
                != frozenset(
                    {
                        DurableFinalizeConsumer.CONTROLLER_RELEASE,
                        DurableFinalizeConsumer.OUTCOME_RETIRE,
                    }
                )
                or record.controller_tombstone is None
            ):
                raise ContractViolation("durable_finalize_not_complete")
            return record

    def remove_completed(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
        *,
        controller: OwnerActionController,
        tombstone: object,
    ) -> None:
        nonlocal records
        record = completed_record(authority, ticket)
        if (
            record.controller_ref() is not controller
            or record.controller_tombstone is not tombstone
        ):
            raise ContractViolation("durable_finalize_tombstone_mismatch")
        with lock:
            prune_locked()
            if record not in records:
                raise ContractViolation("durable_finalize_not_complete")
            records = tuple(item for item in records if item is not record)

    def complete(
        authority: OwnerActionDurableFinalizeAuthority,
        ticket: DurableFinalizeTicket,
    ) -> None:
        if ticket.consumer is DurableFinalizeConsumer.PENDING_OUTCOME_RETIRE:
            remove_completed_pending(authority, ticket)
            return
        record = completed_record(authority, ticket)
        record.controller_ref().purge_durable_lifecycle_tombstone(
            record.controller_tombstone,
            ticket=ticket,
            durable_authority=authority,
        )

    def held(store: object, fingerprint: object) -> bool:
        if type(fingerprint) is not str:
            return True
        with lock:
            prune_locked()
            return any(
                record.store is store
                and record.lifecycle_fingerprint == fingerprint
                for record in records
            ) or any(
                record.store is store
                and record.lifecycle_fingerprint == fingerprint
                for record in abort_records
            )

    def ticket_trace(ticket: DurableFinalizeTicket) -> dict[str, str | int | bool]:
        if type(ticket) is not DurableFinalizeTicket:
            raise ContractViolation("durable_finalize_ticket_not_canonical")
        with lock:
            prune_locked()
            matches = tuple(
                (record, consumer)
                for record in records
                for consumer, ref in (
                    (
                        DurableFinalizeConsumer.CONTROLLER_RELEASE,
                        record.controller_ticket,
                    ),
                    (DurableFinalizeConsumer.OUTCOME_RETIRE, record.outcome_ticket),
                )
                if ref is ticket
            )
            if not matches and ticket.consumer is DurableFinalizeConsumer.PREPARED_ABORT:
                abort_matches = tuple(
                    record for record in abort_records if record.ticket is ticket
                )
                if len(abort_matches) != 1:
                    raise ContractViolation("durable_finalize_ticket_not_canonical")
                return {
                    "schema_version": 1,
                    "consumer": ticket.consumer.value,
                    "consumed": abort_matches[0].controller_cleaned,
                    "source_visible": False,
                }
            if (
                not matches
                and ticket.consumer
                is DurableFinalizeConsumer.PENDING_OUTCOME_RETIRE
            ):
                pending_matches = tuple(
                    record for record in pending_records if record.ticket is ticket
                )
                if len(pending_matches) != 1:
                    raise ContractViolation("durable_finalize_ticket_not_canonical")
                return {
                    "schema_version": 1,
                    "consumer": ticket.consumer.value,
                    "consumed": pending_matches[0].consumed,
                    "source_visible": False,
                }
            if len(matches) != 1 or ticket.consumer is not matches[0][1]:
                raise ContractViolation("durable_finalize_ticket_not_canonical")
            return {
                "schema_version": 1,
                "consumer": ticket.consumer.value,
                "consumed": ticket.consumer in matches[0][0].consumed,
                "source_visible": False,
            }

    def dispatch_trace(dispatch: DurableFinalizeDispatch) -> dict[str, int | bool]:
        if type(dispatch) is not DurableFinalizeDispatch:
            raise ContractViolation("durable_finalize_dispatch_not_canonical")
        with lock:
            prune_locked()
            matches = tuple(
                record
                for record in records
                if record.dispatch is dispatch
            )
            if len(matches) != 1:
                raise ContractViolation("durable_finalize_dispatch_not_canonical")
            return {
                "schema_version": 1,
                "ticket_count": 2,
                "consumed_count": len(matches[0].consumed),
                "source_visible": False,
            }

    def authority_trace(
        authority: OwnerActionDurableFinalizeAuthority,
    ) -> dict[str, int | bool]:
        registration = registration_for(authority)
        with lock:
            prune_locked()
            return {
                "schema_version": 1,
                "active_dispatches": sum(
                    record.authority_ref() is authority for record in records
                ) + sum(
                    record.authority_ref() is authority for record in abort_records
                ) + sum(
                    record.authority_ref() is authority for record in pending_records
                ),
                "max_dispatches": registration.max_dispatches,
                "ledger_bounded": True,
                "source_visible": False,
            }

    return (
        issue_authority,
        acknowledge,
        ticket_for,
        inspect_ticket,
        claim_ticket,
        attach_tombstone,
        completed_record,
        remove_completed,
        complete,
        prepare_abort,
        inspect_abort_ticket,
        claim_abort_ticket,
        inspect_completed_abort,
        remove_completed_abort,
        complete_abort,
        acknowledge_pending,
        inspect_pending_outcome_ticket,
        claim_pending_outcome_ticket,
        held,
        ticket_trace,
        dispatch_trace,
        authority_trace,
    )


(
    _issue_durable_authority,
    _acknowledge_exact_delivery,
    _ticket_for_consumer,
    _inspect_finalize_ticket,
    _claim_finalize_ticket,
    _attach_finalize_tombstone,
    _inspect_completed_finalize,
    _remove_completed_finalize,
    _complete_finalize_dispatch,
    _prepare_durable_abort,
    _inspect_prepared_abort_ticket,
    _claim_prepared_abort_ticket,
    _inspect_completed_prepared_abort,
    _remove_completed_prepared_abort,
    _complete_durable_abort,
    _acknowledge_pending_delivery,
    _inspect_pending_outcome_finalize_ticket,
    _claim_pending_outcome_finalize_ticket,
    _lifecycle_record_is_held,
    _durable_ticket_trace,
    _durable_dispatch_trace,
    _durable_authority_trace,
) = _build_durable_finalize_vault()
del _build_durable_finalize_vault


def _inspect_controller_finalize_ticket(
    authority: OwnerActionDurableFinalizeAuthority,
    ticket: DurableFinalizeTicket,
    *,
    controller: OwnerActionController,
    source: object,
) -> None:
    _inspect_finalize_ticket(
        authority,
        ticket,
        DurableFinalizeConsumer.CONTROLLER_RELEASE,
        controller=controller,
        source=source,
    )


def _claim_controller_finalize_ticket(
    authority: OwnerActionDurableFinalizeAuthority,
    ticket: DurableFinalizeTicket,
    *,
    controller: OwnerActionController,
    source: object,
    tombstone: object,
) -> None:
    _attach_finalize_tombstone(
        authority,
        ticket,
        controller=controller,
        source=source,
        tombstone=tombstone,
    )
    _claim_finalize_ticket(
        authority,
        ticket,
        DurableFinalizeConsumer.CONTROLLER_RELEASE,
        controller=controller,
        source=source,
    )


def _inspect_completed_controller_finalize(
    authority: OwnerActionDurableFinalizeAuthority,
    ticket: DurableFinalizeTicket,
    *,
    controller: OwnerActionController,
    tombstone: object,
) -> None:
    record = _inspect_completed_finalize(authority, ticket)
    if (
        record.controller_ref() is not controller
        or record.controller_tombstone is not tombstone
    ):
        raise ContractViolation("durable_finalize_tombstone_mismatch")


def _claim_completed_controller_finalize(
    authority: OwnerActionDurableFinalizeAuthority,
    ticket: DurableFinalizeTicket,
    *,
    controller: OwnerActionController,
    tombstone: object,
) -> None:
    _remove_completed_finalize(
        authority,
        ticket,
        controller=controller,
        tombstone=tombstone,
    )


def _inspect_outcome_finalize_ticket(
    authority: OwnerActionDurableFinalizeAuthority,
    ticket: DurableFinalizeTicket,
    *,
    outcome_authority: ActionOutcomeAuthority,
    outcome: ActionOutcomeIntent,
    current_planned_action: PlannedAction,
) -> None:
    if ticket.consumer is DurableFinalizeConsumer.PENDING_OUTCOME_RETIRE:
        _inspect_pending_outcome_finalize_ticket(
            authority,
            ticket,
            outcome_authority=outcome_authority,
            outcome=outcome,
            current_planned_action=current_planned_action,
        )
        return
    _inspect_finalize_ticket(
        authority,
        ticket,
        DurableFinalizeConsumer.OUTCOME_RETIRE,
        outcome_authority=outcome_authority,
        outcome=outcome,
        current_planned_action=current_planned_action,
    )


def _claim_outcome_finalize_ticket(
    authority: OwnerActionDurableFinalizeAuthority,
    ticket: DurableFinalizeTicket,
    *,
    outcome_authority: ActionOutcomeAuthority,
    outcome: ActionOutcomeIntent,
    current_planned_action: PlannedAction,
) -> None:
    if ticket.consumer is DurableFinalizeConsumer.PENDING_OUTCOME_RETIRE:
        _claim_pending_outcome_finalize_ticket(
            authority,
            ticket,
            outcome_authority=outcome_authority,
            outcome=outcome,
            current_planned_action=current_planned_action,
        )
        return
    _claim_finalize_ticket(
        authority,
        ticket,
        DurableFinalizeConsumer.OUTCOME_RETIRE,
        outcome_authority=outcome_authority,
        outcome=outcome,
        current_planned_action=current_planned_action,
    )


__all__ = [
    "DurableFinalizeConsumer",
    "DurableFinalizeDispatch",
    "DurableFinalizeTicket",
    "OwnerActionDurableFinalizeAuthority",
]
