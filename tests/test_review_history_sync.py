"""History synchronization for the final agent observation.

The hook plumbing (fences, run tokens, extras) is covered elsewhere; the
defect under test is only what happens to ``run_context.messages`` once
the turn carries a final observation.
"""

import json
from types import SimpleNamespace

import pytest

from astrbot.core.agent.message import (
    Message,
    TextPart,
    ThinkPart,
    dump_messages_with_checkpoints,
)
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_shio.core.sys001 import (
    SYS001_FINAL_AGENT_OBSERVATION_EXTRA,
    SYS001_LIFECYCLE_EXTRA,
    FinalAgentObservation,
    TurnLifecycle,
)

from test_dialogue_preparation import cfg, mk, turn

pytestmark = pytest.mark.asyncio

TOOL_CALL = {
    "id": "x",
    "type": "function",
    "function": {"name": "lookup", "arguments": "{}"},
}

DRAFT = [ThinkPart(think="old reasoning"), TextPart(text="raw")]


def dump(messages):
    return json.dumps(
        dump_messages_with_checkpoints(list(messages)),
        default=str,
        ensure_ascii=False,
    )


def prepare(messages, observation, stored_context=None):
    plugin = mk(config=cfg(prep="off"))
    event = turn()
    event.set_extra(SYS001_LIFECYCLE_EXTRA, TurnLifecycle())
    run = SimpleNamespace(messages=list(messages))
    event.set_extra("shio.sys001.agent_run_token", object())
    event.set_extra(
        "shio.sys001.agent_run_context",
        run if stored_context is None else stored_context,
    )
    event.set_extra(SYS001_FINAL_AGENT_OBSERVATION_EXTRA, observation)
    return plugin, event, run


async def observe(plugin, event, run, response):
    await plugin.observe_main_agent_done(event, run, response)


async def test_final_text_replaces_draft_tail_only():
    messages = [
        Message(role="user", content=[TextPart(text="question")]),
        Message(role="assistant", content=[TextPart(text="raw")]),
        Message(role="assistant", content=list(DRAFT)),
    ]
    response = LLMResponse(role="assistant", completion_text="reviewed")
    plugin, event, run = prepare(
        messages, FinalAgentObservation("final_text", response)
    )
    head_before = dump(run.messages[:2])

    await observe(plugin, event, run, response)

    after = dump(run.messages)
    assert "reviewed" in after
    assert "old reasoning" not in after
    assert dump(run.messages[:2]) == head_before
    assert after.count('"raw"') == 1


async def test_review_exhausted_drops_tail_and_keeps_history():
    messages = [
        Message(role="user", content=[TextPart(text="question")]),
        Message(role="assistant", content=[], tool_calls=[TOOL_CALL]),
        Message(role="tool", content=[TextPart(text="tool result")], tool_call_id="x"),
        Message(role="assistant", content=[TextPart(text="raw")]),
    ]
    response = LLMResponse(role="assistant", completion_text="")
    plugin, event, run = prepare(
        messages, FinalAgentObservation("review_exhausted", response)
    )
    head_before = dump(run.messages[:3])

    await observe(plugin, event, run, response)

    after = dump(run.messages)
    assert '"raw"' not in after
    assert "tool result" in after
    assert dump(run.messages[:3]) == head_before


@pytest.mark.parametrize(
    "response,stored_context,tail",
    [
        (
            LLMResponse(role="assistant", completion_text="raw"),
            None,
            Message(role="assistant", content=list(DRAFT)),
        ),
        (
            LLMResponse(role="assistant", completion_text="reviewed"),
            object(),
            Message(role="assistant", content=list(DRAFT)),
        ),
        (
            LLMResponse(role="assistant", completion_text="reviewed"),
            None,
            Message(role="assistant", content=[], tool_calls=[TOOL_CALL]),
        ),
    ],
    ids=["identical_raw", "foreign_run_context", "tool_call_tail"],
)
async def test_preservation_leaves_messages_untouched(response, stored_context, tail):
    messages = [
        Message(role="user", content=[TextPart(text="question")]),
        tail,
    ]
    plugin, event, run = prepare(
        messages,
        FinalAgentObservation("final_text", response),
        stored_context=stored_context,
    )
    before = dump(run.messages)

    await observe(plugin, event, run, response)

    assert dump(run.messages) == before
