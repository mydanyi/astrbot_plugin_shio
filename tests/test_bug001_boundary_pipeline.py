"""BUG-001 replay through AstrBot 4.27.4 public pipeline entry points.

Each case registers the real Shio handlers in AstrBot's public registry, then
uses fixed-core Waking/PreProcess/Process, the Agent hook dispatcher,
RespondStage, or PluginManager.reload.  The assertions never call Shio terminal
helpers, wakeups, ``record_standard_send`` or ``terminate`` directly.
"""

from __future__ import annotations

import asyncio
import zoneinfo
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

import pytest
import tzlocal

# AstrBot's shared-preferences singleton asks tzlocal for the Windows zone
# during import.  On this UNC test workspace that resource lookup can stall;
# pinning UTC only keeps imports deterministic and does not replace any of the
# Waking/Process/PreProcess/Respond/reload behavior exercised below.
tzlocal.get_localzone = lambda: zoneinfo.ZoneInfo("UTC")

from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.process_stage.method.star_request import (
    StarRequestSubStage,
)
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.filter.platform_adapter_type import (
    PlatformAdapterType,
    PlatformAdapterTypeFilter,
)
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from astrbot.core.star.star import star_map, star_registry
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)
from astrbot.core.star.star_manager import PluginManager

from astrbot_plugin_shio.main import ShioPlugin


class _InboundEvent(AstrMessageEvent):
    """In-memory platform endpoint; RespondStage still owns hook scheduling."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: list[object] = []

    async def send(self, message):
        self.sent.append(message)


class _NoOpAgentSubStage:
    async def process(self, event):
        if False:  # Keep this an async generator without Provider I/O.
            yield event


class _ConversationManager:
    async def get_curr_conversation_id(self, scope):
        return "bug001"

    async def get_conversation(self, scope, conversation_id):
        return SimpleNamespace(id=conversation_id)

    async def new_conversation(self, scope, *, platform_id):
        return "bug001"


def _core_config() -> dict:
    return {
        "admins_id": ["master"],
        "wake_prefix": [],
        "plugin_set": ["*"],
        "platform_settings": {
            "no_permission_reply": True,
            "friend_message_needs_wake_prefix": False,
            "ignore_bot_self_message": False,
            "ignore_at_all": False,
            "unique_session": False,
            "reply_with_mention": False,
            "reply_with_quote": False,
            "segmented_reply": {
                "enable": False,
                "only_llm_result": False,
                "interval_method": "random",
                "log_base": 10,
                "interval": "1.5,3.5",
            },
        },
        "disable_builtin_commands": False,
        "provider_settings": {
            "enable": True,
            "prompt_prefix": "",
            "identifier": "fixed-test-provider",
        },
    }


def _plugin() -> ShioPlugin:
    context = SimpleNamespace(conversation_manager=_ConversationManager())
    return ShioPlugin(
        context,
        {
            "sys001": {
                "ingress": {"group_allowed_scopes": ["bug001-group", "other-group"]},
                "group": {
                    "continuous_window_enabled": True,
                    "continuous_window_seconds": 1,
                    "continuous_window_max_scopes": 4,
                    "natural_participation_enabled": False,
                },
            }
        },
    )


def _event(
    message_id: str,
    sender_id: str,
    text: str,
    components: list,
    scope: str = "bug001-group",
):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = "bot"
    message.session_id = scope
    message.message_id = message_id
    message.group_id = scope
    message.sender = MessageMember(user_id=sender_id, nickname=sender_id)
    message.message = components
    message.message_str = text
    message.raw_message = None
    return _InboundEvent(
        message_str=text,
        message_obj=message,
        platform_meta=PlatformMetadata(
            name="aiocqhttp", description="BUG-001 fixture", id="bug001"
        ),
        session_id=scope,
    )


async def _run_official_pipeline(event, pipeline_context, *, preprocess=False) -> None:
    """Run fixed PreProcess/Waking/Process entry points without private signals."""
    if preprocess:
        preprocessor = PreProcessStage()
        await preprocessor.initialize(pipeline_context)
        await preprocessor.process(event)

    waking = WakingCheckStage()
    await waking.initialize(pipeline_context)
    await waking.process(event)
    assert any(
        handler.handler_module_path == ShioPlugin.__module__
        for handler in event.get_extra("activated_handlers", [])
    ), "fixed WakingCheckStage did not activate the enabled Shio adapter handler"

    process = ProcessStage()
    process.ctx = pipeline_context
    process.config = pipeline_context.astrbot_config
    process.plugin_manager = pipeline_context.plugin_manager
    process.star_request_sub_stage = StarRequestSubStage()
    await process.star_request_sub_stage.initialize(pipeline_context)
    # ProcessStage remains the official entry.  The no-op leaf preserves its
    # observable ProviderRequest hand-off without contacting a Provider.
    process.agent_sub_stage = _NoOpAgentSubStage()
    async for _ in process.process(event):
        pass


async def _expect_provider(task, event) -> None:
    await asyncio.wait_for(task, timeout=3)
    assert event.get_extra("provider_request") is not None


async def _expect_waiting(task) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.05)


async def _cancel_fixture_task(task) -> None:
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _official_agent_done_without_send(event) -> None:
    """Use AstrBot's MainAgentHooks dispatcher, not a plugin hook invocation."""
    await MAIN_AGENT_HOOKS.on_agent_done(
        SimpleNamespace(context=SimpleNamespace(event=event)),
        LLMResponse(role="assistant", completion_text="final without transport"),
    )


