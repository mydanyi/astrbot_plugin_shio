"""Production tool-context payload contract for the dialogue preparation boundary.

These cases drive the real ``attach_turn_and_project_capabilities`` entry point
and assert only on the payload the preparation hands to the auxiliary model.
They never assert a mocked response or a fixed prompt style.

Contract under test: the preparation prompt must carry one independent JSON
tool-catalog line whose title is
``【本轮实际可见工具（只用于理解现实任务，不执行）】``.  The array elements are
``{name, description}`` taken verbatim from the tools currently present on
``req.func_tool.tools``.  The preparation must see the *projected* visibility:
an ordinary group member is narrowed by
``sys001.capability_visibility.ordinary_capability_names`` while a Master keeps
the real set.  No tool is fabricated and no tool is executed.  The decision
protocol itself is unchanged (scene / keep_memories / knowledge_query only).

Expected failure points against the current tree (this file adds no production
change):

* No tool-catalog line exists at all -> every case fails at parse time.
* ``_project_group_visible_tools`` currently runs *after* the preparation call
  inside ``attach_turn_and_project_capabilities``, so an ordinary member's
  preparation still sees ``astrbot_execute_shell`` -> the member case fails on
  the extra name (projection too late / wrong object).
* A missing empty-catalog path would echo stale or invented tools -> the empty
  case fails on a non-``[]`` value.
* A disabled preparation that still invokes the auxiliary provider or mutates
  the incoming in-context parts -> the disabled case fails.

Second contract: when the preparation decides ``real_execution``, the real
request must gain exactly one temporary ``TextPart`` beginning with
``[Shio 本轮现实任务]``.  It carries this turn's real utterance, the real
sender, and a tool catalog identical to ``req.func_tool.tools``; it must not
impersonate the role-world scene (``[Shio 本轮角色场景]``).  The official
``assemble_context`` / ``bind_checkpoint_messages`` / ``dump_messages_with_checkpoints``
APIs prove the focus is provided to the model yet not persisted, and the
incoming ``contexts`` and original ``prompt`` stay untouched.  Expected failure
against the current tree: ``prepare_dialogue_turn`` only injects the scene part
for ``role_action``/``roleplay``/``daily``, so ``real_execution`` produces no
focus part and both cases fail at "must be exactly one".
"""
from __future__ import annotations

import json

import pytest

from astrbot.core.agent.message import bind_checkpoint_messages, dump_messages_with_checkpoints
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.components import At, Plain

from astrbot_plugin_shio.core.sys001 import SYS001_TURN_EXTRA, create_snapshot

from test_bug001_boundary_pipeline import _event
from test_dialogue_preparation import (
    BASE,
    HISTORY,
    RAG,
    TXT,
    FakeProvider,
    cfg,
    calls,
    has,
    mk,
    req,
    turn,
)

pytestmark = pytest.mark.asyncio

TITLE = "【本轮实际可见工具（只用于理解现实任务，不执行）】"

SEND = FunctionTool(
    name="send_message_to_user",
    description="向用户发送消息；mention_user 决定是否 @ 对方",
    parameters={"type": "object", "properties": {}},
)
SHELL = FunctionTool(
    name="astrbot_execute_shell",
    description="在宿主机执行 shell 命令",
    parameters={"type": "object", "properties": {}},
)


def turn_role(role: str):
    """Re-capture the official snapshot after fixing the admin role."""
    event = turn()
    event.role = role
    event.set_extra(SYS001_TURN_EXTRA, create_snapshot(event, origin="directed"))
    return event


def cfg_member(allowed):
    """Extend the shared cfg() with the exact capability_visibility keys."""
    config = cfg()
    config["sys001"]["capability_visibility"] = {
        "ordinary_capability_names": list(allowed),
        "friendly_sender_ids": [],
    }
    return config


def tool_catalog(prompt: str):
    """Return the JSON array on the first non-empty line after the fixed title."""
    lines = prompt.split("\n")
    for index, line in enumerate(lines):
        if line.strip() != TITLE:
            continue
        for candidate in lines[index + 1:]:
            if candidate.strip():
                payload = json.loads(candidate.strip())
                assert isinstance(payload, list), "工具目录必须是 JSON 数组"
                for entry in payload:
                    assert isinstance(entry, dict)
                    assert set(entry) == {"name", "description"}, (
                        "工具目录元素只能是 {name, description}"
                    )
                return payload
        raise AssertionError("工具目录标题行后缺少 JSON 行")
    raise AssertionError("准备器 prompt 缺少工具目录标题行")


def catalog_map(prompt: str):
    return {entry["name"]: entry["description"] for entry in tool_catalog(prompt)}


async def test_master_sees_real_send_message_and_shell_tools():
    prov = FakeProvider()
    plug = mk(provider=prov)
    request, _, _ = req()
    request.func_tool = ToolSet(tools=[SEND, SHELL])
    await plug.attach_turn_and_project_capabilities(turn_role("admin"), request)
    prompt = prov.calls[0]["prompt"]
    entries = tool_catalog(prompt)
    assert {entry["name"] for entry in entries} == {
        "send_message_to_user",
        "astrbot_execute_shell",
    }
    visible = catalog_map(prompt)
    assert visible["send_message_to_user"] == SEND.description
    assert "mention_user" in visible["send_message_to_user"]
    assert visible["astrbot_execute_shell"] == SHELL.description


