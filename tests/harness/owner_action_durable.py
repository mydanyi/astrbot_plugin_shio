from __future__ import annotations

import tempfile
from typing import NamedTuple

from astrbot_plugin_shio.core.action_outcome import (
    ActionOutcomeAuthority,
    ActionOutcomeIntent,
)
from astrbot_plugin_shio.core.action_planner import PlannedAction
from astrbot_plugin_shio.core.owner_action_controller import OwnerActionController
from astrbot_plugin_shio.core.owner_action_durable_finalize import (
    DurableFinalizeConsumer,
    DurableFinalizeTicket,
    OwnerActionDurableFinalizeAuthority,
    _terminal_projection,
)
from astrbot_plugin_shio.core.owner_action_lifecycle import (
    LifecyclePhase,
    OwnerActionLifecycleStore,
)


class DurableTicketPair(NamedTuple):
    controller: DurableFinalizeTicket
    outcome: DurableFinalizeTicket


class DurableFinalizeHarness:
    """Test-only owner graph that exercises the same public durable path."""

    def __init__(
        self,
        controller: OwnerActionController,
        outcome_authority: ActionOutcomeAuthority,
        *,
        max_records: int = 2048,
        max_dispatches: int = 2048,
    ) -> None:
        self.controller = controller
        self.outcome_authority = outcome_authority
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = OwnerActionLifecycleStore(
            self.tempdir.name,
            install_secret=b"owner-action-durable-test-secret!!",
            recovery_now=1.0,
            max_records=max_records,
            tombstone_ttl_seconds=900.0,
        )
        self.authority = OwnerActionDurableFinalizeAuthority.issue_for_runtime(
            self.store,
            controller,
            outcome_authority,
            max_dispatches=max_dispatches,
        )

    def close(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def abandoned_tickets(
        self,
        *,
        source: object,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        now: float,
    ) -> DurableTicketPair:
        projection = _terminal_projection(
            self.controller,
            source,
            current_planned_action,
        )
        reservation = self.store.reserve(
            projection.operation,
            request_digest=projection.idempotency_material,
            now=now,
        )
        if projection.attempted:
            self.store.mark_in_progress(reservation.handle, now=now + 0.1)
        terminal = self.store.mark_terminal(
            reservation.handle,
            status=projection.status,
            effect_state=projection.effect_state,
            attempted=projection.attempted,
            now=now + 0.2,
        )
        assert terminal.phase is LifecyclePhase.TERMINAL
        dispatch = self.authority.acknowledge_abandoned(
            lifecycle_handle=reservation.handle,
            source=source,
            outcome=outcome,
            current_planned_action=current_planned_action,
            now=now + 0.3,
        )
        return DurableTicketPair(
            self.authority.ticket_for(
                dispatch,
                DurableFinalizeConsumer.CONTROLLER_RELEASE,
            ),
            self.authority.ticket_for(
                dispatch,
                DurableFinalizeConsumer.OUTCOME_RETIRE,
            ),
        )

    def prepared_abort_ticket(
        self,
        request: object,
        *,
        now: float,
    ) -> tuple[object, DurableFinalizeTicket]:
        reservation = self.store.reserve(
            request.operation,
            request_digest=request.idempotency_key,
            now=now,
        )
        ticket = self.authority.prepare_abort(
            lifecycle_handle=reservation.handle,
            request=request,
            now=now + 0.1,
        )
        return reservation.handle, ticket

    def finalize_outcome(
        self,
        *,
        outcome: ActionOutcomeIntent,
        current_planned_action: PlannedAction,
        ticket: DurableFinalizeTicket,
    ) -> None:
        self.outcome_authority.finalize_reclaimed_outcome(
            outcome,
            current_planned_action,
            ticket=ticket,
            durable_authority=self.authority,
        )
