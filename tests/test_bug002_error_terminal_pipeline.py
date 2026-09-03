"""BUG-002 regression: ToolLoop errors must not reach the current group."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
import zoneinfo

import pytest
import tzlocal

# AstrBot's shared-preferences singleton asks for the host zone while its
# process-stage modules are importing.  This matches the fixed BUG-001
# official-pipeline fixture and keeps that public import order deterministic.
tzlocal.get_localzone = lambda: zoneinfo.ZoneInfo("UTC")

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.message.components import At, Plain
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
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

from astrbot_plugin_shio.core.sys001 import (
    SYS001_FINAL_AGENT_OBSERVATION_EXTRA,
    SYS001_LIFECYCLE_EXTRA,
    TurnLifecycle,
)
from astrbot_plugin_shio.main import ShioPlugin


class _InboundEvent(AstrMessageEvent):
    """Isolated OneBot-shaped event; real RespondStage owns the send call."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: list[object] = []

    async def send(self, message):
        self.sent.append(message)


class _ConversationManager:
    async def get_curr_conversation_id(self, _scope):
        return "bug002"

    async def get_conversation(self, _scope, conversation_id):
        return SimpleNamespace(id=conversation_id)

    async def new_conversation(self, _scope, *, platform_id):
        return "bug002"


class _PluginRuntimeContext:
    conversation_manager = _ConversationManager()

    async def get_using_tts_provider_async(self, _scope):
        return None


class _NoOpAgentSubStage:
    async def process(self, _event):
        if False:
            yield None


class _FailingProvider:
    """The only substituted boundary: an isolated Provider transport failure."""

    provider_config = {"id": "bug002-failing-provider", "max_context_tokens": 0}

    async def text_chat(self, **_kwargs):
        raise RuntimeError("isolated provider failure")


class _NoOpHooks(BaseAgentRunHooks):
    async def on_agent_begin(self, _run_context):
        return None

    async def on_tool_start(self, _run_context, _tool, _tool_args):
        return None

    async def on_tool_end(self, _run_context, _tool, _tool_args, _tool_result):
        return None

    async def on_agent_done(self, _run_context, _llm_response):
        return None


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
            "reply_prefix": "",
            "forward_threshold": 9999,
            "segmented_reply": {
                "enable": False,
                "only_llm_result": False,
                "interval_method": "random",
                "log_base": 10,
                "interval": "1.5,3.5",
                "words_count_threshold": 9999,
                "split_mode": "regex",
                "regex": r".*",
                "split_words": ["。"],
                "content_cleanup_rule": "",
            },
        },
        "provider_settings": {
            "enable": True,
            "prompt_prefix": "",
            "identifier": "bug002-failing-provider",
        },
        "provider_tts_settings": {"enable": False, "trigger_probability": 1},
        "content_safety": {"also_use_in_response": False},
        "t2i": False,
        "t2i_strategy": "local",
        "t2i_word_threshold": 150,
        "t2i_active_template": "",
        "t2i_use_file_service": False,
        "callback_api_base": "",
    }


def _plugin() -> ShioPlugin:
    return ShioPlugin(
        _PluginRuntimeContext(),
        {
            "sys001": {
                "ingress": {"group_allowed_scopes": ["bug002-group"]},
                "group": {
                    "continuous_window_enabled": False,
                    "natural_participation_enabled": False,
                },
                "master_alert": {"master_alert_enabled": False},
            }
        },
    )


def _event() -> _InboundEvent:
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = "bot"
    message.session_id = "bug002-group"
    message.message_id = "bug002-error"
    message.group_id = "bug002-group"
    message.sender = MessageMember(user_id="master", nickname="master")
    message.message = [At(qq="bot"), Plain("continue")]
    message.message_str = "@bot continue"
    message.raw_message = None
    return _InboundEvent(
        message_str=message.message_str,
        message_obj=message,
        platform_meta=PlatformMetadata(
            name="aiocqhttp", description="BUG-002 fixture", id="bug002"
        ),
        session_id=message.session_id,
    )


