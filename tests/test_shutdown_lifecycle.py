"""Termination lock/cancellation behavior, using the existing plugin loader."""
import asyncio

import pytest

from test_name_semantic_provider_routing import MAIN


def plugin():
    instance = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
    instance.config = {"sys001": {"lifecycle": {"shutdown_grace_seconds": 1}}}
    instance._reply_drain = MAIN.ReplyDrain()
    return instance


@pytest.mark.asyncio
async def test_concurrent_termination_waits_for_same_reply_and_closes_once():
    instance = plugin()
    release = asyncio.Event()
    sent = []

    async def reply():
        instance._reply_drain.track(object())
        await release.wait()
        sent.append("reply")

    worker = asyncio.create_task(reply())
    await asyncio.sleep(0)
    first = asyncio.create_task(instance.terminate())
    second = asyncio.create_task(instance.terminate())
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second, worker), 1)
    assert sent == ["reply"]
    assert instance._shutdown_complete
    assert instance._auxiliary_epoch == 1
    assert not instance._reply_drain.track(object())


@pytest.mark.asyncio
async def test_cancelled_termination_still_closes_and_cancels_waiting_reply():
    instance = plugin()
    waiting = asyncio.Event()

    async def reply():
        instance._reply_drain.track(object())
        await waiting.wait()

    worker = asyncio.create_task(reply())
    await asyncio.sleep(0)
    stopping = asyncio.create_task(instance.terminate())
    await asyncio.sleep(0)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    await asyncio.gather(worker, return_exceptions=True)
    assert worker.cancelled()
    assert instance._shutdown_complete
    assert instance._master_alert_terminated
    await instance.terminate()
    assert instance._auxiliary_epoch == 1


@pytest.mark.parametrize("value, expected", [(None, 60), (True, 60), (float("nan"), 60), (0, 1), (500, 120), (30, 30)])
def test_shutdown_budget_uses_documented_defaults_and_limits(value, expected):
    instance = plugin()
    instance.config["sys001"]["lifecycle"]["shutdown_grace_seconds"] = value
    assert instance._shutdown_grace_seconds() == expected
