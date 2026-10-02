"""Red tests: a finished Pipeline Task must retire its same-scope merge slot.

Replays AstrBot 4.27.4 public entry points exactly like
``test_bug001_boundary_pipeline``: the real Shio handlers are registered in the
public registry, Waking/PreProcess/Process and RespondStage run unmodified, and
every event is driven in its own ``asyncio.Task`` the way EventBus does.  No
case calls Shio terminal helpers, wakeups, ``record_standard_send`` or
``terminate`` to make itself pass.

Pre-fix expectation: the "next same-scope request proceeds" assertions time out,
because a Pipeline whose Task already ended without a send / terminal hook keeps
owning the merge position and strands later same-scope events.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

try:  # Reuse the collected sibling module; fall back to the package path.
    import test_bug001_boundary_pipeline as _base
except ImportError:  # pragma: no cover - depends on the collected test root
    import astrbot_plugin_shio.tests.test_bug001_boundary_pipeline as _base

from astrbot.core.message.components import At, Plain, Reply

_official_fixture = _base._official_fixture
_run_official_pipeline = _base._run_official_pipeline
_master_text = _base._master_text
_master_media = _base._master_media
_event = _base._event
_expect_provider = _base._expect_provider
_expect_waiting = _base._expect_waiting
_cancel_fixture_task = _base._cancel_fixture_task


def _spawn(event, pipeline_context, *, preprocess: bool = False) -> asyncio.Task:
    """EventBus shape: one Pipeline Task per event, never the whole test caller."""
    return asyncio.create_task(
        _run_official_pipeline(event, pipeline_context, preprocess=preprocess)
    )


def _install_gated_agent(monkeypatch):
    """Park the fixture's leaf agent on an external gate the test controls.

    ``_run_official_pipeline`` builds its leaf from the module-level
    ``_NoOpAgentSubStage`` symbol, so swapping that symbol keeps the official
    PreProcess/Waking/Process entry points untouched while letting the test hold
    a real, still-running Pipeline Task open.
    """
    entered: asyncio.Event = asyncio.Event()
    gate: asyncio.Event = asyncio.Event()

    class _GatedAgentSubStage:
        async def process(self, event):
            entered.set()
            await gate.wait()
            if False:  # keeps this an async generator without Provider I/O
                yield event

    monkeypatch.setattr(_base, "_NoOpAgentSubStage", _GatedAgentSubStage)
    return entered, gate


@pytest.mark.asyncio
async def test_finished_text_pipeline_without_send_releases_same_scope():
    """A text Pipeline that already ended without a send must not block the next."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        boundary = _master_text("retire-text-boundary")
        boundary_task = _spawn(boundary, pipeline_context)
        await asyncio.wait_for(boundary_task, timeout=3)
        assert boundary.sent == []

        follower = _master_text("retire-text-follower")
        follower_task = _spawn(follower, pipeline_context)
        try:
            await _expect_provider(follower_task, follower)
        finally:
            await _cancel_fixture_task(follower_task)


@pytest.mark.asyncio
async def test_finished_media_pipeline_without_send_releases_same_scope():
    """A media Pipeline that already ended without a send must not block the next."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        boundary = _master_media("retire-media-boundary")
        boundary_task = _spawn(boundary, pipeline_context, preprocess=True)
        await asyncio.wait_for(boundary_task, timeout=3)
        assert boundary.sent == []

        follower = _master_text("retire-media-follower")
        follower_task = _spawn(follower, pipeline_context)
        try:
            await _expect_provider(follower_task, follower)
        finally:
            await _cancel_fixture_task(follower_task)


@pytest.mark.asyncio
async def test_finished_reply_pipeline_without_terminal_releases_same_scope():
    """A completed non-waking Reply must not own a boundary either."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        reply = _event(
            "retire-reply-boundary",
            "member",
            "",
            [Reply(id="prior", sender_id="other")],
        )
        boundary_task = _spawn(reply, pipeline_context)
        await asyncio.wait_for(boundary_task, timeout=3)
        assert reply.get_extra("provider_request") is None
        assert reply.sent == []

        follower = _master_text("retire-reply-follower")
        follower_task = _spawn(follower, pipeline_context)
        try:
            await _expect_provider(follower_task, follower)
        finally:
            await _cancel_fixture_task(follower_task)