@asynccontextmanager
async def _official_fixture():
    """Install the same bound handler shapes that AstrBot's loader registers."""
    plugin = _plugin()
    pipeline_context = SimpleNamespace(
        astrbot_config=_core_config(), plugin_manager=SimpleNamespace()
    )
    module_path = ShioPlugin.__module__
    metadata = star_map[module_path]
    original_star = metadata.star_cls
    original_activated = metadata.activated
    original_handlers = list(star_handlers_registry)
    original_star_map = dict(star_map)
    original_star_registry = list(star_registry)
    original_session_filter = SessionPluginManager.__dict__["filter_handlers_by_session"]

    star_handlers_registry.clear()
    star_handlers_registry.append(
        StarHandlerMetadata(
            event_type=EventType.AdapterMessageEvent,
            handler_full_name=f"{module_path}_request_event_bound_reply",
            handler_name="request_event_bound_reply",
            handler_module_path=module_path,
            handler=plugin.request_event_bound_reply,
            event_filters=[PlatformAdapterTypeFilter(PlatformAdapterType.ALL)],
        )
    )
    star_handlers_registry.append(
        StarHandlerMetadata(
            event_type=EventType.OnLLMResponseEvent,
            handler_full_name=f"{module_path}_observe_final_agent_response",
            handler_name="observe_final_agent_response",
            handler_module_path=module_path,
            handler=plugin.observe_final_agent_response,
            event_filters=[],
        )
    )
    star_handlers_registry.append(
        StarHandlerMetadata(
            event_type=EventType.OnAfterMessageSentEvent,
            handler_full_name=f"{module_path}_record_standard_send",
            handler_name="record_standard_send",
            handler_module_path=module_path,
            handler=plugin.record_standard_send,
            event_filters=[],
        )
    )
    metadata.star_cls = plugin
    metadata.activated = True
    star_registry[:] = [metadata]

    async def _enabled_in_this_fixture(event, handlers):
        return handlers

    # The fixture has no durable session-plugin records.  Its declared plugin
    # is enabled above, so Waking's official selection only bypasses unrelated
    # local persistence; it still operates on the real public registry.
    SessionPluginManager.filter_handlers_by_session = staticmethod(
        _enabled_in_this_fixture
    )
    try:
        yield plugin, pipeline_context, metadata
    finally:
        metadata.star_cls = original_star
        metadata.activated = original_activated
        SessionPluginManager.filter_handlers_by_session = original_session_filter
        star_handlers_registry.clear()
        for handler in original_handlers:
            star_handlers_registry.append(handler)
        star_map.clear()
        star_map.update(original_star_map)
        star_registry[:] = original_star_registry


def _master_text(message_id: str, scope: str = "bug001-group"):
    return _event(
        message_id,
        "master",
        "@bot continue",
        [At(qq="bot"), Plain("continue")],
        scope,
    )


def _master_media(message_id: str, scope: str = "bug001-group"):
    return _event(
        message_id,
        "master",
        "@bot image",
        [At(qq="bot"), Image(None)],
        scope,
    )


@pytest.mark.asyncio
async def test_reply_without_official_terminal_does_not_block_later_master_request():
    """Matrix 1: non-waking Reply never owns a boundary."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        reply = _event(
            "reply-without-agent", "member", "", [Reply(id="prior", sender_id="other")]
        )
        await _run_official_pipeline(reply, pipeline_context)
        assert reply.get_extra("provider_request") is None
        print(f"reply_boundary_scope={reply.get_extra('shio.sys001.batch_scope')!r}")

        master = _master_text("master-after-reply")
        process_task = asyncio.create_task(_run_official_pipeline(master, pipeline_context))
        try:
            await _expect_provider(process_task, master)
        finally:
            await _cancel_fixture_task(process_task)


@pytest.mark.asyncio
async def test_media_preprocess_failure_does_not_lock_same_scope():
    """Matrix 2: fixed PreProcess catches media conversion failure before @bot."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        # Image(None) makes the official Image.convert_to_file_path raise; the
        # fixed PreProcessStage catches it as a media preprocessing failure.
        media = _event("ordinary-media-error", "member", "", [Image(None)])
        await _run_official_pipeline(media, pipeline_context, preprocess=True)
        assert media.get_extra("provider_request") is None
        assert media.get_extra("shio.sys001.batch_scope") is None

        master = _master_text("master-after-media-error")
        process_task = asyncio.create_task(_run_official_pipeline(master, pipeline_context))
        try:
            await _expect_provider(process_task, master)
        finally:
            await _cancel_fixture_task(process_task)


