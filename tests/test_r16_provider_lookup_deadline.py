"""R16: public Provider lookup is inside every auxiliary purpose deadline."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

try:
    from test_name_semantic_provider_routing import MAIN, FakeEvent
    import test_r13_master_termination_regressions as R13
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import MAIN, FakeEvent
    from astrbot_plugin_shio.tests import test_r13_master_termination_regressions as R13


class _Event(FakeEvent):
    def __init__(self, text="Shio, classify"):
        super().__init__(text)
        self._extras: dict[str, object] = {}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class _Provider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []
        self.started = asyncio.Event()

    async def text_chat(self, *, prompt, func_tool):
        self.calls.append({"prompt": prompt, "func_tool": func_tool})
        self.started.set()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, tuple) and outcome[0] == "delay":
            await asyncio.sleep(outcome[1])
            outcome = outcome[2]
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(completion_text=outcome)


class _RecordedPlain:
    """Minimal recorded Plain shape used by the fixed LLMResponse contract."""

    def __init__(self, text):
        self.text = text


class _NonTextResultSentinel:
    """A non-text result-chain component that completion_text must not erase."""

    def __init__(self, name):
        self.name = name


class _RecordedMessageChain:
    """The fixed public ``MessageChain.chain/get_plain_text`` surface."""

    def __init__(self, chain):
        self.chain = chain

    def get_plain_text(self):
        return " ".join(
            component.text
            for component in self.chain
            if isinstance(component, _RecordedPlain)
        )


class _RecordedLLMResponse:
    """Faithful, independent replay of the fixed result-chain text property.

    Recorded from AstrBot 4.27.4 commit
    ``d2d7e5aefd2f9471d211974e7e193f321e0ebe0a``,
    ``astrbot/core/provider/entities.py`` blob
    ``2fab40ca783634ccef3d2a372e29cd1794f64be7``.  The fixed
    ``LLMResponse`` exposes visible text through
    ``result_chain.get_plain_text()``.  Fixed
    ``astrbot/core/message/message_event_result.py`` blob
    ``72dc481a2389675f0bb6cb9821870d8ccc762073`` joins Plain component text
    with one literal space. Its setter removes every Plain from the
    existing chain and inserts one new Plain at index zero, preserving only
    non-Plain objects. This independently recorded destructive write means a
    same-text mutation still changes Plain identity and placement.
    """

    def __init__(self, result_chain, *, role="assistant", reasoning_content=""):
        self.result_chain = result_chain
        self.role = role
        self.reasoning_content = reasoning_content
        self.setter_writes = 0

    @property
    def completion_text(self):
        if self.result_chain:
            return self.result_chain.get_plain_text()
        return self._completion_text

    @completion_text.setter
    def completion_text(self, value):
        self.setter_writes += 1
        if self.result_chain:
            self.result_chain.chain = [
                component for component in self.result_chain.chain
                if not isinstance(component, _RecordedPlain)
            ]
            self.result_chain.chain.insert(0, _RecordedPlain(value))
        else:
            self._completion_text = value


class _LookupContext:
    def __init__(self, provider=None, *, by_id=None, errors=(), cancelled=()) -> None:
        self.provider = provider
        self.by_id = dict(by_id or {})
        self.errors = set(errors)
        self.cancelled = set(cancelled)
        self.lookup_started = asyncio.Event()
        self.lookup_release = asyncio.Event()
        self.lookup_cancel_seen = asyncio.Event()
        self.current_calls = 0
        self.by_id_calls: list[str] = []
        self.block_current = False
        self.late_exception: BaseException | None = None
        self.conversation_manager = self

    def get_config(self, *, umo):
        return {"wake_prefix": ["Shio"], "provider_ltm_settings": {}}

    async def get_using_provider_async(self, _umo):
        self.current_calls += 1
        if not self.block_current:
            return self.provider
        self.lookup_started.set()
        while not self.lookup_release.is_set():
            try:
                await self.lookup_release.wait()
            except asyncio.CancelledError:
                self.lookup_cancel_seen.set()
        if self.late_exception is not None:
            raise self.late_exception
        return self.provider

    def get_provider_by_id(self, provider_id):
        self.by_id_calls.append(provider_id)
        if provider_id in self.cancelled:
            raise asyncio.CancelledError()
        if provider_id in self.errors:
            raise RuntimeError("recorded public lookup failure")
        return self.by_id.get(provider_id)


class R16ProviderLookupDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, context, *, group=None, review=None):
        plugin = R13.R13MasterTerminationTests().plugin(
            R13.MasterKVSpy()
        )
        plugin.context = context
        plugin.config = {"sys001": {"group": group or {}, "final_review": review or {}}}
        # The test fixture's successful/released lookup budget; production
        # remains at 8s and dedicated timeout tests override explicitly.
        plugin._NATURAL_KV_AWAIT_SECONDS = 1.0
        return plugin

    async def _must_finish_before_release(self, context, awaitable):
        task = asyncio.create_task(awaitable)
        await asyncio.wait_for(context.lookup_started.wait(), timeout=2.0)
        done, pending = await asyncio.wait({task}, timeout=1.0)
        if pending:
            context.lookup_release.set()
            await asyncio.wait_for(task, timeout=2.0)
            self.fail("caller waited for a cancellation-resistant public lookup")
        return task.result()

    async def _release_tracked_lookup(self, plugin, context):
        context.lookup_release.set()
        tasks = tuple(getattr(plugin, "_auxiliary_tasks", set()))
        if tasks:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=2.0
            )
        await asyncio.sleep(0)
        self.assertEqual(set(), getattr(plugin, "_auxiliary_tasks", set()))

    async def test_name_current_lookup_consumes_the_single_budget_without_chat(self):
        provider = _Provider(['{"decision":"DIRECT"}'])
        fallback = _Provider(['{"decision":"DIRECT"}'])
        context = _LookupContext(provider, by_id={"fallback": fallback})
        context.block_current = True
        plugin = self._plugin(context, group={
            "name_wake_mode": "semantic", "name_semantic_timeout_seconds": 0.01,
            "name_semantic_fallback_provider_ids": ["fallback"],
        })
        event = _Event()
        try:
            self.assertFalse(await self._must_finish_before_release(context, plugin._visible_name_wake(event)))
            self.assertEqual([], provider.calls)
            self.assertEqual([], fallback.calls)
            self.assertEqual([], context.by_id_calls)
            self.assertTrue(context.lookup_cancel_seen.is_set())
            self.assertTrue(plugin._auxiliary_tasks)
        finally:
            await self._release_tracked_lookup(plugin, context)
        self.assertFalse(event.get_extra("shio.sys001.requested", False))

    async def test_natural_lookup_timeout_is_bounded_through_terminate_and_late_result(self):
        provider = _Provider(['{"decision":"REPLY"}'])
        fallback = _Provider(['{"decision":"REPLY"}'])
        context = _LookupContext(provider, by_id={"fallback": fallback})
        context.block_current = True
        plugin = self._plugin(context, group={
            "natural_decision_provider_id": "",
            "natural_decision_fallback_provider_ids": ["fallback"],
            "natural_decision_timeout_seconds": 0.01,
            "natural_participation_prompt": "be conservative",
        })
        event = _Event("ordinary group message")
        snapshot = MAIN.create_snapshot(event, origin="natural")
        try:
            self.assertEqual("", await self._must_finish_before_release(
                context, plugin._decide_natural_participation(event, snapshot)
            ))
            await asyncio.wait_for(plugin.terminate(), timeout=2.0)
            self.assertTrue(plugin._auxiliary_terminated)
            self.assertNotIn("shio.sys001.generation", event._extras)
            self.assertEqual([], provider.calls)
            self.assertEqual([], fallback.calls)
            self.assertEqual([], context.by_id_calls)
        finally:
            await self._release_tracked_lookup(plugin, context)

    async def test_review_repair_uses_remaining_budget_after_lookup(self):
        provider = _Provider([
            '{"action":"replace","text":"repaired"}',
            ("delay", 2.0, '{"action":"keep"}'),
        ])
        context = _LookupContext(provider)
        original_lookup = context.get_using_provider_async

        async def delayed_lookup(umo):
            await asyncio.sleep(0.02)
            return await original_lookup(umo)

        context.get_using_provider_async = delayed_lookup
        plugin = self._plugin(context, review={
            # Mixed success-to-timeout contract: the first lookup/repair has
            # a stable real budget; the second recorded response is explicitly
            # beyond that same absolute budget, so exhaustion is deterministic.
            "mode": "core", "timeout_seconds": 1.0, "max_repair_attempts": 1,
            "repair_enabled": True, "use_repaired_text": True,
            "provider_id": "", "fallback_provider_ids": [],
        })
        event = _Event("review")
        outcome = await asyncio.wait_for(
            plugin._review_final_text(event, MAIN.create_snapshot(event, origin="private"), "original"),
            timeout=3.0,
        )
        self.assertEqual("repaired", outcome.text)
        self.assertTrue(outcome.exhausted)
        self.assertEqual(2, len(provider.calls))

    async def test_model_lookup_timeout_keeps_original_components_and_consumes_late_error(self):
        provider = _Provider(['{"segments":["alpha","beta"]}'])
        fallback = _Provider(['{"segments":["alpha","beta"]}'])
        context = _LookupContext(provider, by_id={"fallback": fallback})
        context.block_current = True
        context.late_exception = RuntimeError("late lookup")
        plugin = self._plugin(context)
        event = _Event("layout")
        presentation = {
            "model_segment_provider_id": "", "model_segment_fallback_provider_ids": ["fallback"],
            "model_segment_timeout_seconds": 0.01,
            "text_component_min_segments": 1, "text_component_max_segments": 3,
        }
        try:
            self.assertEqual(["alphabeta"], await self._must_finish_before_release(
                context, plugin._model_text_components(event, "alphabeta", presentation)
            ))
            self.assertEqual([], provider.calls)
            self.assertEqual([], fallback.calls)
            self.assertEqual([], context.by_id_calls)
        finally:
            await self._release_tracked_lookup(plugin, context)

    async def test_explicit_lookup_order_errors_and_cancellation_follow_public_contract(self):
        good = _Provider(['{"decision":"DIRECT"}'])
        context = _LookupContext(None, by_id={"good": good}, errors={"bad"})
        plugin = self._plugin(context, group={
            "name_wake_mode": "semantic", "name_semantic_timeout_seconds": 1.0,
            "name_semantic_provider_id": "bad",
            "name_semantic_fallback_provider_ids": ["good"],
        })
        self.assertTrue(await plugin._visible_name_wake(_Event()))
        self.assertEqual(["bad", "good"], context.by_id_calls)
        self.assertEqual(0, context.current_calls)

        cancelled = _LookupContext(None, cancelled={"cancel"})
        plugin = self._plugin(cancelled, group={
            "name_wake_mode": "semantic", "name_semantic_timeout_seconds": 1.0,
            "name_semantic_provider_id": "cancel",
            "name_semantic_fallback_provider_ids": [],
        })
        with self.assertRaises(asyncio.CancelledError):
            await plugin._visible_name_wake(_Event())

    async def test_review_lookup_deadline_does_not_start_fallback_or_repair(self):
        current = _Provider(['{"action":"replace","text":"changed"}'])
        fallback = _Provider(['{"action":"keep"}'])
        context = _LookupContext(current, by_id={"fallback": fallback})
        context.block_current = True
        plugin = self._plugin(context, review={
            "mode": "core", "timeout_seconds": 0.01, "max_repair_attempts": 1,
            "repair_enabled": True, "use_repaired_text": True,
            "provider_id": "", "fallback_provider_ids": ["fallback"],
        })
        event = _Event("review timeout")
        try:
            # This is the dedicated real 0.01s deadline path.  Under an
            # asyncio-debug scheduler the deadline may expire before public
            # provider lookup begins; both that and a detached started lookup
            # must leave the same fail-closed result.  Do not couple this
            # timeout oracle to the successful-lookup helper's start barrier.
            outcome = await asyncio.wait_for(
                plugin._review_final_text(
                    event, MAIN.create_snapshot(event, origin="private"), "original"
                ),
                timeout=2.0,
            )
            self.assertEqual("original", outcome.text)
            self.assertTrue(outcome.exhausted)
            self.assertEqual([], current.calls)
            self.assertEqual([], fallback.calls)
            self.assertEqual([], context.by_id_calls)
        finally:
            await self._release_tracked_lookup(plugin, context)

    async def test_eager_done_lookup_past_deadline_is_not_adopted(self):
        """A synchronous eager prefix may not turn an expired lookup into a route.

        This uses the real current-Provider lookup path rather than duplicating
        the auxiliary helper's timeout calculation.  R16 consumed ``provider``
        here because it inspected ``task.done()`` before checking the same
        absolute deadline again.
        """
        provider = _Provider(['{"decision":"DIRECT"}'])
        context = _LookupContext(provider)
        plugin = self._plugin(context)
        event = _Event()
        binding = plugin._bind_auxiliary_call(event, None)
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0.0

        def clock():
            return real_time() + offset

        async def eager_current(_umo):
            nonlocal offset
            # ``Task(..., eager_start=True)`` executes this prefix while it is
            # being built on Python 3.12, before the helper sees task.done().
            offset = 1.1
            return provider

        context.get_using_provider_async = eager_current
        loop.time = clock
        try:
            providers = await plugin._auxiliary_providers(
                event,
                binding,
                explicit_provider_id="",
                fallback_provider_ids=[],
                deadline=clock() + 1.0,
            )
        finally:
            loop.time = real_time
        self.assertIs(providers, MAIN._AUXILIARY_DEADLINE_EXHAUSTED)
        self.assertEqual([], provider.calls)
        await asyncio.sleep(0)
        self.assertEqual(set(), getattr(plugin, "_auxiliary_tasks", set()))

    async def test_eager_prefix_wait_uses_only_the_recomputed_remaining_budget(self):
        """The wait branch must not reuse the pre-construction timeout."""
        context = _LookupContext(_Provider(['{"decision":"DIRECT"}']))
        plugin = self._plugin(context)
        event = _Event()
        binding = plugin._bind_auxiliary_call(event, None)
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0.0
        seen_timeouts: list[float] = []
        original_wait = asyncio.wait

        def clock():
            return real_time() + offset

        async def eager_then_block(_umo):
            nonlocal offset
            offset = 0.5
            await context.lookup_release.wait()
            return context.provider

        async def recorded_wait(tasks, *, timeout=None, **kwargs):
            seen_timeouts.append(float(timeout))
            return set(), set(tasks)

        context.get_using_provider_async = eager_then_block
        loop.time = clock
        asyncio.wait = recorded_wait
        try:
            providers = await plugin._auxiliary_providers(
                event,
                binding,
                explicit_provider_id="",
                fallback_provider_ids=[],
                deadline=clock() + 1.0,
            )
        finally:
            asyncio.wait = original_wait
            loop.time = real_time
            await self._release_tracked_lookup(plugin, context)
        self.assertIs(providers, MAIN._AUXILIARY_DEADLINE_EXHAUSTED)
        # Depending on scheduler overhead, the post-construction fence may
        # already find the shared deadline exhausted (zero waits) or may
        # issue exactly one wait. It must never reuse the original 1.0s budget.
        self.assertLessEqual(len(seen_timeouts), 1)
        if seen_timeouts:
            self.assertLessEqual(seen_timeouts[0], 0.5)

    async def test_done_wait_result_after_deadline_is_not_adopted(self):
        """A non-empty wait completion still needs an adoption-time fence."""
        provider = _Provider(['{"decision":"DIRECT"}'])
        context = _LookupContext(provider)
        plugin = self._plugin(context)
        event = _Event()
        binding = plugin._bind_auxiliary_call(event, None)
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0.0
        wait_used = False
        original_wait = asyncio.wait

        def clock():
            return real_time() + offset

        async def current_after_turn(_umo):
            await asyncio.sleep(0)
            return provider

        async def completed_at_deadline(tasks, *, timeout=None, **kwargs):
            nonlocal offset
            await asyncio.sleep(0)
            offset = 1.0
            return set(tasks), set()

        context.get_using_provider_async = current_after_turn
        loop.time = clock
        asyncio.wait = completed_at_deadline
        try:
            providers = await plugin._await_auxiliary_operation(
                event, binding, lambda: current_after_turn(event.unified_msg_origin),
                deadline=clock() + 1.0, deadline_sentinel=True,
            )
        finally:
            asyncio.wait = original_wait
            loop.time = real_time
            await asyncio.sleep(0)
        self.assertIs(providers, MAIN._AUXILIARY_DEADLINE_EXHAUSTED)
        self.assertEqual(set(), plugin._auxiliary_tasks)

    async def test_done_wait_expired_stale_binding_prefers_no_result(self):
        """A superseded event is stale, not an exhausted current purpose."""
        provider = _Provider(['{"decision":"DIRECT"}'])
        context = _LookupContext(provider)
        plugin = self._plugin(context)
        event = _Event()
        binding = plugin._bind_auxiliary_call(event, None)
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0.0
        original_wait = asyncio.wait
        wait_used = False

        def clock():
            return real_time() + offset

        release = asyncio.Event()

        async def blocked_operation():
            await release.wait()
            return provider

        async def completed_stale_and_expired(tasks, *, timeout=None, **kwargs):
            nonlocal offset
            await asyncio.sleep(0)
            event.set_extra("shio.sys001.auxiliary_binding", object())
            self.assertFalse(plugin._auxiliary_call_is_current(event, binding))
            plugin._auxiliary_epoch += 1
            offset = 1.1
            release.set()
            await asyncio.sleep(0)
            return set(tasks), set()

        loop.time = clock
        asyncio.wait = completed_stale_and_expired
        try:
            providers = await plugin._await_auxiliary_operation(
                event, binding, blocked_operation,
                deadline=clock() + 1.0, deadline_sentinel=True,
            )
        finally:
            asyncio.wait = original_wait
            loop.time = real_time
            await asyncio.sleep(0)
        self.assertIsNone(providers)
        self.assertIsNot(event.get_extra("shio.sys001.auxiliary_binding"), binding)
        self.assertEqual(set(), plugin._auxiliary_tasks)

    async def test_done_wait_expired_stale_epoch_preserves_final_review_response(self):
        """The real review caller must not turn an old response into exhaustion."""
        context = _LookupContext()
        plugin = self._plugin(context, review={
            "mode": "core", "timeout_seconds": 0.01, "max_repair_attempts": 0,
            "repair_enabled": False, "use_repaired_text": False,
            "provider_id": "", "fallback_provider_ids": [],
        })
        event = _Event("review")
        snapshot = MAIN.create_snapshot(event, origin="private")
        lifecycle = MAIN.TurnLifecycle()
        event.set_extra(MAIN.SYS001_TURN_EXTRA, snapshot)
        event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, lifecycle)
        async def stale_provider_lookup(*_args, **_kwargs):
            plugin._auxiliary_epoch += 1
            return None

        plugin._auxiliary_providers = stale_provider_lookup
        outcome = await plugin._review_final_text(event, snapshot, "old response")
        self.assertEqual("old response", outcome.text)
        self.assertTrue(outcome.stale)
        self.assertFalse(outcome.exhausted)
        self.assertFalse(lifecycle.terminal_reason)
        self.assertEqual(set(), getattr(plugin, "_auxiliary_tasks", set()))

    async def test_observe_final_review_done_expired_stale_keeps_old_turn_inert(self):
        """The final-response hook must compose done/expired/stale in one call.

        The recorded wait is deliberately an external scheduling oracle: it
        completes the public current-Provider lookup, advances the same clock
        beyond its budget, and replaces the event binding before the helper may
        adopt the result.  It does not reproduce the helper's fence ordering.
        """
        provider = _Provider(['{"action":"replace","text":"new text"}'])
        context = _LookupContext(provider)
        context.block_current = True
        kv = R13.MasterKVSpy()
        plugin = R13.R13MasterTerminationTests().plugin(kv)
        plugin.context = context
        plugin.config = {"sys001": {"group": {}, "final_review": {
            "mode": "core", "timeout_seconds": 1.0, "max_repair_attempts": 0,
            "repair_enabled": False, "use_repaired_text": False,
            "provider_id": "", "fallback_provider_ids": [],
            "send_last_reply_on_review_exhausted": False,
        }}}
        plugin._NATURAL_KV_AWAIT_SECONDS = 1.0
        event = _Event("old final response")
        snapshot = MAIN.create_snapshot(event, origin="direct")
        lifecycle = MAIN.TurnLifecycle()
        event.set_extra(MAIN.SYS001_TURN_EXTRA, snapshot)
        event.set_extra(MAIN.SYS001_LIFECYCLE_EXTRA, lifecycle)
        first_plain = _RecordedPlain("old final")
        non_text = _NonTextResultSentinel("media-boundary")
        last_plain = _RecordedPlain("response")
        chain = _RecordedMessageChain([first_plain, non_text, last_plain])
        response = _RecordedLLMResponse(chain)
        # Prove the recorded setter gives this oracle discriminatory power:
        # an otherwise invisible same-text write rebuilds and loses the
        # sentinel/identity, so a stale-before-return mutation turns the
        # identity assertions below into a deterministic red result.
        mutation_probe_chain = _RecordedMessageChain([
            _RecordedPlain("old final"), _NonTextResultSentinel("probe"),
            _RecordedPlain("response"),
        ])
        mutation_probe = _RecordedLLMResponse(mutation_probe_chain)
        mutation_probe.completion_text = mutation_probe.completion_text
        self.assertEqual(1, mutation_probe.setter_writes)
        self.assertIs(mutation_probe_chain, mutation_probe.result_chain)
        self.assertEqual(2, len(mutation_probe.result_chain.chain))
        self.assertIsInstance(mutation_probe.result_chain.chain[0], _RecordedPlain)
        self.assertIsInstance(mutation_probe.result_chain.chain[1], _NonTextResultSentinel)
        old_record = plugin._master_alert_record
        master_calls: list[str] = []
        continuous_calls: list[tuple[str, str]] = []

        async def forbidden_master(*_args, **_kwargs):
            master_calls.append("called")
            raise AssertionError("stale final review must not record a Master alert")

        async def forbidden_continuous(scope, message_id):
            continuous_calls.append((scope, message_id))
            raise AssertionError("stale final review must not mark a terminal window")

        plugin._record_master_alert_terminal = forbidden_master
        plugin._continuous_mark_terminal = forbidden_continuous
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0.0
        original_wait = asyncio.wait

        def clock():
            return real_time() + offset

        # The helper binding is stored on the event.  Capture it in the wait
        # oracle after lookup started, before replacing it with the invalidator.
        async def wait_with_bound_stale(tasks, *, timeout=None, **_kwargs):
            nonlocal offset
            await asyncio.wait_for(context.lookup_started.wait(), timeout=2.0)
            binding = event.get_extra("shio.sys001.auxiliary_binding")
            self.assertIsNotNone(binding)
            event.set_extra("shio.sys001.auxiliary_binding", object())
            self.assertFalse(plugin._auxiliary_call_is_current(event, binding))
            offset = 1.1
            context.lookup_release.set()
            await asyncio.sleep(0)
            return set(tasks), set()

        loop.time = clock
        asyncio.wait = wait_with_bound_stale
        try:
            await plugin.observe_final_agent_response(event, response)
        finally:
            asyncio.wait = original_wait
            loop.time = real_time
            await self._release_tracked_lookup(plugin, context)
        self.assertEqual("old final response", response.completion_text)
        self.assertEqual(0, response.setter_writes)
        self.assertIs(chain, response.result_chain)
        self.assertEqual(3, len(response.result_chain.chain))
        self.assertIs(first_plain, response.result_chain.chain[0])
        self.assertIs(non_text, response.result_chain.chain[1])
        self.assertIs(last_plain, response.result_chain.chain[2])
        self.assertEqual(("old final", "media-boundary", "response"), (
            response.result_chain.chain[0].text, response.result_chain.chain[1].name,
            response.result_chain.chain[2].text,
        ))
        self.assertIsNone(event.get_extra(MAIN.SYS001_FINAL_AGENT_OBSERVATION_EXTRA))
        self.assertEqual(("captured", ""), (lifecycle.state, lifecycle.terminal_reason))
        self.assertEqual([], master_calls)
        self.assertEqual([], continuous_calls)
        self.assertIs(plugin._master_alert_record, old_record)
        kv.assert_unused(self)
        self.assertEqual([], getattr(context, "send_trace", []))
        self.assertEqual(set(), getattr(plugin, "_auxiliary_tasks", set()))

    async def test_done_wait_result_before_deadline_remains_usable(self):
        provider = _Provider(['{"decision":"DIRECT"}'])
        context = _LookupContext(provider)
        plugin = self._plugin(context)
        event = _Event()
        binding = plugin._bind_auxiliary_call(event, None)
        original_wait = asyncio.wait

        async def current_after_turn(_umo):
            await asyncio.sleep(0)
            return provider

        async def completed_in_budget(tasks, *, timeout=None, **kwargs):
            await asyncio.sleep(0)
            return set(tasks), set()

        context.get_using_provider_async = current_after_turn
        asyncio.wait = completed_in_budget
        try:
            providers = await plugin._auxiliary_providers(
                event, binding, explicit_provider_id="", fallback_provider_ids=[],
                deadline=asyncio.get_running_loop().time() + 1.0,
            )
        finally:
            asyncio.wait = original_wait
        self.assertEqual([provider], providers)
