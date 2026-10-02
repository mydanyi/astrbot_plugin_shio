"""Shutdown must await real work, never renew its deadline or await itself."""
import asyncio

import pytest

from astrbot_plugin_shio.core.reply_drain import ReplyDrain


@pytest.mark.asyncio
async def test_reply_finishes_before_wait_returns():
    drain = ReplyDrain()
    sent = []
    release = asyncio.Event()
    async def reply():
        assert drain.track(object())
        await release.wait()
        sent.append('reply')
    worker = asyncio.create_task(reply())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(drain.wait(.5))
    await asyncio.sleep(0)
    assert not waiter.done()
    release.set()
    assert await waiter == ()
    assert sent == ['reply']
    assert not drain.track(object())
    await worker


@pytest.mark.asyncio
async def test_new_arrivals_during_drain_also_finish_without_resetting_budget():
    drain = ReplyDrain()
    first = asyncio.Event()
    second = asyncio.Event()
    async def reply(gate):
        assert drain.track(object())
        await gate.wait()
    one = asyncio.create_task(reply(first))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(drain.wait(.08))
    await asyncio.sleep(.02)
    two = asyncio.create_task(reply(second))
    await asyncio.sleep(0)
    first.set()
    await one
    await asyncio.sleep(0)
    assert not waiter.done()
    pending = await asyncio.wait_for(waiter, .1)
    assert len(pending) == 1
    assert not two.done()
    drain.abort()
    await asyncio.gather(two, return_exceptions=True)
    assert two.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['error', 'cancel'])
async def test_failed_or_cancelled_task_does_not_hold_shutdown(outcome):
    drain = ReplyDrain()
    gate = asyncio.Event()
    async def reply():
        drain.track(object())
        await gate.wait()
        raise ValueError('fixture')
    worker = asyncio.create_task(reply())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(drain.wait(.5))
    if outcome == 'cancel': worker.cancel()
    else: gate.set()
    await asyncio.gather(worker, return_exceptions=True)
    assert await waiter == ()


@pytest.mark.asyncio
async def test_termination_never_waits_for_its_own_calling_task():
    drain = ReplyDrain()
    drain.track(object())
    assert await drain.wait(.02) == ()
    drain.abort()
    assert not asyncio.current_task().cancelling()
