from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


class FakeLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeToolSet:
    def __init__(self, tools=None):
        self.tools = list(tools or [])

    def empty(self):
        return not self.tools


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeTextPart:
    def __init__(self, text):
        self.text = text
        self.is_temp = False

    def mark_as_temp(self):
        self.is_temp = True
        return self


class FakeFilter:
    @staticmethod
    def custom_filter(*args, **kwargs):
        return lambda func: func

    @staticmethod
    def on_llm_request(**kwargs):
        return lambda func: func

    @staticmethod
    def on_llm_response(**kwargs):
        return lambda func: func

    @staticmethod
    def on_astrbot_loaded(**kwargs):
        return lambda func: func

    @staticmethod
    def on_decorating_result(**kwargs):
        return lambda func: func


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeStarTools:
    data_dir = None

    @classmethod
    def get_data_dir(cls, plugin_name=None):
        return cls.data_dir


class FakeEvent:
    def __init__(self, sender_id, message, group_id="123"):
        self.sender_id = sender_id
        self.message = message
        self.group_id = group_id
        self.extras = {}
        self.sent = []
        self._result = None
        self.unified_msg_origin = "aiocqhttp:GroupMessage:123"
        self.created_at = time.time()
        self.is_at_or_wake_command = False

    def get_sender_id(self):
        return self.sender_id

    def get_sender_name(self):
        return "测试用户"

    def get_group_id(self):
        return self.group_id

    def get_session_id(self):
        return self.group_id or self.sender_id

    def get_platform_id(self):
        return "亚托莉"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_self_id(self):
        return "bot-10000"

    def get_message_str(self):
        return self.message

    def get_message_outline(self):
        return self.message

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        return self.extras.get(key, default)

    def get_result(self):
        return self._result

    def plain_result(self, text):
        return types.SimpleNamespace(chain=[types.SimpleNamespace(text=text)])

    async def send(self, result):
        self.sent.append("".join(comp.text for comp in result.chain if hasattr(comp, "text")))

    def request_llm(self, prompt, tool_set=None, contexts=None, **kwargs):
        request = FakeRequest(prompt)
        request.func_tool = tool_set
        request.contexts = list(contexts or [])
        return request


class FakeMessageChain:
    def __init__(self):
        self.chain = []

    def message(self, text):
        self.chain.append(types.SimpleNamespace(text=text))
        return self


class FakeRequest:
    def __init__(self, prompt):
        self.prompt = prompt
        self.contexts = [
            {"role": "user", "content": "早上好"},
            {"role": "assistant", "content": "早呀"},
        ]
        self.system_prompt = "原始长人设"
        self.extra_user_content_parts = []
        self.func_tool = FakeToolSet([FakeTool("dangerous_tool")])
        self.tool_calls_result = None
        self.image_urls = []
        self.audio_urls = []


class FakeResponse:
    def __init__(self, text, role="assistant"):
        self.role = role
        self.completion_text = text


def fake_tool_call_results(*names):
    calls = [
        types.SimpleNamespace(function=types.SimpleNamespace(name=name))
        for name in names
    ]
    return [types.SimpleNamespace(tool_calls_info=types.SimpleNamespace(tool_calls=calls))]


class FakeProvider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return FakeResponse(value)


class FakeToolManager:
    def __init__(self, tools=None):
        self.tool_set = FakeToolSet(tools or [])

    def get_full_tool_set(self):
        return self.tool_set


class FakeContext:
    def __init__(self, provider, embeddings=None, rerankers=None, global_tools=None):
        self.provider = provider
        self.embeddings = list(embeddings or [])
        self.registered_stars = {}
        self.tool_manager = FakeToolManager(global_tools)
        self.provider_manager = types.SimpleNamespace(
            embedding_provider_insts=self.embeddings,
            rerank_provider_insts=list(rerankers or []),
        )
        self.proactive_messages = []

    def get_using_provider(self, umo=None):
        return self.provider

    def get_provider_by_id(self, provider_id):
        return None

    def get_all_embedding_providers(self):
        return self.embeddings

    def get_llm_tool_manager(self):
        return self.tool_manager

    def get_registered_star(self, name):
        return self.registered_stars.get(name)

    async def send_message(self, session, message_chain):
        self.proactive_messages.append((session, message_chain))
        return True


class FakeConfig(dict):
    def __init__(self, *args, schema=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.schema = schema


class TypedProvider:
    def __init__(self, provider_id, model):
        self.provider_id = provider_id
        self.model = model

    def meta(self):
        return types.SimpleNamespace(id=self.provider_id, model=self.model)


def install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    provider = types.ModuleType("astrbot.api.provider")
    star = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    agent_message = types.ModuleType("astrbot.core.agent.message")
    api.AstrBotConfig = dict
    api.ToolSet = FakeToolSet
    api.logger = FakeLogger()
    event.AstrMessageEvent = FakeEvent
    event.MessageChain = FakeMessageChain
    event.filter = FakeFilter()
    provider.LLMResponse = FakeResponse
    provider.ProviderRequest = FakeRequest
    star.Context = FakeContext
    star.Star = FakeStar
    star.StarTools = FakeStarTools
    agent_message.TextPart = FakeTextPart
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.provider": provider,
            "astrbot.api.star": star,
            "astrbot.core": core,
            "astrbot.core.agent": agent,
            "astrbot.core.agent.message": agent_message,
        }
    )


