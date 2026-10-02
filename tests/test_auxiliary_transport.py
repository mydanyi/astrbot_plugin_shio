"""Catch transport-selection regressions at Shio's real bounded call entry."""
import asyncio
from types import SimpleNamespace
import unittest

from test_name_semantic_provider_routing import MAIN
from test_r14_reviewer_regressions import _Event
from test_r14_reviewer_regressions import R14LifecycleAndPresentationTests, _AuxiliaryEvent
from test_name_semantic_provider_routing import FakeContext


class Provider:
    """External provider fixture follows AstrBot's chunks + final contract."""
    def __init__(self, provider_id="stream-model", *, complete=True, hang=False):
        self.provider_config = {"id": provider_id}
        self.complete = complete
        self.hang = hang
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def text_chat(self, *, prompt, func_tool):
        assert func_tool is None
        return SimpleNamespace(completion_text="non-stream result")

    async def text_chat_stream(self, *, prompt, func_tool):
        assert func_tool is None
        try:
            self.started.set()
            yield SimpleNamespace(is_chunk=True, completion_text="", reasoning_content="private thought")
            yield SimpleNamespace(is_chunk=True, completion_text='{"decision":')
            if self.hang:
                await asyncio.Event().wait()
            yield SimpleNamespace(is_chunk=True, completion_text='"REPLY"}')
            if self.complete:
                yield SimpleNamespace(is_chunk=False, completion_text='{"decision":"REPLY"}', role="assistant")
        finally:
            self.closed.set()


class AuxiliaryTransportTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, ids=None):
        p = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        p.config = {"sys001": {"model_transport": {"stream_provider_ids": ids or []}}}
        p._auxiliary_epoch = 1
        p._auxiliary_terminated = False
        p._natural_generations = {}
        p._natural_candidates = {}
        return p

    async def call(self, p, provider, timeout=1):
        event = _Event()
        return await p._auxiliary_text_chat(event, p._bind_auxiliary_call(event), provider, prompt="fixture", timeout=timeout)

    async def test_default_keeps_non_stream_provider_with_no_stream_method(self):
        provider = SimpleNamespace(text_chat=Provider().text_chat)
        self.assertEqual((await self.call(self.plugin(), provider)).completion_text, "non-stream result")

    async def test_selected_stream_returns_final_once_without_thought_or_duplicate(self):
        provider = Provider()
        result = await self.call(self.plugin(["stream-model"]), provider)
        self.assertEqual(result.completion_text, '{"decision":"REPLY"}')
        self.assertTrue(provider.closed.is_set())

    async def test_unselected_fallback_model_keeps_non_stream(self):
        result = await self.call(self.plugin(["stream-model"]), Provider("fallback"))
        self.assertEqual(result.completion_text, "non-stream result")

    async def test_selected_stream_only_provider_never_requires_text_chat(self):
        provider = Provider()
        provider.text_chat = None
        result = await self.call(self.plugin(["stream-model"]), provider)
        self.assertIsNotNone(result)
        self.assertEqual(result.completion_text, '{"decision":"REPLY"}')

    async def test_stream_without_final_response_is_not_accepted(self):
        self.assertIsNone(await self.call(self.plugin(["stream-model"]), Provider(complete=False)))

    async def test_stream_total_deadline_discards_partial_and_closes_generator(self):
        provider = Provider(hang=True)
        result = await self.call(self.plugin(["stream-model"]), provider, timeout=0.02)
        self.assertIsNone(result)
        await asyncio.wait_for(provider.closed.wait(), 1)

    async def test_caller_cancel_closes_stream(self):
        provider = Provider(hang=True)
        task = asyncio.create_task(self.call(self.plugin(["stream-model"]), provider))
        try:
            await asyncio.wait_for(provider.started.wait(), 0.5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(provider.closed.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_shutdown_discards_stream_final(self):
        p = self.plugin(["stream-model"])
        provider = Provider()
        original = provider.text_chat_stream
        async def stream(**kwargs):
            async for chunk in original(**kwargs):
                if not chunk.is_chunk:
                    p._auxiliary_epoch += 1
                yield chunk
        provider.text_chat_stream = stream
        self.assertIsNone(await self.call(p, provider))

    async def test_mixed_fallback_uses_each_providers_own_mode(self):
        for stream_primary in (False, True):
            with self.subTest(stream_primary=stream_primary):
                primary, fallback = Provider("primary"), Provider("fallback")
                async def invalid_non_stream(**kwargs):
                    return SimpleNamespace(completion_text="invalid")
                async def valid_non_stream(**kwargs):
                    return SimpleNamespace(completion_text='{"decision":"DIRECT"}')
                async def invalid_stream(**kwargs):
                    yield SimpleNamespace(is_chunk=False, role="assistant", completion_text="invalid")
                async def valid_stream(**kwargs):
                    yield SimpleNamespace(is_chunk=True, completion_text='{"decision":')
                    yield SimpleNamespace(is_chunk=False, role="assistant", completion_text='{"decision":"DIRECT"}')
                primary.text_chat = invalid_non_stream
                primary.text_chat_stream = invalid_stream
                # Wrong transport on the successful fallback cannot pass.
                fallback.text_chat = invalid_non_stream if not stream_primary else valid_non_stream
                fallback.text_chat_stream = invalid_stream if stream_primary else valid_stream
                p = R14LifecycleAndPresentationTests()._auxiliary_plugin(primary, group={
                    "name_wake_mode": "semantic", "name_semantic_provider_id": "primary",
                    "name_semantic_fallback_provider_ids": ["fallback"], "name_semantic_timeout_seconds": 1,
                })
                p.config["sys001"]["model_transport"] = {"stream_provider_ids": ["primary" if stream_primary else "fallback"]}
                p.context = FakeContext(current=None, by_id={"primary": primary, "fallback": fallback})
                self.assertTrue(await p._visible_name_wake(_AuxiliaryEvent("Shio, hello")))

    async def test_stream_error_response_is_not_adopted(self):
        provider = Provider()
        async def error_stream(**kwargs):
            yield SimpleNamespace(is_chunk=False, role="err", completion_text='{"decision":"REPLY"}')
        provider.text_chat_stream = error_stream
        self.assertIsNone(await self.call(self.plugin(["stream-model"]), provider))
