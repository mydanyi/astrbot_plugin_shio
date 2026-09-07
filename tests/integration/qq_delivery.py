"""Real AstrBot 4.27.4 QQ pipeline regression, run inside its Docker image.

Only external boundaries are fixtures: an OpenAI HTTP endpoint and a OneBot
WebSocket peer. PluginManager, QQ parsing, hooks, runner, scheduler, sending,
and SQLite conversation persistence are the real implementations.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import tzlocal

tzlocal.get_localzone = lambda: ZoneInfo("UTC")

import websockets
from aiohttp import web
from aiocqhttp import Event

from astrbot.core import db_helper, sp
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
from astrbot.core.platform.sources.webchat.webchat_adapter import WebChatAdapter
from astrbot.core.platform.sources.webchat.webchat_queue_mgr import webchat_queue_mgr
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.star.context import Context
from astrbot.core.star.star import star_registry
from astrbot.core.star.star_manager import PluginManager


class Lookup(FunctionTool):
    async def call(self, context, **kwargs):
        return "lookup completed"


class Tools:
    def get_full_tool_set(self):
        return ToolSet([Lookup(name="lookup", description="Fixture lookup", parameters={"type": "object", "properties": {}})])

    def get_builtin_tool(self, tool_type):
        return tool_type()


class Providers:
    def __init__(self, provider):
        self.provider = provider
        self.inst_map = {"fixture": provider}
        self.provider_insts = [provider]
        self.llm_tools = Tools()
        self.personas = []

    async def get_using_provider_async(self, **kwargs):
        if "TEXT_TO_SPEECH" in str(kwargs.get("provider_type")):
            return None
        return self.provider

    async def get_provider_by_id(self, provider_id):
        return self.inst_map.get(provider_id)


class Personas:
    async def resolve_selected_persona(self, **kwargs):
        return None, None, None, False


class Configs:
    def __init__(self, config):
        self.config = config

    def get_conf(self, umo):
        return self.config


class FixtureContext(Context):
    def __init__(self, config, provider, adapter, queue):
        super().__init__(
            queue, config, SimpleNamespace(), Providers(provider),
            SimpleNamespace(platform_insts=[adapter]), ConversationManager(db_helper),
            SimpleNamespace(), Personas(), Configs(config), SimpleNamespace(), SimpleNamespace(),
        )
        self.fixture_provider = provider

    def get_provider_by_id(self, provider_id):
        return self.fixture_provider if provider_id == "fixture" else None


class Endpoint:
    def __init__(self):
        self.responses = []
        self.requests = []

    async def handle(self, request):
        payload = await request.json()
        self.requests.append(payload)
        assert self.responses, "unexpected provider request"
        message = self.responses.pop(0)
        response = {
            "id": "fixture", "object": "chat.completion", "created": 0,
            "model": "fixture", "choices": [{"index": 0, "message": message,
            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        if payload.get("stream"):
            delta = copy.deepcopy(message)
            for index, call in enumerate(delta.get("tool_calls", [])):
                call["index"] = index
            chunks = [
                {"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": "fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": "fixture", "choices": [{"index": 0, "delta": {}, "finish_reason": response["choices"][0]["finish_reason"]}], "usage": response["usage"]},
            ]
            body = "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return web.Response(text=body, content_type="text/event-stream")
        return web.json_response(response)


def settings():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["admins_id"] = ["100"]
    config["wake_prefix"] = ["/fixture"]
    config["plugin_set"] = ["*"]
    config["platform_settings"].update({"reply_with_mention": False, "reply_with_quote": False})
    config["platform_settings"]["segmented_reply"].update({"enable": False, "interval": "0,0"})
    config["provider_settings"].update({
        "enable": True, "default_provider_id": "fixture", "identifier": "fixture",
        "streaming_response": False, "buffer_intermediate_messages": False,
        "show_tool_use_status": False, "show_tool_call_result": False,
        "display_reasoning_text": False, "request_max_retries": 1, "max_agent_step": 5,
        "proactive_capability": {"add_cron_tools": False},
    })
    config["provider_tts_settings"]["enable"] = False
    config["provider_ltm_settings"].update({"group_icl_enable": False, "group_message_history_enable": False})
    config["t2i"] = False
    config["content_safety"]["also_use_in_response"] = False
    return config


async def peer_connect():
    for _ in range(60):
        try:
            return await websockets.connect("ws://127.0.0.1:18081/ws/api", additional_headers={"X-Client-Role": "api", "X-Self-ID": "42"}, proxy=None)
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError("fixture OneBot peer did not connect")


async def capture_sends(ws, captures):
    try:
        async for raw in ws:
            payload = json.loads(raw)
            captures.append(payload)
            data = {"message_id": len(captures)}
            if payload.get("action") == "get_msg":
                data = {"time": int(time.time()), "self_id": 42, "message_type": "group",
                        "sub_type": "normal", "message_id": 9000, "user_id": 42,
                        "group_id": 10001, "sender": {"user_id": 42, "nickname": "bot"},
                        "message": [{"type": "text", "data": {"text": "之前的一句话。"}}],
                        "raw_message": "之前的一句话。"}
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": payload["echo"]}))
    except websockets.exceptions.ConnectionClosed:
        pass


async def event_from_qq(adapter, *, private, number, sender="100", text="醒醒", mention=True, reply=False):
    segments = [{"type": "text", "data": {"text": text}}]
    if not private and mention:
        segments.insert(0, {"type": "at", "data": {"qq": "42"}})
    if reply:
        segments.insert(0, {"type": "reply", "data": {"id": "9000"}})
    raw = {"time": int(time.time()), "self_id": 42, "post_type": "message",
           "message_type": "private" if private else "group", "sub_type": "friend" if private else "normal",
           "message_id": number, "user_id": int(sender), "message": segments,
           "raw_message": text, "font": 0, "sender": {"user_id": int(sender), "nickname": "tester", "role": "member"}}
    if not private:
        raw["group_id"] = 10001
    message = await adapter.convert_message(Event(raw))
    assert message is not None
    return adapter.create_event(message)


def text_bubbles(captures):
    texts = ["".join(part["data"]["text"] for part in payload.get("params", {}).get("message", []) if part.get("type") == "text")
             for payload in captures if payload.get("action") in {"send_group_msg", "send_private_msg", "send_msg"}]
    return [text for text in texts if text]


async def main():
    await db_helper.initialize()
    await sp.initialize()
    config = settings()
    plugin_config = {"sys001": {
        "ingress": {"group_allowed_scopes": ["10001"], "private_allowed_sender_ids": ["101"]},
        "group": {"continuous_window_enabled": False, "natural_participation_enabled": False, "name_wake_mode": "direct"},
        "final_review": {"mode": "off"},
        "presentation": {"text_component_mode": "plugin", "text_component_min_segments": 1, "text_component_max_segments": 3, "bubble_send_min_wait_seconds": 0, "bubble_send_max_wait_seconds": 0},
    }}
    Path("data/config").mkdir(parents=True, exist_ok=True)
    Path("data/config/astrbot_plugin_shio_config.json").write_text(json.dumps(plugin_config), encoding="utf-8")
    with_meme = os.environ.get("QQ_FIXTURE_MEME") == "1"
    if with_meme:
        image_dir = Path("data/plugin_data/meme_manager/packs/builtin-default/memes/happy")
        image_dir.mkdir(parents=True, exist_ok=True)
        image_dir.joinpath("fixture.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/zg7WAAAAAElFTkSuQmCC"))
        Path("data/config/meme_manager_config.json").write_text(json.dumps({"generation": {"trigger": {"scope": "chat_and_plugin_llm"}, "emotion": {"llm": {"enabled": False}}, "message": {"enable_mixed": False}}, "semantic": {"enabled": False}}), encoding="utf-8")
    queue = asyncio.Queue()
    adapter = AiocqhttpAdapter({"id": "fixture-qq", "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 18081}, config["platform_settings"], queue)
    bot_task = asyncio.create_task(adapter.bot.run_task("127.0.0.1", 18081))
    endpoint = Endpoint()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", endpoint.handle)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 18082)
    await site.start()
    failures = []
    try:
        provider = ProviderOpenAIOfficial({"id": "fixture", "type": "openai_chat_completion", "key": ["fixture-key"], "model": "fixture", "api_base": "http://127.0.0.1:18082/v1", "max_context_tokens": 0}, config["provider_settings"])
        context = FixtureContext(config, provider, adapter, queue)
        manager = PluginManager(context, config)
        if with_meme:
            loaded, error = await manager.load(specified_dir_name="meme_manager")
            assert loaded, error
        loaded, error = await manager.load(specified_dir_name="astrbot_plugin_shio")
        assert loaded, error
        # Production loads installed plugins before the internal context plugin.
        # Equal-priority Shio must not start its Agent ahead of ICL collection.
        loaded, error = await manager.load(specified_dir_name="astrbot")
        assert loaded, error
        shio = next(item.star_cls for item in star_registry if item.root_dir_name == "astrbot_plugin_shio")
        pipeline_context = PipelineContext(astrbot_config=config, plugin_manager=manager, astrbot_config_id="fixture")
        scheduler = PipelineScheduler(pipeline_context)
        scheduler.stages = [WakingCheckStage(), ProcessStage(), ResultDecorateStage(), RespondStage()]
        for stage in scheduler.stages:
            await stage.initialize(pipeline_context)
        async with await peer_connect() as ws:
            captures = []
            capture_task = asyncio.create_task(capture_sends(ws, captures))
            scenarios = [("group_at", False, False), ("private", True, False), ("group_tool", False, True), ("private_tool", True, True), ("production_stream_setting", False, True), ("buffered_tool", False, True), ("reply_missing_tool", False, True), ("reply_valid_tool", False, True), ("four_lines", False, False)]
            if with_meme:
                scenarios.append(("unresolved_meme_marker", False, False))
                scenarios.append(("private_unicode_meme", True, False))
            for number, (name, private, tool) in enumerate(scenarios, 1):
                config["provider_settings"]["streaming_response"] = name == "production_stream_setting"
                config["provider_settings"]["unsupported_streaming_strategy"] = "turn_off"
                config["provider_settings"]["buffer_intermediate_messages"] = name == "buffered_tool"
                await scheduler.stages[1].initialize(pipeline_context)
                captures.clear()
                endpoint.requests.clear()
                final = "醒啦！\n刚在听你们聊天~\n怎么啦？" + ("\n&&happy&&" if with_meme else "")
                if name == "private_unicode_meme":
                    final = '真棒！\n蛋糕备好了——你说“太好了”👩\u200d💻。\n继续加油！\n&&happy&&'
                if name == "unresolved_meme_marker":
                    final += "\n&&meme:04ee85d7815d&&"
                if name == "four_lines":
                    final = "一句。\n二句。\n三句。\n四句。\n\n" + ("&&happy&&" if with_meme else "")
                draft = {"role": "assistant", "content": "醒啦，这是工具前的草稿。", "tool_calls": [{"id": "lookup1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}
                if name == "reply_missing_tool":
                    draft["tool_calls"][0]["function"]["name"] = "search_memes"
                endpoint.responses = ([draft] if tool else []) + [{"role": "assistant", "content": final}]
                event = await event_from_qq(adapter, private=private, number=number,
                                          reply=name.startswith("reply_"),
                                          mention=not name.startswith("reply_"))
                await asyncio.wait_for(scheduler.execute(event), timeout=25)
                bubbles = text_bubbles(captures)
                cid = await context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
                conv = await context.conversation_manager.get_conversation(event.unified_msg_origin, cid)
                history = json.loads(conv.history)
                media_sends = [p for p in captures if any(c.get("type") == "image" for c in p.get("params", {}).get("message", []))]
                result = {"scenario": name, "provider_requests": len(endpoint.requests), "bubbles": bubbles, "images": len(media_sends),
                          "history_rows": len(history), "stream": [r.get("stream") for r in endpoint.requests], "stopped": event.is_stopped()}
                print("CASE " + json.dumps(result, ensure_ascii=False), flush=True)
                want = ["醒啦！", "刚在听你们聊天~", "怎么啦？"]
                if name == "private_unicode_meme":
                    want = ['真棒！', '蛋糕备好了——你说“太好了”👩\u200d💻。', '继续加油！']
                if name == "four_lines":
                    want = ['一句。', '二句。', '三句。\n四句。']
                if bubbles != want or endpoint.responses:
                    failures.append(name)
                if with_meme:
                    if len(media_sends) != 1 or captures[-1] is not media_sends[0]:
                        failures.append(name + "_meme")

            if with_meme:
                meme = next(item.star_cls for item in star_registry if item.root_dir_name == 'meme_manager')
                meme.emotion_llm_enabled = True
                meme.emotion_llm_context_turns = 2
                meme.emotion_llm_provider_id = 'fixture'
                captures.clear()
                endpoint.requests.clear()
                current = '请写三句庆祝的话，只要文字，不要图片和表情包。'
                endpoint.responses = [
                    {'role': 'assistant', 'content': '真棒！\n成功了——太开心了！\n继续加油！'},
                    {'role': 'assistant', 'content': '{"emotions":[]}'},
                ]
                event = await event_from_qq(adapter, private=True, number=29, text=current)
                await asyncio.wait_for(scheduler.execute(event), timeout=25)
                auxiliary = endpoint.requests[-1]['messages']
                auxiliary_text = '\n'.join(m.get('content', '') for m in auxiliary if isinstance(m.get('content'), str))
                cid = await context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
                conv = await context.conversation_manager.get_conversation(event.unified_msg_origin, cid)
                ok = (len(endpoint.requests) == 2 and current in auxiliary_text
                      and [part.strip() for part in text_bubbles(captures)] == ['真棒！', '成功了——太开心了！', '继续加油！']
                      and not any(c.get('type') == 'image' for p in captures for c in p.get('params', {}).get('message', []))
                      and json.dumps(json.loads(conv.history), ensure_ascii=False).count(current) == 1)
                print('CASE ' + json.dumps({'scenario': 'meme_auxiliary_current_user', 'ok': ok,
                      'current_user_reaches_selector': current in auxiliary_text,
                      'bubbles': text_bubbles(captures),
                      'current_user_history_count': json.dumps(json.loads(conv.history), ensure_ascii=False).count(current)}, ensure_ascii=False), flush=True)
                if not ok:
                    failures.append('meme_auxiliary_current_user')
                meme.emotion_llm_enabled = False

            # A private batch must keep both messages just like a group batch;
            # a group's later speaker may differ from the first one.
            config["provider_settings"]["buffer_intermediate_messages"] = False
            await scheduler.stages[1].initialize(pipeline_context)
            shio.config["sys001"]["group"].update({"continuous_window_enabled": True, "continuous_window_seconds": 1})
            for private in (False, True):
                endpoint.requests.clear()
                endpoint.responses = [{"role": "assistant", "content": "收到两句话。"}]
                first = await event_from_qq(adapter, private=private, number=30 + int(private) * 2, sender="101" if private else "100", text="批次第一句")
                second = await event_from_qq(adapter, private=private, number=31 + int(private) * 2, sender="101", text="批次第二句", mention=False)
                first_task = asyncio.create_task(scheduler.execute(first))
                await asyncio.sleep(0.15)
                second_task = asyncio.create_task(scheduler.execute(second))
                await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=15)
                request_text = json.dumps(endpoint.requests, ensure_ascii=False)
                merged = len(endpoint.requests) == 1 and "批次第一句" in request_text and "批次第二句" in request_text
                print("CASE " + json.dumps({"scenario": "private_batch" if private else "group_batch", "merged": merged}), flush=True)
                if not merged:
                    failures.append("private_batch" if private else "group_batch")
            shio.config["sys001"]["group"]["continuous_window_enabled"] = False

            shio.config["sys001"]["group"].update({
                "natural_participation_enabled": True, "natural_group_scopes": ["10001"],
                "natural_reply_cooldown_seconds": 1, "natural_window_max_replies": 20,
            })
            for number, sender in ((40, "101"), (41, "100")):
                if number == 41:
                    await asyncio.sleep(1.1)
                captures.clear()
                endpoint.requests.clear()
                endpoint.responses = [{"role": "assistant", "content": '{"decision":"REPLY"}'}, {"role": "assistant", "content": "这个话题我也喜欢。"}]
                event = await event_from_qq(adapter, private=False, number=number, sender=sender, text="普通聊天话题", mention=False)
                await asyncio.wait_for(scheduler.execute(event), timeout=15)
                ok = len(endpoint.requests) == 2 and text_bubbles(captures) == ["这个话题我也喜欢。"]
                case = "natural_member" if sender == "101" else "natural_master"
                print("CASE " + json.dumps({"scenario": case, "ok": ok, "provider_requests": len(endpoint.requests)}), flush=True)
                if not ok:
                    failures.append(case)
            for number, mention in ((42, False), (43, True)):
                captures.clear()
                endpoint.requests.clear()
                endpoint.responses = [{"role": "assistant", "content": "叫我啦。"}] if mention else []
                event = await event_from_qq(adapter, private=False, number=number, sender="101", text="再说一句", mention=mention)
                await asyncio.wait_for(scheduler.execute(event), timeout=15)
                ok = len(endpoint.requests) == int(mention) and bool(text_bubbles(captures)) == mention
                case = "direct_during_cooldown" if mention else "natural_cooldown"
                print("CASE " + json.dumps({"scenario": case, "ok": ok}), flush=True)
                if not ok:
                    failures.append(case)
            shio.config["sys001"]["group"]["natural_participation_enabled"] = False

            config["provider_settings"]["buffer_intermediate_messages"] = False
            config["provider_ltm_settings"]["group_icl_enable"] = True
            await scheduler.stages[1].initialize(pipeline_context)
            ambient = await event_from_qq(adapter, private=False, number=20, sender="101", text="山顶露营上下文测试", mention=False)
            await asyncio.wait_for(scheduler.execute(ambient), timeout=10)
            endpoint.requests.clear()
            endpoint.responses = [{"role": "assistant", "content": "记得呀。"}]
            current = await event_from_qq(adapter, private=False, number=21, text="刚才说到哪了")
            await asyncio.wait_for(scheduler.execute(current), timeout=15)
            cid = await context.conversation_manager.get_curr_conversation_id(current.unified_msg_origin)
            conv = await context.conversation_manager.get_conversation(current.unified_msg_origin, cid)
            provider_text = json.dumps(endpoint.requests, ensure_ascii=False)
            history_text = json.dumps(json.loads(conv.history), ensure_ascii=False)
            icl_result = {"scenario": "official_icl", "seen_by_provider": "山顶露营上下文测试" in provider_text, "saved_to_conversation": "山顶露营上下文测试" in history_text}
            print("CASE " + json.dumps(icl_result), flush=True)
            if not icl_result["seen_by_provider"] or icl_result["saved_to_conversation"]:
                failures.append("official_icl")

            shio.config["sys001"]["group"].update({"continuous_window_enabled": True, "continuous_window_seconds": 1})
            endpoint.requests.clear()
            endpoint.responses = [{"role": "assistant", "content": "收到大家的早餐安排。"}]
            captures.clear()
            breakfast = [
                await event_from_qq(adapter, private=False, number=50, sender="100", text="我想喝豆浆", mention=True),
                await event_from_qq(adapter, private=False, number=51, sender="101", text="我想吃油条", mention=False),
                await event_from_qq(adapter, private=False, number=52, sender="101", text="还要煎饼", mention=False),
            ]
            tasks = []
            for item in breakfast:
                tasks.append(asyncio.create_task(scheduler.execute(item)))
                await asyncio.sleep(0.15)
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
            delivered = json.dumps(endpoint.requests, ensure_ascii=False)
            conv = await context.conversation_manager.get_conversation(current.unified_msg_origin, cid)
            persisted = json.dumps(json.loads(conv.history), ensure_ascii=False)
            batch_ok = (len(endpoint.requests) == 1 and all(word in delivered for word in ("豆浆", "油条", "煎饼"))
                        and "豆浆" not in persisted and "油条" not in persisted
                        and text_bubbles(captures) == ["收到大家的早餐安排。"])
            print("CASE " + json.dumps({"scenario": "official_icl_cross_sender_batch", "ok": batch_ok,
                  "provider_requests": len(endpoint.requests), "earlier_seen": "豆浆" in delivered and "油条" in delivered}), flush=True)
            if not batch_ok:
                failures.append("official_icl_cross_sender_batch")
            shio.config["sys001"]["group"]["continuous_window_enabled"] = False

            # QQ friend/typing notices can become empty private events. They
            # must not reserve a batch whose Agent never starts/completes.
            captures.clear()
            endpoint.requests.clear()
            endpoint.responses = [{"role": "assistant", "content": "私聊没有被空事件堵住。"}]
            empty_private = await event_from_qq(adapter, private=True, number=60, sender="101", text="")
            await asyncio.wait_for(scheduler.execute(empty_private), timeout=5)
            empty_request_count = len(endpoint.requests)
            next_private = await event_from_qq(adapter, private=True, number=61, sender="101", text="加好友后正常聊天")
            timed_out = False
            try:
                await asyncio.wait_for(scheduler.execute(next_private), timeout=5)
            except TimeoutError:
                timed_out = True
            empty_ok = (not timed_out and empty_request_count == 0 and len(endpoint.requests) == 1
                        and text_bubbles(captures) == ["私聊没有被空事件堵住。"])
            print("CASE " + json.dumps({"scenario": "empty_private_then_text", "ok": empty_ok, "timed_out": timed_out}), flush=True)
            if not empty_ok:
                failures.append("empty_private_then_text")

            # Preserve the user's working comparison path: native WebChat
            # streaming goes through RespondStage before Agent completion.
            config["provider_settings"]["streaming_response"] = True
            await scheduler.stages[1].initialize(pipeline_context)
            web_adapter = WebChatAdapter({"id": "webchat"}, config["platform_settings"], queue)
            web_message = await web_adapter.convert_message(("100", "fixture-web", {"message_id": "web-1", "message": [{"type": "plain", "text": "网页测试"}]}))
            web_event = web_adapter.create_event(web_message)
            back_queue = webchat_queue_mgr.get_or_create_back_queue("web-1")
            endpoint.requests.clear()
            endpoint.responses = [{"role": "assistant", "content": "网页回复正常。"}]
            await asyncio.wait_for(scheduler.execute(web_event), timeout=15)
            outputs = []
            while not back_queue.empty():
                outputs.append(back_queue.get_nowait())
            web_ok = len(endpoint.requests) == 1 and any(p.get("type") == "plain" and p.get("data") == "网页回复正常。" for p in outputs)
            print("CASE " + json.dumps({"scenario": "webchat_stream", "ok": web_ok}), flush=True)
            if not web_ok:
                failures.append("webchat_stream")
            webchat_queue_mgr.remove_back_queue("web-1")

            # An ordinary user can mention these words; reloading a plugin must
            # not delete that user's persisted message by substring matching.
            seed = [{"role": "user", "content": "请解释 official_platform_history 这个术语"}, {"role": "assistant", "content": "这是一个标识符。"}]
            scope = "fixture-qq:FriendMessage:999"
            cid = await context.conversation_manager.new_conversation(scope, content=seed)
            loaded, error = await manager.reload("astrbot_plugin_shio")
            assert loaded, error
            conv = await context.conversation_manager.get_conversation(scope, cid)
            unchanged = json.loads(conv.history) == seed
            print("CASE " + json.dumps({"scenario": "reload_preserves_conversation", "unchanged": unchanged}), flush=True)
            if not unchanged:
                failures.append("reload_preserves_conversation")
            capture_task.cancel()
            await asyncio.gather(capture_task, return_exceptions=True)
    finally:
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)
        await server.cleanup()
        await db_helper.engine.dispose()
    assert not failures, f"QQ final delivery failed: {failures}"
    print("QQ_PIPELINE_PASS", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