async def test_ordinary_member_preparation_sees_only_whitelisted_send_tool():
    prov = FakeProvider()
    plug = mk(provider=prov, config=cfg_member(["send_message_to_user"]))
    request, _, _ = req()
    request.func_tool = ToolSet(tools=[SEND, SHELL])
    await plug.attach_turn_and_project_capabilities(turn_role("member"), request)
    prompt = prov.calls[0]["prompt"]
    visible = catalog_map(prompt)
    assert set(visible) == {"send_message_to_user"}
    assert visible["send_message_to_user"] == SEND.description
    assert "astrbot_execute_shell" not in visible


async def test_empty_toolset_catalog_is_empty_without_fabrication():
    prov = FakeProvider()
    plug = mk(provider=prov)
    request, _, _ = req()
    request.func_tool = ToolSet(tools=[])
    await plug.attach_turn_and_project_capabilities(turn_role("admin"), request)
    prompt = prov.calls[0]["prompt"]
    assert tool_catalog(prompt) == []


async def test_disabled_preparation_does_not_call_provider_or_touch_context():
    prov = FakeProvider(boom=True)
    plug = mk(provider=prov, config=cfg(prep="off"))
    request, memory, other = req()
    request.func_tool = ToolSet(tools=[SEND, SHELL])
    await plug.attach_turn_and_project_capabilities(turn_role("member"), request)
    assert calls(prov) == []
    assert has(request, memory) and memory.text == RAG and has(request, other)
    assert request.prompt == TXT


# --- real_execution current-task focus -----------------------------------------

FOCUS_TITLE = "[Shio 本轮现实任务]"
ROLE_WORLD_TITLE = "[Shio 本轮角色场景]"
REAL_EXEC = json.dumps(
    {"scene": "real_execution", "keep_memories": [], "knowledge_query": ""},
    ensure_ascii=False,
)


def _as_text(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def first_tool_catalog(text: str):
    """Return the first embedded array whose items are exactly {name, description}."""
    decoder = json.JSONDecoder()
    index = text.find("[")
    while index != -1:
        try:
            value, _end = decoder.raw_decode(text[index:])
        except ValueError:
            index = text.find("[", index + 1)
            continue
        if isinstance(value, list) and value and all(
            isinstance(item, dict) and set(item) == {"name", "description"}
            for item in value
        ):
            return value
        index = text.find("[", index + 1)
    return None


def real_turn(message_id: str, utterance: str, components, role: str = "member"):
    event = _event(message_id, "u1", utterance, components, scope="bug001-group")
    event.role = role
    event.set_extra(SYS001_TURN_EXTRA, create_snapshot(event, origin="directed"))
    return event


def assert_real_execution_focus(request, utterance: str, sender_id: str):
    focused = [
        part
        for part in request.extra_user_content_parts
        if isinstance(getattr(part, "text", None), str)
        and part.text.startswith(FOCUS_TITLE)
    ]
    assert len(focused) == 1, "现实执行必须产生且只产生一个本轮现实任务焦点临时片段"
    part = focused[0]
    assert getattr(part, "_no_save", None) is True
    text = part.text
    assert ROLE_WORLD_TITLE not in text, "现实任务焦点不得冒充角色世界场景"
    assert utterance in text and sender_id in text
    catalog = first_tool_catalog(text)
    assert catalog is not None, "现实任务焦点必须携带来自 req.func_tool 的工具目录"
    expected = {tool.name: tool.description for tool in request.func_tool.tools}
    assert {entry["name"] for entry in catalog} == set(expected)
    for entry in catalog:
        assert set(entry) == {"name", "description"}
        assert entry["description"] == expected[entry["name"]]
    return part


async def assert_focus_provided_but_not_persisted(request):
    """Official APIs: present in the model context, absent from persistence."""
    assembled = await request.assemble_context()
    assert FOCUS_TITLE in _as_text(assembled)
    runtime = bind_checkpoint_messages([*request.contexts, assembled])
    persisted = dump_messages_with_checkpoints(runtime)
    assert FOCUS_TITLE not in _as_text(persisted)


async def test_real_execution_bare_mention_builds_task_focus():
    prov = FakeProvider(REAL_EXEC)
    plug = mk(provider=prov, config=cfg_member(["send_message_to_user"]))
    request, _, _ = req()
    utterance = "不要做别的，单纯艾特他"
    request.prompt = utterance
    request.func_tool = ToolSet(tools=[SEND, SHELL])
    event = real_turn("m-real-mention", utterance, [Plain(utterance)])
    await plug.attach_turn_and_project_capabilities(event, request)
    assert_real_execution_focus(request, utterance, "u1")
    assert request.contexts == HISTORY
    assert request.prompt == utterance
    assert BASE in request.system_prompt
    await assert_focus_provided_but_not_persisted(request)


async def test_real_execution_explicit_qq_task_builds_task_focus():
    prov = FakeProvider(REAL_EXEC)
    plug = mk(provider=prov, config=cfg_member(["send_message_to_user"]))
    request, _, _ = req()
    utterance = "给123456789发送消息"
    request.prompt = utterance
    request.func_tool = ToolSet(tools=[SEND, SHELL])
    event = real_turn("m-real-qq", utterance, [At(qq="bot"), Plain(utterance)])
    await plug.attach_turn_and_project_capabilities(event, request)
    assert_real_execution_focus(request, utterance, "u1")
    assert request.contexts == HISTORY
    assert request.prompt == utterance
    assert BASE in request.system_prompt
    await assert_focus_provided_but_not_persisted(request)