async def _run_public_entry(event, pipeline_context) -> None:
    waking = WakingCheckStage()
    await waking.initialize(pipeline_context)
    await waking.process(event)
    process = ProcessStage()
    process.ctx = pipeline_context
    process.config = pipeline_context.astrbot_config
    process.plugin_manager = pipeline_context.plugin_manager
    process.star_request_sub_stage = StarRequestSubStage()
    await process.star_request_sub_stage.initialize(pipeline_context)
    process.agent_sub_stage = _NoOpAgentSubStage()
    async for _ in process.process(event):
        pass


async def _tool_loop_error_chain(event, request):
    """Run the fixed ToolLoop final-exception branch, without Shio hook calls."""
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=_FailingProvider(),
        request=request,
        run_context=ContextWrapper(context=SimpleNamespace(event=event)),
        tool_executor=SimpleNamespace(),
        agent_hooks=_NoOpHooks(),
        streaming=False,
    )
    chains = [
        chain
        async for chain in run_agent(runner, max_step=1, show_tool_use=False)
        if chain is not None
    ]
    final = runner.get_final_llm_resp()
    assert final is not None and final.role == "err"
    assert len(chains) == 1
    return chains[0]


@asynccontextmanager
async def _official_fixture():
    plugin = _plugin()
    pipeline_context = SimpleNamespace(
        astrbot_config=_core_config(),
        plugin_manager=SimpleNamespace(context=plugin.context),
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
    for event_type, name, handler, filters in (
        (
            EventType.AdapterMessageEvent,
            "request_event_bound_reply",
            plugin.request_event_bound_reply,
            [PlatformAdapterTypeFilter(PlatformAdapterType.ALL)],
        ),
        (
            EventType.OnDecoratingResultEvent,
            "layout_text_components",
            plugin.layout_text_components,
            [],
        ),
    ):
        star_handlers_registry.append(
            StarHandlerMetadata(
                event_type=event_type,
                handler_full_name=f"{module_path}_{name}",
                handler_name=name,
                handler_module_path=module_path,
                handler=handler,
                event_filters=filters,
            )
        )
    metadata.star_cls = plugin
    metadata.activated = True
    star_registry[:] = [metadata]

    async def _enabled_in_fixture(_event, handlers):
        return handlers

    SessionPluginManager.filter_handlers_by_session = staticmethod(_enabled_in_fixture)
    try:
        yield pipeline_context
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


@pytest.mark.asyncio
async def test_manual_tool_loop_chain_without_agent_hook_is_not_suppressed():
    """A fixture chain without the public Agent hook cannot claim final_error.

    This deliberately low-level neighbor protects the boundary introduced by
    BUG-002.  The Docker L5 harness is the C1 proof because only it loads the
    plugin via PluginManager and dispatches the real MainAgent hook.
    """
    async with _official_fixture() as pipeline_context:
        event = _event()
        await _run_public_entry(event, pipeline_context)
        request = event.get_extra("provider_request")
        assert request is not None
        request.conversation = SimpleNamespace(token_usage=0)
        chain = await _tool_loop_error_chain(event, request)
        event.set_result(
            MessageEventResult(
                chain=chain.chain,
                result_content_type=ResultContentType.GENERAL_RESULT,
            )
        )
        decorate = ResultDecorateStage()
        await decorate.initialize(pipeline_context)
        async for _ in decorate.process(event):
            pass
        respond = RespondStage()
        await respond.initialize(pipeline_context)
        await respond.process(event)

        lifecycle = event.get_extra(SYS001_LIFECYCLE_EXTRA)
        assert isinstance(lifecycle, TurnLifecycle)
        assert lifecycle.terminal_reason == ""
        assert event.get_extra(SYS001_FINAL_AGENT_OBSERVATION_EXTRA) is None
        assert event.sent != []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_lifecycle", [None, object()])
async def test_final_agent_response_ignores_missing_or_invalid_lifecycle(
    invalid_lifecycle,
):
    """A malformed event extra must not turn an Agent error hook into a crash."""
    async with _official_fixture() as pipeline_context:
        event = _event()
        await _run_public_entry(event, pipeline_context)
        event.set_extra(SYS001_LIFECYCLE_EXTRA, invalid_lifecycle)
        plugin = star_map[ShioPlugin.__module__].star_cls

        await plugin.observe_final_agent_response(
            event, LLMResponse(role="err", completion_text="isolated error")
        )

        assert event.get_extra(SYS001_FINAL_AGENT_OBSERVATION_EXTRA) is None