@pytest.mark.asyncio
async def test_agent_done_hook_without_send_releases_media_boundary():
    """Matrix 3a: MainAgentHooks emits real OnLLMResponse without RespondStage."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        boundary = _master_media("agent-done-boundary")
        await _run_official_pipeline(boundary, pipeline_context, preprocess=True)
        follower = _master_text("after-agent-done")
        follower_task = asyncio.create_task(_run_official_pipeline(follower, pipeline_context))
        try:
            await _expect_waiting(follower_task)
            await _official_agent_done_without_send(boundary)
            assert boundary.sent == []
            await _expect_provider(follower_task, follower)
        finally:
            await _cancel_fixture_task(follower_task)


@pytest.mark.asyncio
async def test_agent_done_hook_keeps_text_batch_until_respond_stage():
    """R10 guard: a text final response keeps its visible-send ordering."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        text = _master_text("text-final")
        await _run_official_pipeline(text, pipeline_context)
        follower = _master_text("after-text-final")
        follower_task = asyncio.create_task(_run_official_pipeline(follower, pipeline_context))
        try:
            await _expect_waiting(follower_task)
            await _official_agent_done_without_send(text)
            await _expect_waiting(follower_task)
            respond = RespondStage()
            await respond.initialize(pipeline_context)
            text.set_result(text.plain_result("visible text"))
            await respond.process(text)
            await _expect_provider(follower_task, follower)
        finally:
            await _cancel_fixture_task(follower_task)


@pytest.mark.asyncio
async def test_respond_stage_duplicate_after_send_is_idempotent():
    """Matrix 3b: RespondStage dispatches both AfterMessageSent events itself."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        boundary = _master_media("after-send-boundary")
        await _run_official_pipeline(boundary, pipeline_context, preprocess=True)
        follower = _master_text("after-duplicate-send")
        follower_task = asyncio.create_task(_run_official_pipeline(follower, pipeline_context))
        try:
            await _expect_waiting(follower_task)
            respond = RespondStage()
            await respond.initialize(pipeline_context)
            boundary.set_result(boundary.plain_result("first public send"))
            await respond.process(boundary)
            boundary.set_result(boundary.plain_result("duplicate public send"))
            await respond.process(boundary)
            assert len(boundary.sent) == 2
            await _expect_provider(follower_task, follower)
        finally:
            await _cancel_fixture_task(follower_task)


@pytest.mark.asyncio
async def test_no_public_terminal_fails_closed_until_official_reload():
    """Matrix 4: only PluginManager.reload releases a terminal-less chain."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, metadata):
        boundary = _master_media("reload-boundary")
        await _run_official_pipeline(boundary, pipeline_context, preprocess=True)
        follower = _master_text("reload-follower")
        follower_task = asyncio.create_task(_run_official_pipeline(follower, pipeline_context))
        try:
            await _expect_waiting(follower_task)
            # Invoke the fixed public reload entry.  Stub only its subsequent
            # discovery/load step: reload itself performs the official
            # terminate/unbind scheduling that fences this plugin instance.
            manager = PluginManager.__new__(PluginManager)
            manager._pm_lock = asyncio.Lock()

            async def _reload_fixture_load(*_args, **_kwargs):
                return True, None

            manager.load = _reload_fixture_load
            result = await manager.reload(metadata.name)
            assert result == (True, None)
            await asyncio.wait_for(follower_task, timeout=1)
            assert follower.get_extra("provider_request") is None
            assert boundary.sent == []
        finally:
            await _cancel_fixture_task(follower_task)


@pytest.mark.asyncio
async def test_old_generation_terminal_cannot_release_new_scope_or_other_scope():
    """Matrix 5: late hook is generation-fenced and another scope is independent."""
    async with _official_fixture() as (_plugin_instance, pipeline_context, _metadata):
        old_boundary = _master_media("old-generation")
        await _run_official_pipeline(old_boundary, pipeline_context, preprocess=True)
        await _official_agent_done_without_send(old_boundary)

        current_boundary = _master_media("current-generation")
        await _run_official_pipeline(current_boundary, pipeline_context, preprocess=True)
        same_scope_follower = _master_text("same-scope-follower")
        same_scope_task = asyncio.create_task(
            _run_official_pipeline(same_scope_follower, pipeline_context)
        )
        other_scope = _master_text("other-scope-follower", scope="other-group")
        other_scope_task = asyncio.create_task(
            _run_official_pipeline(other_scope, pipeline_context)
        )
        try:
            await _expect_waiting(same_scope_task)
            # Deliver a late old-generation send through RespondStage itself.
            # Its public AfterMessageSent dispatch must not release the newer
            # boundary currently holding this same scope.
            respond = RespondStage()
            await respond.initialize(pipeline_context)
            old_boundary.set_result(old_boundary.plain_result("late old send"))
            await respond.process(old_boundary)
            await _expect_waiting(same_scope_task)
            await _expect_provider(other_scope_task, other_scope)
            await _official_agent_done_without_send(current_boundary)
            await _expect_provider(same_scope_task, same_scope_follower)
        finally:
            await _cancel_fixture_task(same_scope_task)
            await _cancel_fixture_task(other_scope_task)