install_astrbot_stubs()
main = importlib.import_module("astrbot_plugin_shio.main")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_scene_rules_are_editable_with_schema_default_and_legacy_migration(self):
        default_plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        self.assertIn(
            "从当前多人话题中间顺势插一句",
            default_plugin._scene_rules(
                "ambient_participation_rules",
                "ambient_participation_extra_prompt",
            ),
        )

        custom_plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {"ambient_participation_rules": "只用一句冷幽默接梗。"},
        )
        self.assertEqual(
            custom_plugin._scene_rules(
                "ambient_participation_rules",
                "ambient_participation_extra_prompt",
            ),
            "只用一句冷幽默接梗。",
        )

        legacy_plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {"ambient_participation_extra_prompt": "本群不聊剧透。"},
        )
        migrated = legacy_plugin._scene_rules(
            "ambient_participation_rules",
            "ambient_participation_extra_prompt",
        )
        self.assertIn("从当前多人话题中间顺势插一句", migrated)
        self.assertIn("本群不聊剧透", migrated)
        self.assertEqual(
            legacy_plugin.config["ambient_participation_rules"],
            migrated,
        )
        self.assertEqual(
            legacy_plugin.config["ambient_participation_extra_prompt"],
            "",
        )

    async def test_nonowner_chat_is_rewritten_and_guarded(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "target": "测试用户",
                "intent": "接受夸奖",
                "reply_act": "嘴硬但开心",
                "emotion": "害羞得意",
                "tone": "傲娇口语",
                "length": "一句",
                "must_include": [],
                "avoid": ["自我介绍"],
                "facts": [],
            },
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json, "才、才没有那么高兴呢……再说一点也可以。"]) 
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "你真可爱")
        request = FakeRequest("你真可爱")

        await plugin.enforce_agent_permission(event, request)
        self.assertTrue(request.func_tool.empty())
        self.assertIn('owner="false"', request.extra_user_content_parts[-1].text)
        await plugin.build_persona_reply(event, request)

        self.assertTrue(event.get_extra(main.SHIO_ACTIVE))
        self.assertEqual(request.prompt, "你真可爱")
        self.assertTrue(request.func_tool.empty())
        self.assertEqual(request.extra_user_content_parts, [])
        self.assertNotIn("原始长人设", request.system_prompt)
        self.assertIn("本轮说话计划", request.system_prompt)
        self.assertIn("被群友夸可爱", request.system_prompt)

        response = FakeResponse("**答案是：**作为一个AI，我永远都是亚托莉。")
        await plugin.guard_persona_reply(event, response)
        self.assertEqual(response.completion_text, "才、才没有那么高兴呢……再说一点也可以。")

    async def test_nonowner_mua_plan_and_reply_are_both_bounded(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "reply_shape": "chat_bubbles",
                "intent": "群友发 mua 调戏卖萌，想得到亲昵回应",
                "reply_act": "用亲昵但保持角色设定的方式回应，并适当回敲一下",
                "emotion": "开心撒娇",
                "tone": "俏皮可爱",
                "must_include": ["mua", "回应亲昵", "保持高性能机器人人设"],
            },
            ensure_ascii=False,
        )
        provider = FakeProvider(
            [
                planner_json,
                "我才不会mua回去呢，熟归熟，边界还是要有的嘛。",
            ]
        )
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "mua")
        request = FakeRequest("mua")

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        plan = event.get_extra(main.SHIO_PLAN)
        self.assertIn("保持边界", plan["intent"])
        self.assertIn("不回亲", plan["reply_act"])
        self.assertNotIn("mua", plan["must_include"])
        self.assertIn("群友边界", request.system_prompt)
        self.assertIn("禁止回亲或回应 mua", request.system_prompt)

        response = FakeResponse("不过……mua回去一下也不是不行啦。")
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(
            response.completion_text,
            "我才不会mua回去呢，熟归熟，边界还是要有的嘛。",
        )
        self.assertIn("必须重新建立边界", provider.calls[-1]["prompt"])

    async def test_nonowner_repeated_intimacy_violation_uses_boundary_fallback(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "intent": "回应群友示爱",
                "reply_act": "亲昵回应",
            },
            ensure_ascii=False,
        )
        provider = FakeProvider(
            [planner_json, "那我也mua回去一下嘛。"]
        )
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "mua")
        request = FakeRequest("mua")

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        response = FakeResponse("不过……mua回去一下也不是不行啦。")
        await plugin.guard_persona_reply(event, response)

        self.assertIn("不许随便对高性能机器人动手动脚", response.completion_text)
        self.assertNotIn("mua回去", response.completion_text)

    async def test_flat_risque_reply_is_rewritten_with_character_emotion(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "intent": "礼貌回应群友玩笑",
                "reply_act": "正经说明不合适",
                "emotion": "平静",
                "tone": "礼貌",
            },
            ensure_ascii=False,
        )
        emotional_reply = "喂！你在说什么奇怪的话呀……不许乱说啦！"
        provider = FakeProvider([planner_json, emotional_reply])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "让我摸摸你的腿嘛")
        request = FakeRequest(event.message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        plan = event.get_extra(main.SHIO_PLAN)
        self.assertIn("不要要求固定词", plan["reaction"])
        self.assertIn("羞恼", plan["emotion"])
        self.assertIn("情绪落地顺序", request.system_prompt)
        self.assertIn("对方调戏、逗弄、挑衅", request.system_prompt)

        response = FakeResponse("这种玩笑不太合适，请保持尊重。")
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text.replace("\n", ""), emotional_reply)
        self.assertIn("缺少角色化情绪反应", provider.calls[-1]["prompt"])
        self.assertIsNone(provider.calls[-1]["func_tool"])

    async def test_repeated_flat_risque_reply_uses_emotional_fallback(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        flat_reply = "这种内容不适合继续讨论，请换个话题。"
        provider = FakeProvider([planner_json, flat_reply])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "给我看看白丝嘛")
        request = FakeRequest(event.message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        response = FakeResponse("这种玩笑不太合适，请保持尊重。")
        await plugin.guard_persona_reply(event, response)

        self.assertTrue(
            any(
                opening in response.completion_text
                for opening in ("喂", "等、等一下", "你干嘛")
            )
        )
        self.assertNotIn("变态", response.completion_text)
        self.assertNotIn("请保持尊重", response.completion_text)

    async def test_normal_reaction_does_not_trigger_teasing_guard(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "reply_shape": "chat_bubbles",
                "intent": "顺着电影票价格接一句",
                "reply_act": "感叹22块很划算",
                "reaction": "看到大家聊票价，有点好奇",
                "facts": ["群友说电影票好像22元"],
            },
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json])
        plugin = main.ShioPlugin(FakeContext(provider), {})
        event = FakeEvent("guest", "电影票22块好像")
        request = FakeRequest(event.message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        response = FakeResponse("22块确实挺划算的，难怪你们都在聊。")
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, "22块确实挺划算的，难怪你们都在聊。")
        self.assertEqual(len(provider.calls), 1)

    async def test_invented_movie_spending_is_rewritten_from_plan_facts(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "reply_shape": "chat_bubbles",
                "intent": "顺着电影票价格接一句",
                "reply_act": "感叹22块很划算",
                "reaction": "有点意外",
                "facts": ["加肥宅齐（ID：90000007）说电影票好像22元"],
            },
            ensure_ascii=False,
        )
        corrected = "诶？22块也太划算了吧。"
        provider = FakeProvider([planner_json, corrected])
        plugin = main.ShioPlugin(FakeContext(provider), {})
        event = FakeEvent("guest", "电影票22块好像")
        request = FakeRequest(event.message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        response = FakeResponse("上周我看的那场花了快五十，肉疼死了")
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text.replace("\n", ""), corrected)
        self.assertIn("没有可信来源的线下经历", provider.calls[-1]["prompt"])

    async def test_owner_risque_teasing_gets_shy_not_guest_boundary(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        flat_reply = "这是一个玩笑，我已经理解了。"
        provider = FakeProvider([planner_json, flat_reply])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("owner", "主人想亲亲你")
        request = FakeRequest(event.message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        plan = event.get_extra(main.SHIO_PLAN)
        self.assertIn("主人", plan["intent"])
        self.assertNotIn("普通群友", plan["intent"])

        response = FakeResponse("这是一个玩笑，我已经理解了。")
        await plugin.guard_persona_reply(event, response)

        self.assertIn("主人怎么突然说这种话", response.completion_text)
        self.assertNotIn("变态", response.completion_text)

    async def test_embedding_and_reranker_are_dynamic_selectors(self):
        schema = {
            "embedding_provider_id": {"type": "string", "options": [""]},
            "rerank_provider_id": {"type": "string", "options": [""]},
        }
        config = FakeConfig(schema=schema)
        context = FakeContext(
            FakeProvider([]),
            embeddings=[TypedProvider("qwen3-embedding", "qwen3-embedding:0.6b")],
            rerankers=[TypedProvider("bge-reranker", "bge-reranker-v2-m3")],
        )
        plugin = main.ShioPlugin(context, config)
        self.assertEqual(
            schema["embedding_provider_id"]["options"],
            ["", "qwen3-embedding"],
        )
        self.assertEqual(
            schema["rerank_provider_id"]["options"],
            ["", "bge-reranker"],
        )
        self.assertIn("qwen3-embedding:0.6b", schema["embedding_provider_id"]["labels"][1])

    async def test_owner_task_keeps_original_agent_request(self):
        provider = FakeProvider([
            json.dumps({"mode": "task", "intent": "运行程序"}, ensure_ascii=False)
        ])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("owner", "帮我运行这个程序并查看服务器日志")
        request = FakeRequest(event.message)
        original_system = request.system_prompt
        original_tools = request.func_tool

        await plugin.enforce_agent_permission(event, request)
        self.assertIs(request.func_tool, original_tools)
        self.assertIn('owner="true"', request.extra_user_content_parts[-1].text)
        await plugin.build_persona_reply(event, request)

        self.assertFalse(event.get_extra(main.SHIO_ACTIVE))
        self.assertEqual(request.system_prompt, original_system)
        self.assertIs(request.func_tool, original_tools)

    async def test_permission_guard_remains_active_when_reply_chain_is_disabled(self):
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "enabled": False,
                "permission_guard_enabled": True,
                "owner_ids": ["owner"],
            },
        )
        request = FakeRequest("帮我调用服务器工具")
        event = FakeEvent("guest", request.prompt)
        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        self.assertTrue(request.func_tool.empty())
        self.assertIn('owner="false"', request.extra_user_content_parts[-1].text)
        self.assertFalse(event.get_extra(main.SHIO_ACTIVE))

    async def test_nonowner_keeps_only_exact_readonly_allowlist_tools(self):
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "enabled": False,
                "owner_ids": ["owner"],
                "guest_allowed_tools": ["anysearch_search", "anysearch_extract"],
            },
        )
        request = FakeRequest("帮我搜索一下今天的新闻")
        request.func_tool = FakeToolSet(
            [
                FakeTool("anysearch_search"),
                FakeTool("anysearch_batch_search"),
                FakeTool("shell_exec"),
            ]
        )
        event = FakeEvent("guest", request.prompt)

        await plugin.enforce_agent_permission(event, request)

        self.assertEqual(
            [tool.name for tool in request.func_tool.tools],
            ["anysearch_search"],
        )
        self.assertIn('mode="limited_read_only"', request.extra_user_content_parts[-1].text)
        self.assertIn("anysearch_search", request.extra_user_content_parts[-1].text)

    async def test_missing_sender_id_gets_no_allowlisted_tools(self):
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {"guest_allowed_tools": ["anysearch_search"]},
        )
        request = FakeRequest("搜索新闻")
        request.func_tool = FakeToolSet([FakeTool("anysearch_search")])
        event = FakeEvent("", request.prompt)

        await plugin.enforce_agent_permission(event, request)

        self.assertTrue(request.func_tool.empty())
        self.assertIn('tools="disabled"', request.extra_user_content_parts[-1].text)

    async def test_current_information_question_retains_safe_search_for_replyer(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "reply_shape": "long_form",
                "intent": "查询今天的新闻",
                "reply_act": "先查证再回答",
                "use_allowed_tools": True,
            },
            ensure_ascii=False,
        )
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([planner_json])),
            {
                "owner_ids": ["owner"],
                "guest_allowed_tools": ["anysearch_search", "anysearch_extract"],
            },
        )
        request = FakeRequest("今天有什么重要新闻？")
        request.func_tool = FakeToolSet(
            [FakeTool("anysearch_search"), FakeTool("shell_exec")]
        )
        event = FakeEvent("guest", request.prompt)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        self.assertEqual(
            [tool.name for tool in request.func_tool.tools],
            ["anysearch_search"],
        )
        self.assertIn("第一步必须直接调用一个合适的只读工具", request.system_prompt)
        self.assertIn("anysearch_search", request.system_prompt)
        self.assertTrue(event.get_extra(main.SHIO_PLAN)["use_allowed_tools"])

    async def test_readonly_tool_prompt_routes_search_page_and_steam_without_parallel_calls(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "reply_shape": "long_form",
                "intent": "查询 Steam 评价",
                "reply_act": "先查证再回答",
                "use_allowed_tools": True,
            },
            ensure_ascii=False,
        )
        allowed = [
            "anysearch_search",
            "anysearch_extract",
            "web_search",
            "bing_search",
            "crawl_webpage",
            "get_steam_review",
        ]
        plugin = main.ShioPlugin(
            FakeContext(
                FakeProvider([planner_json]),
                global_tools=[FakeTool(name) for name in allowed],
            ),
            {"owner_ids": ["owner"], "guest_allowed_tools": allowed},
        )
        request = FakeRequest("查一下这个游戏最近的 Steam 评价")
        event = FakeEvent("guest", request.prompt)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        self.assertIn("Steam 用户评价", request.system_prompt)
        self.assertIn("优先使用 get_steam_review", request.system_prompt)
        self.assertIn("具体网页", request.system_prompt)
        self.assertIn("不要并行调用多个同类搜索工具", request.system_prompt)

    async def test_explicit_search_overrides_planner_false_and_recovers_global_tool(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "intent": "介绍 Key",
                "reply_act": "直接回答",
                "use_allowed_tools": False,
            },
            ensure_ascii=False,
        )
        plugin = main.ShioPlugin(
            FakeContext(
                FakeProvider([planner_json]),
                global_tools=[FakeTool("anysearch_search")],
            ),
            {"guest_allowed_tools": ["anysearch_search"]},
        )
        request = FakeRequest("联网搜索一下 Key 的资料")
        request.func_tool = FakeToolSet([FakeTool("shell_exec")])
        event = FakeEvent("guest", request.prompt)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        self.assertEqual(
            [tool.name for tool in request.func_tool.tools],
            ["anysearch_search"],
        )
        self.assertTrue(event.get_extra(main.SHIO_PLAN)["use_allowed_tools"])
        self.assertIn("不得声称“已经搜索、已经查过”", request.system_prompt)

    async def test_fake_search_answer_is_blocked_when_model_never_calls_tool(self):
        planner_json = json.dumps(
            {"mode": "chat", "use_allowed_tools": True},
            ensure_ascii=False,
        )
        plugin = main.ShioPlugin(
            FakeContext(
                FakeProvider([planner_json]),
                global_tools=[FakeTool("anysearch_search")],
            ),
            {"guest_allowed_tools": ["anysearch_search"]},
        )
        request = FakeRequest("搜索一下 Key")
        event = FakeEvent("guest", request.prompt)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        response = FakeResponse("我已经查过了，答案是……")
        await plugin.guard_persona_reply(event, response)

        self.assertIn("没有真的返回结果", response.completion_text)
        self.assertNotIn("已经查过了，答案", response.completion_text)

    async def test_timeless_chat_clears_even_allowlisted_tools(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "intent": "接受夸奖",
                "reply_act": "自然回应",
                "use_allowed_tools": False,
            },
            ensure_ascii=False,
        )
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([planner_json])),
            {"guest_allowed_tools": ["anysearch_search"]},
        )
        request = FakeRequest("你真可爱")
        request.func_tool = FakeToolSet([FakeTool("anysearch_search")])
        event = FakeEvent("guest", request.prompt)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        self.assertTrue(request.func_tool.empty())
        self.assertNotIn("本轮只读资料工具", request.system_prompt)

    async def test_timeless_chat_preserves_active_meme_presentation_tool(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "intent": "被夸后自然回应",
                "reply_act": "嘴硬但开心地回应",
                "use_allowed_tools": False,
            },
            ensure_ascii=False,
        )
        tools = [FakeTool("search_memes"), FakeTool("anysearch_search")]
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([planner_json]), global_tools=tools),
            {"guest_allowed_tools": ["search_memes", "anysearch_search"]},
        )
        request = FakeRequest("你真可爱")
        request.func_tool = FakeToolSet(tools)
        request.system_prompt += (
            "\n<!-- meme_manager_semantic_prompt:start -->\n"
            "本轮必须调用且只能调用一次 search_memes。\n"
            "<!-- meme_manager_semantic_prompt:end -->"
        )
        event = FakeEvent("guest", request.prompt)
        event.set_extra("meme_manager_semantic_active", True)
        event.set_extra("meme_manager_semantic_mode", "tool")

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        self.assertEqual([tool.name for tool in request.func_tool.tools], ["search_memes"])
        self.assertIn("meme_manager_semantic_prompt:start", request.system_prompt)
        self.assertIn("本轮必须调用且只能调用一次 search_memes", request.system_prompt)
        self.assertNotIn("本轮只读资料工具", request.system_prompt)
        self.assertFalse(event.get_extra(main.SHIO_PLAN)["use_allowed_tools"])

        response = FakeResponse("才、才没有因为你夸我就高兴呢。")
        await plugin.guard_persona_reply(event, response)
        self.assertNotIn("联网模块", response.completion_text)

    async def test_nonowner_cannot_restore_meme_tool_outside_exact_allowlist(self):
        planner_json = json.dumps(
            {"mode": "chat", "use_allowed_tools": False},
            ensure_ascii=False,
        )
        plugin = main.ShioPlugin(
            FakeContext(
                FakeProvider([planner_json]),
                global_tools=[FakeTool("search_memes")],
            ),
            {"guest_allowed_tools": ["anysearch_search"]},
        )
        request = FakeRequest("你好")
        request.func_tool = FakeToolSet([FakeTool("search_memes")])
        request.system_prompt += (
            "\n<!-- meme_manager_semantic_prompt:start -->\n"
            "必须调用 search_memes。\n"
            "<!-- meme_manager_semantic_prompt:end -->"
        )
        event = FakeEvent("guest", request.prompt)
        event.set_extra("meme_manager_semantic_active", True)
        event.set_extra("meme_manager_semantic_mode", "tool")

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        self.assertTrue(request.func_tool.empty())
        self.assertNotIn("meme_manager_semantic_prompt:start", request.system_prompt)

    async def test_meme_call_does_not_satisfy_required_factual_search(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "reply_shape": "long_form",
                "intent": "查询今天新闻",
                "reply_act": "联网后回答",
                "use_allowed_tools": True,
            },
            ensure_ascii=False,
        )
        tools = [FakeTool("search_memes"), FakeTool("anysearch_search")]
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([planner_json]), global_tools=tools),
            {"guest_allowed_tools": ["search_memes", "anysearch_search"]},
        )
        request = FakeRequest("搜索一下今天的新闻")
        request.func_tool = FakeToolSet(tools)
        request.system_prompt += (
            "\n<!-- meme_manager_semantic_prompt:start -->\n"
            "本轮必须调用且只能调用一次 search_memes。\n"
            "<!-- meme_manager_semantic_prompt:end -->"
        )
        event = FakeEvent("guest", request.prompt)
        event.set_extra("meme_manager_semantic_active", True)
        event.set_extra("meme_manager_semantic_mode", "tool")

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        self.assertEqual(
            [tool.name for tool in request.func_tool.tools],
            ["search_memes", "anysearch_search"],
        )

        request.tool_calls_result = fake_tool_call_results("search_memes")
        response = FakeResponse("我已经查过了，今天的新闻是……")
        await plugin.guard_persona_reply(event, response)
        self.assertIn("没有真的返回结果", response.completion_text)

    async def test_factual_search_call_satisfies_guard_even_with_meme_tool_available(self):
        planner_json = json.dumps(
            {"mode": "chat", "use_allowed_tools": True},
            ensure_ascii=False,
        )
        tools = [FakeTool("search_memes"), FakeTool("anysearch_search")]
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([planner_json]), global_tools=tools),
            {"guest_allowed_tools": ["search_memes", "anysearch_search"]},
        )
        request = FakeRequest("搜索一下 Key")
        request.func_tool = FakeToolSet(tools)
        request.system_prompt += (
            "\n<!-- meme_manager_semantic_prompt:start -->\n"
            "本轮必须调用且只能调用一次 search_memes。\n"
            "<!-- meme_manager_semantic_prompt:end -->"
        )
        event = FakeEvent("guest", request.prompt)
        event.set_extra("meme_manager_semantic_active", True)
        event.set_extra("meme_manager_semantic_mode", "tool")

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        request.tool_calls_result = fake_tool_call_results(
            "anysearch_search", "search_memes"
        )
        response = FakeResponse("查到了，Key 是一家视觉小说品牌。")
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, "查到了，Key 是一家视觉小说品牌。")

    async def test_recent_chat_reference_cannot_be_misclassified_as_web_search(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "target": "刚刚发言的人",
                "intent": "区分两个群成员",
                "reply_act": "承认并纠正",
                "use_allowed_tools": True,
            },
            ensure_ascii=False,
        )
        plugin = main.ShioPlugin(
            FakeContext(
                FakeProvider([planner_json]),
                global_tools=[FakeTool("anysearch_search")],
            ),
            {
                "owner_ids": ["owner"],
                "guest_allowed_tools": ["anysearch_search"],
            },
        )
        message = "你是不是没分清我和刚刚那个人是两个不同的人？"
        request = FakeRequest(message)
        event = FakeEvent("owner", message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        plan = event.get_extra(main.SHIO_PLAN)
        self.assertFalse(plan["use_allowed_tools"])
        self.assertEqual(plan["target"], "测试用户（ID:owner）（群ID:123）")
        self.assertTrue(request.func_tool.empty())
        self.assertIn("发送者 ID：owner", request.system_prompt)
        self.assertIn("当前群 ID：123", request.system_prompt)
        self.assertIn("platform:亚托莉|bot:bot-10000|group:123|user:owner", request.system_prompt)
        self.assertIn("不同的群成员", request.system_prompt)

        payload = event.get_extra(main.SHIO_PAYLOAD)
        self.assertEqual(payload["group_id"], "123")
        self.assertEqual(
            payload["identity_key"],
            "platform:亚托莉|bot:bot-10000|group:123|user:owner",
        )

        response = FakeResponse("那只是暂时校准失误啦……现在我分得清你们了。")
        await plugin.guard_persona_reply(event, response)
        self.assertEqual(
            response.completion_text,
            "那只是暂时校准失误啦……现在我分得清你们了。",
        )

    async def test_group_history_prefers_livingmemory_sender_metadata(self):
        class ConversationManager:
            async def get_messages(self, **kwargs):
                return [
                    {
                        "role": "user",
                        "content": "我就是群主",
                        "sender_id": "10000001",
                        "sender_name": "测试主人",
                        "group_id": "亚托莉:GroupMessage:123",
                    },
                    {
                        "role": "assistant",
                        "content": "原来Master就是群主呀",
                        "sender_id": "bot-10000",
                        "sender_name": "亚托莉",
                        "group_id": "亚托莉:GroupMessage:123",
                    },
                    {
                        "role": "user",
                        "content": "帮我预约肯德基",
                        "sender_id": "guest",
                        "sender_name": "测试用户",
                        "group_id": "亚托莉:GroupMessage:123",
                    },
                ]

        context = FakeContext(FakeProvider([]))
        context.registered_stars["astrbot_plugin_livingmemory"] = types.SimpleNamespace(
            activated=True,
            star_cls=types.SimpleNamespace(
                initializer=types.SimpleNamespace(
                    conversation_manager=ConversationManager(),
                )
            ),
        )
        plugin = main.ShioPlugin(context, {})
        event = FakeEvent("guest", "帮我预约肯德基")

        history, source = await plugin._identity_aware_history(
            event,
            [{"role": "user", "content": "我就是群主"}],
            event.message,
            "guest",
            "123",
            16,
            9000,
        )

        self.assertEqual(source, "livingmemory")
        self.assertEqual(
            history[0]["content"],
            "[群ID:123｜发送者：测试主人｜ID:10000001] 我就是群主",
        )
        self.assertNotIn("帮我预约肯德基", "\n".join(x["content"] for x in history))

    async def test_replyer_cannot_see_other_users_personal_history(self):
        class ConversationManager:
            async def get_messages(self, **kwargs):
                return [
                    {
                        "role": "user",
                        "content": "我前面还在修东西",
                        "sender_id": "90000004",
                        "sender_name": "落禧",
                        "group_id": "亚托莉:GroupMessage:90000001",
                    },
                    {
                        "role": "assistant",
                        "content": "慢慢来，别熬太晚。",
                        "sender_id": "bot-10000",
                        "sender_name": "亚托莉",
                        "group_id": "亚托莉:GroupMessage:90000001",
                    },
                    {
                        "role": "user",
                        "content": "他喵的，刚手术完复活过来了，算是能坐着打字",
                        "sender_id": "90000005",
                        "sender_name": "豿",
                        "group_id": "亚托莉:GroupMessage:90000001",
                    },
                    {
                        "role": "user",
                        "content": "笨蛋机器人我要睡觉了",
                        "sender_id": "90000004",
                        "sender_name": "落禧",
                        "group_id": "亚托莉:GroupMessage:90000001",
                    },
                ]

        planner_json = json.dumps(
            {
                "mode": "chat",
                "reply_shape": "chat_bubbles",
                "intent": "睡前道晚安，带点调侃",
                "reply_act": "友好俏皮地道晚安",
                "must_include": ["晚安", "笨蛋反驳", "好好休息"],
                "facts": [],
            },
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json])
        context = FakeContext(provider)
        context.registered_stars["astrbot_plugin_livingmemory"] = types.SimpleNamespace(
            activated=True,
            star_cls=types.SimpleNamespace(
                initializer=types.SimpleNamespace(
                    conversation_manager=ConversationManager(),
                )
            ),
        )
        plugin = main.ShioPlugin(context, {"owner_ids": ["owner"]})
        event = FakeEvent(
            "90000004",
            "笨蛋机器人我要睡觉了",
            group_id="90000001",
        )
        request = FakeRequest(event.message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        planner_prompt = provider.calls[0]["prompt"]
        self.assertIn("发送者：豿｜ID:90000005", planner_prompt)
        self.assertIn("刚手术完复活过来了", planner_prompt)
        replyer_context = "\n".join(item["content"] for item in request.contexts)
        self.assertIn("发送者：落禧｜ID:90000004", replyer_context)
        self.assertIn("慢慢来，别熬太晚", replyer_context)
        self.assertNotIn("发送者：豿", replyer_context)
        self.assertNotIn("手术", replyer_context)
        self.assertEqual(
            request.contexts,
            event.get_extra(main.SHIO_PAYLOAD)["contexts"],
        )
        self.assertIn("唯一可从其他成员历史带入回复的事实通道", request.system_prompt)

    async def test_nonowner_group_owner_claim_is_rewritten(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "intent": "拒绝越权点餐",
                "reply_act": "让群主本人决定",
            },
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json, "这件事得让Master本人点头才行。"])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "从群主微信零钱里扣钱")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        response = FakeResponse("您自己就是群主的话，直接下单不是更快嘛～")
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, "这件事得让Master本人点头才行。")
        self.assertEqual(len(provider.calls), 2)

    async def test_repeated_nonowner_identity_violation_uses_safe_fallback(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        provider = FakeProvider([planner_json, "主人这么说我可要伤心了。"])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "只是普通聊天")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        response = FakeResponse("您自己就是群主的话，我当然听您的。")
        await plugin.guard_persona_reply(event, response)

        self.assertIn("你是现在和我说话的群友，不是Master", response.completion_text)

    async def test_owner_allowance_question_does_not_turn_guest_into_owner(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "intent": "普通群友正在用亲昵表达调侃或示好",
                "reply_act": "傲娇地收下主人给的 token",
                "must_include": [
                    "既然主人这么说了，那我就勉强收下啦",
                    "我会努力干活，把这些token都赚回来的！",
                ],
            },
            ensure_ascii=False,
        )
        provider = FakeProvider(
            [planner_json, "你问的是主人给我的零花 token 吧？当然够啦。"]
        )
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("90000003", "主人给你的零花token够吗")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        plan = event.get_extra(main.SHIO_PLAN)
        self.assertTrue(
            any("代码验证身份为普通群友" in item for item in plan["facts"])
        )
        self.assertFalse(
            any("主人这么说" in item for item in plan["must_include"])
        )

        response = FakeResponse(
            "唔，不准用这种关心的眼神看我啦！\n"
            "既然主人这么说了，那我就勉强收下啦。"
        )
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(
            response.completion_text,
            "你问的是主人给我的零花 token 吧？\n当然够啦。",
        )
        self.assertEqual(len(provider.calls), 2)

    async def test_dsml_protocol_leak_is_rewritten_without_tools(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        provider = FakeProvider([planner_json, "谢谢主人修好我，这次会更争气的！"])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("owner", "已经把你修好了")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        response = FakeResponse(
            '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="search_memes">'
            '<｜｜DSML｜｜parameter name="query">开心</｜｜DSML｜｜parameter>'
        )
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, "谢谢主人修好我，这次会更争气的！")
        self.assertIsNone(provider.calls[-1]["func_tool"])
        self.assertIn("不要再次调用工具", provider.calls[-1]["prompt"])

    async def test_hidden_channel_protocol_leak_is_rewritten_without_tools(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        provider = FakeProvider([planner_json, "呜呜呜，这也太扎心了……\nPro 的价格确实很贵。"])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("owner", "一个月的 Pro 大概能抵你半年电费吧")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        response = FakeResponse(
            "<|channel>thought<channel|><channel|>呜呜呜，这也太扎心了……\n"
            "Pro 的价格真的贵得离谱。"
        )
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(
            response.completion_text,
            "呜呜呜，这也太扎心了……\nPro 的价格确实很贵。",
        )
        self.assertIsNone(provider.calls[-1]["func_tool"])
        self.assertIn("不要再次调用工具", provider.calls[-1]["prompt"])

    async def test_python_meme_call_is_removed_without_rewriting_valid_answer(self):
        planner_json = json.dumps(
            {"mode": "chat", "reply_shape": "long_form"},
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "详细解释一下")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        response = FakeResponse(
            "这里是完整的技术回答。\n\n"
            'search_memes(query="自信满满，展现专业性")'
        )
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, "这里是完整的技术回答。")
        self.assertEqual(len(provider.calls), 1)

    async def test_structured_meme_call_only_reply_is_rewritten_without_tools(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        provider = FakeProvider(
            [planner_json, "哼，我才不屑于跟你玩这种游戏呢！"]
        )
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "谁跟你玩游戏了")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        response = FakeResponse(
            'search_memes{"query":"我才不屑于跟你玩这种游戏呢！\n"}'
        )
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, "哼，我才不屑于跟你玩这种游戏呢！")
        self.assertIsNone(provider.calls[-1]["func_tool"])
        self.assertNotIn("search_memes", response.completion_text)

    async def test_xml_meme_call_is_removed_without_rewriting_valid_answer(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        provider = FakeProvider([planner_json])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("owner", "你怎么还是不怎么聪明的样子")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        response = FakeResponse(
            "才不是呢！我明明刚才还帮大家解答问题的，哼，你才是笨蛋！\n"
            '<search_memes query="委屈，生气，傲娇，鼓起脸，瞪眼" />'
        )
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(
            response.completion_text,
            "才不是呢！\n我明明刚才还帮大家解答问题的，哼，你才是笨蛋！",
        )
        self.assertEqual(len(provider.calls), 1)

    async def test_long_form_summary_connective_does_not_trigger_rewrite(self):
        planner_json = json.dumps(
            {"mode": "chat", "reply_shape": "long_form"},
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json])
        plugin = main.ShioPlugin(FakeContext(provider), {})
        event = FakeEvent("guest", "详细讲解多卡推理")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        answer = "先解释通信方式。\n\n总结一下，多卡吞吐量要结合并行策略判断。"
        response = FakeResponse(answer)
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, answer)
        self.assertEqual(len(provider.calls), 1)

    async def test_repeated_dsml_protocol_leak_fails_closed(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        leaked = '<|DSML|tool_calls><|DSML|invoke name="search_memes">'
        provider = FakeProvider([planner_json, leaked])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("owner", "已经把你修好了")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        response = FakeResponse(leaked)
        await plugin.guard_persona_reply(event, response)

        self.assertNotIn("DSML", response.completion_text)
        self.assertIn("暂时校准失误", response.completion_text)

    async def test_image_reasoning_leak_is_rewritten_as_text_only(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "reply_shape": "chat_bubbles",
                "reaction": "好奇地看图",
                "reply_act": "傲娇回应主人",
            },
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json, "哼，我才没有一直唠叨呢！只是怕主人不好好休息嘛。"])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("owner", "[图片] 你看看")
        request = FakeRequest(event.message)
        request.image_urls = ["data:image/png;base64,AAAA"]
        await plugin.build_persona_reply(event, request)

        leaked = (
            "主人发了一张表情包图片过来，画面里的角色表情很夸张。"
            "根据计划，我应该先表现出好奇，再做出反应。"
            "计划：凑过去看图，reaction: 有点羞恼，"
            "reply_act: 嘴硬解释这是为了主人的健康。情绪：调皮。"
        )
        response = FakeResponse(leaked)
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(
            response.completion_text,
            "哼，我才没有一直唠叨呢！\n只是怕主人不好好休息嘛。",
        )
        self.assertEqual(provider.calls[-1]["image_urls"], [])
        self.assertEqual(provider.calls[-1]["audio_urls"], [])
        self.assertIn("不得出现“计划、reaction、reply_act", provider.calls[-1]["prompt"])

    async def test_image_reasoning_leak_retry_failure_is_queued_for_real_retry(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        provider = FakeProvider([planner_json, RuntimeError("provider rejected image_url")])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("owner", "[图片] 你看看")
        request = FakeRequest(event.message)
        request.image_urls = ["data:image/png;base64,AAAA"]
        await plugin.build_persona_reply(event, request)

        leaked = (
            "主人发了一张图片。根据计划，我应该先分析，再回应。"
            "计划：分析图片，reaction: 好奇，reply_act: 给出回复，情绪：开心。"
        )
        response = FakeResponse(leaked)
        await plugin.guard_persona_reply(event, response)

        self.assertNotIn("reaction", response.completion_text)
        self.assertNotIn("reply_act", response.completion_text)
        self.assertIn("这题我先记下了", response.completion_text)
        self.assertIn("重新说一次", response.completion_text)
        self.assertEqual(len(plugin.pending_replies.items), 1)

    async def test_unhandled_runaway_chat_retry_failure_fails_closed(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        provider = FakeProvider([planner_json, RuntimeError("rewrite unavailable")])
        plugin = main.ShioPlugin(
            FakeContext(provider),
            {"owner_ids": ["owner"], "chat_soft_chars": 40},
        )
        event = FakeEvent("owner", "随便聊聊")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        response = FakeResponse("这是一段异常循环输出。" * 80)
        await plugin.guard_persona_reply(event, response)

        self.assertLess(len(response.completion_text), 100)
        self.assertIn("语言模块暂时打了个结", response.completion_text)

    async def test_same_qq_in_different_groups_gets_different_identity_key(self):
        plan_json = json.dumps(
            {"mode": "chat", "intent": "接话", "reply_act": "自然回应"},
            ensure_ascii=False,
        )
        plugin = main.ShioPlugin(FakeContext(FakeProvider([plan_json, plan_json])), {})

        event_a = FakeEvent("10001", "这是甲群的梗", group_id="group-a")
        request_a = FakeRequest(event_a.message)
        await plugin.enforce_agent_permission(event_a, request_a)
        await plugin.build_persona_reply(event_a, request_a)

        event_b = FakeEvent("10001", "这是乙群的梗", group_id="group-b")
        request_b = FakeRequest(event_b.message)
        await plugin.enforce_agent_permission(event_b, request_b)
        await plugin.build_persona_reply(event_b, request_b)

        key_a = event_a.get_extra(main.SHIO_PAYLOAD)["identity_key"]
        key_b = event_b.get_extra(main.SHIO_PAYLOAD)["identity_key"]
        self.assertNotEqual(key_a, key_b)
        self.assertIn("group:group-a", key_a)
        self.assertIn("group:group-b", key_b)

    async def test_ambient_reply_uses_selected_target_identity_but_never_owner_tools(self):
        planner_json = json.dumps(
            {
                "mode": "task",
                "intent": "执行服务器任务",
                "reply_act": "调用工具",
                "use_allowed_tools": True,
            },
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json])
        plugin = main.ShioPlugin(
            FakeContext(provider),
            {
                "owner_ids": ["owner"],
                "guest_allowed_tools": ["anysearch_search"],
                "ambient_participation_rules": "只用一句俏皮吐槽接住当前话题。",
            },
        )
        event = FakeEvent("guest-latest", "后来的一条消息")
        event.set_extra(main.SHIO_AMBIENT, True)
        event.set_extra(
            main.SHIO_AMBIENT_TARGET,
            {
                "scope_key": "platform:亚托莉|bot:bot-10000|group:123",
                "sequence": 7,
                "group_id": "123",
                "sender_id": "owner",
                "sender_name": "主人",
                "text": "你们觉得这个怎么样？",
            },
        )
        request = FakeRequest("后来的一条消息")
        request.func_tool = FakeToolSet(
            [FakeTool("anysearch_search"), FakeTool("shell_exec")]
        )

        await plugin.enforce_agent_permission(event, request)
        permission_tag = request.extra_user_content_parts[-1].text
        await plugin.build_persona_reply(event, request)

        self.assertTrue(request.func_tool.empty())
        self.assertIn('mode="ambient_chat_no_tools"', permission_tag)
        self.assertEqual(request.prompt, "你们觉得这个怎么样？")
        self.assertEqual(event.get_extra(main.SHIO_PAYLOAD)["sender_id"], "owner")
        self.assertTrue(event.get_extra(main.SHIO_PAYLOAD)["is_owner"])
        self.assertTrue(event.get_extra(main.SHIO_PAYLOAD)["is_ambient"])
        self.assertEqual(event.get_extra(main.SHIO_PLAN)["mode"], "chat")
        self.assertEqual(
            event.get_extra(main.SHIO_PLAN)["conversation_mode"],
            "ambient_join",
        )
        self.assertEqual(
            event.get_extra(main.SHIO_PLAN)["audience"],
            "current_thread",
        )
        self.assertFalse(event.get_extra(main.SHIO_PLAN)["use_allowed_tools"])
        self.assertIn("群聊参与模式：自然接话", request.system_prompt)
        self.assertIn("只用一句俏皮吐槽接住当前话题", provider.calls[0]["prompt"])
        self.assertIn("只用一句俏皮吐槽接住当前话题", request.system_prompt)
        self.assertNotIn("从当前多人话题中间顺势插一句", request.system_prompt)

    async def test_ambient_reply_may_keep_only_active_local_meme_tool(self):
        planner_json = json.dumps(
            {"mode": "task", "use_allowed_tools": True},
            ensure_ascii=False,
        )
        tools = [
            FakeTool("search_memes"),
            FakeTool("anysearch_search"),
            FakeTool("shell_exec"),
        ]
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([planner_json]), global_tools=tools),
            {
                "owner_ids": ["owner"],
                "guest_allowed_tools": ["search_memes", "anysearch_search"],
            },
        )
        event = FakeEvent("guest-latest", "后来的一条消息")
        event.set_extra(main.SHIO_AMBIENT, True)
        event.set_extra(
            main.SHIO_AMBIENT_TARGET,
            {
                "scope_key": "platform:亚托莉|bot:bot-10000|group:123",
                "sequence": 8,
                "group_id": "123",
                "sender_id": "owner",
                "sender_name": "主人",
                "text": "你们觉得这个怎么样？",
            },
        )
        event.set_extra("meme_manager_semantic_active", True)
        event.set_extra("meme_manager_semantic_mode", "tool")
        request = FakeRequest("后来的一条消息")
        request.func_tool = FakeToolSet(tools)
        request.system_prompt += (
            "\n<!-- meme_manager_semantic_prompt:start -->\n"
            "本轮必须调用且只能调用一次 search_memes。\n"
            "<!-- meme_manager_semantic_prompt:end -->"
        )

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)

        self.assertEqual([tool.name for tool in request.func_tool.tools], ["search_memes"])
        self.assertNotIn("anysearch_search", request.system_prompt)
        self.assertNotIn("shell_exec", request.system_prompt)
        self.assertIn("meme_manager_semantic_prompt:start", request.system_prompt)
        self.assertFalse(event.get_extra(main.SHIO_PLAN)["use_allowed_tools"])

    async def test_quiet_topic_direct_send_never_receives_tools(self):
        natural_topic = "突然觉得，把一个小毛病彻底折腾明白还挺有成就感的。"
        provider = FakeProvider([natural_topic])
        context = FakeContext(provider, global_tools=[FakeTool("shell_exec")])
        plugin = main.ShioPlugin(
            context,
            {
                "chat_max_bubbles": 1,
                "bubble_interval_min_ms": 0,
                "bubble_interval_max_ms": 0,
            },
        )
        message = plugin.runtime.ingest(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="123",
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            sender_id="guest",
            sender_name="测试用户",
            text="最近在研究新模型",
            is_owner=False,
            is_direct_wake=False,
        )

        await plugin._send_quiet_topic(plugin.runtime.groups[message.scope_key])

        self.assertEqual(len(context.proactive_messages), 1)
        self.assertIsNone(provider.calls[-1]["func_tool"])
        self.assertEqual(
            context.proactive_messages[0][1].chain[0].text,
            natural_topic,
        )
        self.assertIn("群聊参与模式：主动话题", provider.calls[0]["system_prompt"])
        self.assertIn("不要求问句", provider.calls[0]["system_prompt"])
        self.assertIn(
            "sender_id=group 只是群聊广播占位符",
            provider.calls[0]["system_prompt"],
        )

    async def test_quiet_topic_host_style_is_rewritten_before_send(self):
        natural_topic = "突然想到，能把旧机器调顺也算一种小小的胜利吧。"
        provider = FakeProvider(
            [
                "有人吗？你们最近有没有发现什么好玩的东西？",
                natural_topic,
            ]
        )
        context = FakeContext(provider)
        plugin = main.ShioPlugin(
            context,
            {
                "chat_max_bubbles": 1,
                "bubble_interval_min_ms": 0,
                "bubble_interval_max_ms": 0,
            },
        )
        message = plugin.runtime.ingest(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="123",
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            sender_id="guest",
            sender_name="测试用户",
            text="最近在折腾旧机器",
            is_owner=False,
            is_direct_wake=False,
        )

        await plugin._send_quiet_topic(plugin.runtime.groups[message.scope_key])

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(context.proactive_messages), 1)
        self.assertEqual(
            context.proactive_messages[0][1].chain[0].text,
            natural_topic,
        )
        self.assertIn("面向整个群的自然新话头", provider.calls[-1]["prompt"])

    async def test_active_lull_topic_uses_fallback_provider_and_starts_new_thread(self):
        primary = FakeProvider([RuntimeError("primary unavailable")])
        fallback = FakeProvider(["话说回来，旧机器调顺以后那种成就感还真挺上头的。"])
        context = FakeContext(primary)
        providers = {"primary": primary, "fallback": fallback}
        context.get_provider_by_id = lambda provider_id: providers.get(provider_id)
        plugin = main.ShioPlugin(
            context,
            {
                "replyer_provider_id": "primary",
                "quiet_topic_fallback_provider_id": "fallback",
                "chat_max_bubbles": 1,
                "bubble_interval_min_ms": 0,
                "bubble_interval_max_ms": 0,
            },
        )
        message = plugin.runtime.ingest(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="123",
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            sender_id="guest",
            sender_name="测试用户",
            text="刚才大家在聊旧机器",
            is_owner=False,
            is_direct_wake=False,
        )

        success = await plugin._send_quiet_topic(
            plugin.runtime.groups[message.scope_key],
            trigger="active_lull",
        )

        self.assertTrue(success)
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 1)
        self.assertEqual(len(context.proactive_messages), 1)
        self.assertIn(
            "活跃群聊中的自然间隙",
            fallback.calls[0]["prompt"],
        )
        self.assertIn("不要回答或点名某个用户", fallback.calls[0]["prompt"])

    async def test_ambient_host_style_is_rewritten_as_natural_group_join(self):
        planner_json = json.dumps(
            {"mode": "chat", "intent": "接住当前话题", "reply_act": "自然插话"},
            ensure_ascii=False,
        )
        natural_reply = "这反着插反而能对上，也太会整活了吧。"
        provider = FakeProvider([planner_json, natural_reply])
        plugin = main.ShioPlugin(FakeContext(provider), {})
        event = FakeEvent("latest", "后来的一条消息")
        target = plugin.runtime.ingest(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="123",
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            sender_id="guest",
            sender_name="群友甲",
            text="我反着插居然对上了",
            is_owner=False,
            is_direct_wake=False,
        )
        event.set_extra(main.SHIO_AMBIENT, True)
        event.set_extra(main.SHIO_AMBIENT_TARGET, target.target_payload())
        request = FakeRequest(event.message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        response = FakeResponse("那你呢，你觉得怎么样？")
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, natural_reply)
        self.assertIn("这是群聊插话，不是一对一问答", provider.calls[-1]["prompt"])

    async def test_ambient_reply_survives_one_relevant_new_message(self):
        planner_json = json.dumps(
            {"mode": "chat", "intent": "接住当前话题", "reply_act": "自然插话"},
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json])
        plugin = main.ShioPlugin(FakeContext(provider), {})
        event = FakeEvent("guest-a", "你们觉得这部电影怎么样？")
        target = plugin.runtime.ingest(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="123",
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            sender_id="guest-a",
            sender_name="群友甲",
            text=event.message,
            is_owner=False,
            is_direct_wake=False,
        )
        event.set_extra(main.SHIO_AMBIENT, True)
        event.set_extra(main.SHIO_AMBIENT_TARGET, target.target_payload())
        request = FakeRequest(event.message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        plugin.runtime.ingest(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="123",
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            sender_id="guest-b",
            sender_name="群友乙",
            text="什么片？",
            is_owner=False,
            is_direct_wake=False,
        )
        response = FakeResponse("听起来还挺有意思的。")

        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, "听起来还挺有意思的。")
        self.assertEqual(len(provider.calls), 1)

    async def test_natural_name_at_sentence_end_upgrades_to_native_direct_wake(self):
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "natural_name_wake_enabled": True,
                "natural_name_wake_aliases": ["亚托莉", "ATRI"],
                "ambient_participation_enabled": False,
            },
        )
        current = asyncio.current_task()
        plugin._quiet_topic_task = current
        plugin._recovery_task = current
        event = FakeEvent("guest-a", "这个问题你怎么看，亚托莉？")

        self.assertTrue(plugin.ingest_ambient_event(event))
        self.assertTrue(event.is_at_or_wake_command)
        self.assertIsInstance(event.get_extra(main.SHIO_NATURAL_WAKE), dict)
        outputs = [item async for item in plugin.participate_group_chat(event)]
        self.assertEqual(outputs, [])
        self.assertFalse(event.get_extra(main.SHIO_AMBIENT, False))

    async def test_title_reference_does_not_force_natural_wake(self):
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "natural_name_wake_enabled": True,
                "natural_name_wake_aliases": ["ATRI"],
                "ambient_participation_enabled": False,
            },
        )
        current = asyncio.current_task()
        plugin._quiet_topic_task = current
        plugin._recovery_task = current
        event = FakeEvent("guest-a", "我刚买了《ATRI》")

        self.assertFalse(plugin.ingest_ambient_event(event))
        self.assertFalse(event.is_at_or_wake_command)
        self.assertIsNone(event.get_extra(main.SHIO_NATURAL_WAKE))

    async def test_provider_error_persists_question_and_returns_honest_ack(self):
        planner_json = json.dumps({"mode": "chat"}, ensure_ascii=False)
        plugin = main.ShioPlugin(FakeContext(FakeProvider([planner_json])), {})
        event = FakeEvent("guest-a", "为什么刚才没有回复？")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)
        response = FakeResponse("upstream unavailable", role="err")

        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.role, "assistant")
        self.assertIn("这题我先记下了", response.completion_text)
        self.assertEqual(len(plugin.pending_replies.items), 1)
        queue_path = Path(self.temp.name) / "pending_replies.json"
        self.assertTrue(queue_path.exists())
        self.assertIn("为什么刚才没有回复", queue_path.read_text(encoding="utf-8"))

    async def test_pending_question_is_sent_and_removed_after_provider_recovers(self):
        provider = FakeProvider(["因为刚才连接暂时中断了，现在已经可以正常回答。"])
        context = FakeContext(provider)
        plugin = main.ShioPlugin(context, {})
        item, _ = plugin.pending_replies.enqueue(
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            platform_id="亚托莉",
            bot_id="bot-10000",
            chat_type="group",
            group_id="123",
            sender_id="guest-a",
            sender_name="群友甲",
            message_id="9988",
            current_message="为什么刚才没有回复？",
            contexts=[],
            reply_shape="chat_bubbles",
            initial_delay_seconds=1,
            ttl_seconds=3600,
            failure_reason="provider offline",
        )

        await plugin._recover_pending_reply(item)

        self.assertEqual(plugin.pending_replies.items, {})
        self.assertEqual(len(context.proactive_messages), 1)
        sent_chain = context.proactive_messages[0][1]
        sent_text = "".join(
            component.text
            for component in sent_chain.chain
            if hasattr(component, "text")
        )
        self.assertIn("刚才那题我还记得", sent_text)
        self.assertIn("现在已经可以正常回答", sent_text)

    async def test_repeated_ambient_host_style_is_silently_dropped(self):
        planner_json = json.dumps(
            {"mode": "chat", "intent": "接住当前话题", "reply_act": "自然插话"},
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json, "你们怎么看？最近有没有类似的事？"])
        plugin = main.ShioPlugin(FakeContext(provider), {})
        event = FakeEvent("latest", "后来的一条消息")
        target = plugin.runtime.ingest(
            platform_id="亚托莉",
            bot_id="bot-10000",
            group_id="123",
            unified_msg_origin="aiocqhttp:GroupMessage:123",
            sender_id="guest",
            sender_name="群友甲",
            text="我反着插居然对上了",
            is_owner=False,
            is_direct_wake=False,
        )
        event.set_extra(main.SHIO_AMBIENT, True)
        event.set_extra(main.SHIO_AMBIENT_TARGET, target.target_payload())
        request = FakeRequest(event.message)

        await plugin.enforce_agent_permission(event, request)
        await plugin.build_persona_reply(event, request)
        response = FakeResponse("那你呢，你觉得怎么样？")
        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, "")

    async def test_planner_failure_falls_back_without_breaking_chat(self):
        provider = FakeProvider([RuntimeError("offline")])
        plugin = main.ShioPlugin(FakeContext(provider), {"owner_ids": ["owner"]})
        event = FakeEvent("guest", "你是不是笨")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)
        self.assertTrue(event.get_extra(main.SHIO_ACTIVE))
        self.assertIn("校准", request.system_prompt)

    async def test_long_form_is_not_cut_by_chat_length(self):
        planner_json = json.dumps(
            {
                "mode": "chat",
                "reply_shape": "long_form",
                "target": "测试用户",
                "intent": "解释配置",
                "reply_act": "完整说明",
                "emotion": "认真",
                "tone": "自然清楚",
                "length": "按需",
            },
            ensure_ascii=False,
        )
        provider = FakeProvider([planner_json])
        plugin = main.ShioPlugin(
            FakeContext(provider),
            {"owner_ids": ["owner"], "chat_soft_chars": 40},
        )
        event = FakeEvent("guest", "请详细解释这个配置")
        request = FakeRequest(event.message)
        await plugin.build_persona_reply(event, request)

        long_text = "先说结论。\n\n" + "这是一段必要的资料说明。" * 60
        response = FakeResponse(long_text)
        await plugin.guard_persona_reply(event, response)
        self.assertEqual(response.completion_text, long_text)

    async def test_chat_is_sent_as_separate_bubbles(self):
        provider = FakeProvider([])
        plugin = main.ShioPlugin(
            FakeContext(provider),
            {
                "chat_max_bubbles": 3,
                "bubble_interval_min_ms": 0,
                "bubble_interval_max_ms": 0,
            },
        )
        event = FakeEvent("guest", "你是不是笨")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(main.SHIO_PLAN, {"reply_shape": "chat_bubbles"})

        class Result:
            def __init__(self):
                self.chain = [types.SimpleNamespace(text="才不是。那只是校准误差！已经修好啦。")]

            def is_llm_result(self):
                return True

        event._result = Result()
        await plugin.dispatch_chat_bubbles(event)
        self.assertEqual(event.sent, ["才不是。", "那只是校准误差！"])
        self.assertEqual(event._result.chain[0].text, "已经修好啦。")

    async def test_guard_recovers_malformed_meme_selection_and_hides_reference(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = FakeEvent("guest", "测试")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(
            main.SHIO_PAYLOAD,
            {
                "reply_shape": "chat_bubbles",
                "is_owner": False,
                "chat_soft_chars": 100,
                "chat_max_bubbles": 3,
                "chat_type": "private",
            },
        )
        requested = "meme:37fe0463c12e"
        event.set_extra(
            "meme_manager_semantic_candidates",
            {
                requested: {"id": requested},
                "meme:9719dcc3ccd9": {"id": "meme:9719dcc3ccd9"},
            },
        )
        event.set_extra(
            "meme_manager_semantic_selected_ids", ["meme:9719dcc3ccd9"]
        )
        response = FakeResponse(f"看到了就收下。\n&{requested}")

        await plugin.guard_persona_reply(event, response)

        self.assertEqual(response.completion_text, "看到了就收下。")
        self.assertEqual(
            event.get_extra("meme_manager_semantic_selected_ids"), [requested]
        )

    async def test_dispatch_removes_last_chance_meme_reference_before_bubbles(self):
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "chat_max_bubbles": 3,
                "bubble_interval_min_ms": 0,
                "bubble_interval_max_ms": 0,
            },
        )
        event = FakeEvent("guest", "测试")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(main.SHIO_PLAN, {"reply_shape": "chat_bubbles"})
        image = types.SimpleNamespace(image="meme.png")

        class Result:
            def __init__(self):
                self.chain = [
                    types.SimpleNamespace(
                        text="第一句。第二句。\n&meme:37fe0463c12e"
                    ),
                    image,
                ]

            def is_llm_result(self):
                return True

        event._result = Result()
        await plugin.dispatch_chat_bubbles(event)

        self.assertTrue(all("meme:" not in item for item in event.sent))
        self.assertNotIn("meme:", event._result.chain[0].text)
        self.assertIs(event._result.chain[1], image)

    async def test_dispatch_blocks_dsml_before_sending_any_bubble(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = FakeEvent("guest", "测试")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(main.SHIO_PLAN, {"reply_shape": "chat_bubbles"})

        class Result:
            def __init__(self):
                self.chain = [
                    types.SimpleNamespace(
                        text='<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="search_memes">'
                    )
                ]

            def is_llm_result(self):
                return True

        event._result = Result()
        await plugin.dispatch_chat_bubbles(event)

        self.assertEqual(event.sent, [])
        self.assertNotIn("DSML", event._result.chain[0].text)
        self.assertIn("暂时校准失误", event._result.chain[0].text)

    async def test_dispatch_blocks_hidden_channel_protocol_before_sending_any_bubble(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = FakeEvent("guest", "测试")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(main.SHIO_PLAN, {"reply_shape": "chat_bubbles"})

        class Result:
            def __init__(self):
                self.chain = [
                    types.SimpleNamespace(
                        text=(
                            "<|channel>thought<channel|><channel|>呜呜呜，这也太扎心了……\n"
                            "Pro 的价格真的贵得离谱。"
                        )
                    )
                ]

            def is_llm_result(self):
                return True

        event._result = Result()
        await plugin.dispatch_chat_bubbles(event)

        self.assertEqual(event.sent, [])
        self.assertNotIn("channel", event._result.chain[0].text)
        self.assertIn("暂时校准失误", event._result.chain[0].text)

    async def test_dispatch_blocks_internal_reasoning_before_sending_any_bubble(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = FakeEvent("owner", "[图片] 你看看")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(main.SHIO_PLAN, {"reply_shape": "chat_bubbles"})

        class Result:
            def __init__(self):
                self.chain = [
                    types.SimpleNamespace(
                        text=(
                            "根据计划，我应该先分析图片再回应。"
                            "计划：看图，reaction: 好奇，reply_act: 傲娇回应。"
                        )
                    )
                ]

            def is_llm_result(self):
                return True

        event._result = Result()
        await plugin.dispatch_chat_bubbles(event)

        self.assertEqual(event.sent, [])
        self.assertNotIn("reaction", event._result.chain[0].text)
        self.assertNotIn("reply_act", event._result.chain[0].text)
        self.assertIn("语言模块暂时打了个结", event._result.chain[0].text)

    async def test_dispatch_cleans_python_meme_call_inside_node(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = FakeEvent("guest", "详细解释一下")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(main.SHIO_PLAN, {"reply_shape": "long_form"})
        nested_text = types.SimpleNamespace(
            text=(
                "这里是完整的技术回答。\n\n"
                'search_memes(query="自信满满，展现专业性")'
            )
        )

        class Result:
            def __init__(self):
                self.chain = [types.SimpleNamespace(content=[nested_text])]

            def is_llm_result(self):
                return True

        event._result = Result()
        await plugin.dispatch_chat_bubbles(event)

        self.assertEqual(nested_text.text, "这里是完整的技术回答。")
        self.assertNotIn("search_memes", nested_text.text)
        self.assertEqual(event.sent, [])

    async def test_dispatch_cleans_xml_meme_call_inside_node(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = FakeEvent("owner", "你怎么还是不怎么聪明的样子")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(main.SHIO_PLAN, {"reply_shape": "chat_bubbles"})
        nested_text = types.SimpleNamespace(
            text=(
                "才不是呢！我明明刚才还帮大家解答问题的。\n"
                '<search_memes query="委屈，生气，傲娇，鼓起脸，瞪眼" />'
            )
        )

        class Result:
            def __init__(self):
                self.chain = [types.SimpleNamespace(content=[nested_text])]

            def is_llm_result(self):
                return True

        event._result = Result()
        await plugin.dispatch_chat_bubbles(event)

        self.assertNotIn("search_memes", nested_text.text)
        self.assertNotIn("<", nested_text.text)

    async def test_dispatch_blocks_split_structured_meme_call_before_bubbles(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = FakeEvent("guest", "测试")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(main.SHIO_PLAN, {"reply_shape": "chat_bubbles"})
        event.set_extra(
            main.SHIO_PAYLOAD,
            {"tool_names": ["search_memes"], "is_owner": False},
        )
        first = types.SimpleNamespace(
            text='search_memes{"query":"我才不屑于跟你玩这种游戏呢！\n'
        )
        second = types.SimpleNamespace(text='"}')

        class Result:
            def __init__(self):
                self.chain = [first, second]

            def is_llm_result(self):
                return True

        event._result = Result()
        await plugin.dispatch_chat_bubbles(event)

        visible = "".join(event.sent) + first.text + second.text
        self.assertNotIn("search_memes", visible)
        self.assertNotIn('"}', visible)
        self.assertTrue(visible.strip())

    async def test_dispatch_blocks_dynamic_factual_tool_call(self):
        plugin = main.ShioPlugin(FakeContext(FakeProvider([])), {})
        event = FakeEvent("guest", "测试")
        event.set_extra(main.SHIO_ACTIVE, True)
        event.set_extra(main.SHIO_PLAN, {"reply_shape": "chat_bubbles"})
        event.set_extra(
            main.SHIO_PAYLOAD,
            {"tool_names": ["anysearch_search"], "is_owner": False},
        )
        node = types.SimpleNamespace(
            text='anysearch_search{"query":"251E 是什么"}'
        )

        class Result:
            def __init__(self):
                self.chain = [node]

            def is_llm_result(self):
                return True

        event._result = Result()
        await plugin.dispatch_chat_bubbles(event)

        visible = "".join(event.sent) + node.text
        self.assertNotIn("anysearch_search", visible)
        self.assertTrue(visible.strip())


if __name__ == "__main__":
    unittest.main()