@pytest.mark.parametrize(
    "make_boundary,preprocess",
    [
        pytest.param(_master_text, False, id="text"),
        pytest.param(_master_media, True, id="directed-media"),
    ],
)
@pytest.mark.asyncio
async def test_running_agent_task_blocks_follower_until_task_ends(
    monkeypatch, make_boundary, preprocess
):
    """A genuinely unfinished Pipeline must still block, then release on end.

    Run over both plain text and the official default directed media Agent, so a
    handler that wrongly released the merge slot on handler return (before the
    real Agent Task ended) is caught for the default media path too.
    """
    entered, gate = _install_gated_agent(monkeypatch)
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        boundary = make_boundary("gated-boundary")
        boundary_task = _spawn(boundary, pipeline_context, preprocess=preprocess)
        follower = _master_text("gated-follower")
        follower_task = None
        try:
            # Let the boundary Task own the gate first: starting both
            # concurrently merges the same-sender events and the boundary may
            # retire instead of holding the merge position.
            await asyncio.wait_for(entered.wait(), timeout=3)
            follower_task = _spawn(follower, pipeline_context)
            assert not boundary_task.done()
            await _expect_waiting(follower_task)
            assert follower.get_extra("provider_request") is None

            # The boundary Task is still genuinely inside the Agent gate, so the
            # follower must stay blocked and must not have reached Provider.
            assert not boundary_task.done()
            assert follower.get_extra("provider_request") is None

            gate.set()  # release the controlled external Agent gate
            await asyncio.wait_for(boundary_task, timeout=3)
            await _expect_provider(follower_task, follower)
        finally:
            gate.set()
            await _cancel_fixture_task(boundary_task)
            if follower_task is not None:
                await _cancel_fixture_task(follower_task)


@pytest.mark.asyncio
async def test_cancelled_unfinished_task_releases_same_scope_follower(monkeypatch):
    """Cancelling an unfinished Task must not strand a same-scope follower."""
    entered, gate = _install_gated_agent(monkeypatch)
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        boundary = _master_text("cancelled-boundary")
        boundary_task = _spawn(boundary, pipeline_context)
        follower = _master_text("cancelled-follower")
        follower_task = None
        try:
            # Boundary owns the gate before the follower starts, so the follower
            # really waits on the merge position and not on a merged event.
            await asyncio.wait_for(entered.wait(), timeout=3)
            follower_task = _spawn(follower, pipeline_context)
            await _expect_waiting(follower_task)

            boundary_task.cancel()
            with suppress(asyncio.CancelledError):
                await boundary_task
            assert boundary_task.done()
            # The follower reaches this same controlled Agent gate once the
            # cancelled owner retires; release it before awaiting completion, or
            # the test's own fake gate stalls the follower and blames production.
            gate.set()
            await _expect_provider(follower_task, follower)
        finally:
            gate.set()
            await _cancel_fixture_task(boundary_task)
            if follower_task is not None:
                await _cancel_fixture_task(follower_task)


@pytest.mark.asyncio
async def test_cancelled_queued_waiter_does_not_block_later_follower(monkeypatch):
    """Cancelling a queued waiter must not strand a later, distinct follower."""
    entered, gate = _install_gated_agent(monkeypatch)
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        owner = _master_text("queued-owner")
        owner_task = _spawn(owner, pipeline_context)
        waiter_task = None
        follower_task = None
        try:
            # The owner Task owns the gate first, so both later events queue on
            # the merge position instead of merging into a running event.
            await asyncio.wait_for(entered.wait(), timeout=3)

            waiter = _master_text("queued-waiter")
            waiter_task = _spawn(waiter, pipeline_context)

            # A surviving follower from a DIFFERENT sender, a native
            # At(bot)+Plain directed text, builds an independent waiter.
            follower = _event(
                "queued-follower",
                "member",
                "member follow-up",
                [At(qq=owner.get_self_id()), Plain(text="member follow-up")],
            )
            follower_task = _spawn(follower, pipeline_context)

            # Both queue as real waiters behind the gated owner.
            await _expect_waiting(waiter_task)
            await _expect_waiting(follower_task)

            # Drop the first waiter; its reservation must not block the other.
            waiter_task.cancel()
            with suppress(asyncio.CancelledError):
                await waiter_task
            assert waiter_task.done()

            gate.set()  # release the owner so the surviving follower proceeds
            await _expect_provider(follower_task, follower)
        finally:
            gate.set()
            await _cancel_fixture_task(owner_task)
            if waiter_task is not None:
                await _cancel_fixture_task(waiter_task)
            if follower_task is not None:
                await _cancel_fixture_task(follower_task)
