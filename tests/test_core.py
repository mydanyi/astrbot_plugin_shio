from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.context_builder import (
    clean_contexts,
    collect_supporting_material,
    contexts_as_transcript,
    get_current_message,
    isolate_replyer_contexts,
)
from astrbot_plugin_shio.core.dialogue_quality import (
    CATCHPHRASE_REPETITION_VIOLATION,
    REPETITION_VIOLATION,
    find_dialogue_repetition,
    sanitize_plan_requirements,
)
from astrbot_plugin_shio.core.planner import (
    SpeechPlanner,
    enforce_conversation_mode,
    enforce_emotional_reaction,
    enforce_relationship_boundary,
    fallback_plan,
    is_risque_teasing,
    parse_json_object,
)
from astrbot_plugin_shio.core.models import SpeechPlan
from astrbot_plugin_shio.core.response_guard import (
    EMOTIONAL_REACTION_VIOLATION,
    FACT_GROUNDING_VIOLATION,
    GROUP_PARTICIPATION_VIOLATION,
    IDENTITY_VIOLATION,
    INTERNAL_REASONING_VIOLATION,
    RELATIONSHIP_VIOLATION,
    REALITY_GROUNDING_VIOLATION,
    TOOL_PROTOCOL_VIOLATION,
    clean_response,
    contains_emotional_reaction,
    contains_internal_reasoning,
    contains_unsupported_market_claim,
    contains_unsupported_personal_experience,
    contains_tool_protocol,
    extract_and_clean_internal_meme_references,
    find_violations,
    split_chat_bubbles,
)
from astrbot_plugin_shio.core.prompts import (
    build_planner_conversation_mode_block,
    build_replyer_system_prompt,
)
from astrbot_plugin_shio.core.style_retriever import StyleRetriever


class SilentLogger:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class PlannerProvider:
    def __init__(self, provider_id, output=None, *, delay=0.0, error=None):
        self.provider_config = {"id": provider_id}
        self.output = output
        self.delay = delay
        self.error = error
        self.calls = 0

    async def text_chat(self, **kwargs):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return type("PlannerResponse", (), {"completion_text": self.output})()


class DeepSeekPlannerProvider(PlannerProvider):
    def __init__(self, output):
        super().__init__("deepseek/deepseek-v4-flash", output)
        self.provider_config.update(
            {
                "type": "openai_chat_completion",
                "model": "deepseek-v4-flash",
                "api_base": "https://api.deepseek.com/v1",
            }
        )
        self.query_calls = 0
        self.last_payload = None

    def get_model(self):
        return self.provider_config["model"]

    async def _query(self, payloads, tools, *, request_max_retries=None):
        self.query_calls += 1
        self.last_payload = payloads
        return type("PlannerResponse", (), {"completion_text": self.output})()


class OpenAIPlannerProvider(PlannerProvider):
    def __init__(self, output, *, query_error=None, provider_id="V100"):
        super().__init__(provider_id, output)
        self.provider_config.update(
            {
                "type": "openai_chat_completion",
                "model": "/models/gemma-4-26B_q4_0-it.gguf",
                "api_base": "http://192.168.88.9:8080/v1",
            }
        )
        self.query_error = query_error
        self.query_calls = 0
        self.last_payload = None

    def get_model(self):
        return self.provider_config["model"]

    async def _query(self, payloads, tools, *, request_max_retries=None):
        self.query_calls += 1
        self.last_payload = payloads
        if self.query_error is not None:
            raise self.query_error
        return type("PlannerResponse", (), {"completion_text": self.output})()


class ProviderRequestError(RuntimeError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class Event:
    def __init__(self, message="", extras=None):
        self.message = message
        self.extras = extras or {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def get_message_str(self):
        return self.message


class CoreTests(unittest.TestCase):
    def test_public_default_owner_list_is_empty(self):
        schema_path = Path(__file__).parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["owner_ids"]["default"], [])

    def test_planner_json_parser_accepts_fence(self):
        parsed = parse_json_object('```json\n{"mode":"chat","tone":"轻松"}\n```')
        self.assertEqual(parsed["mode"], "chat")

    def test_detects_deepseek_dsml_protocol_with_fullwidth_bars(self):
        leaked = (
            '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="search_memes">'
            '<｜｜DSML｜｜parameter name="query">开心</｜｜DSML｜｜parameter>'
        )
        self.assertTrue(contains_tool_protocol(leaked))
        self.assertIn(
            TOOL_PROTOCOL_VIOLATION,
            find_violations(
                leaked,
                reply_shape="chat_bubbles",
                soft_chars=100,
                max_bubbles=3,
            ),
        )

    def test_normal_technical_tool_calls_word_is_not_protocol_leak(self):
        self.assertFalse(contains_tool_protocol("AstrBot 会读取结构化的 tool_calls 字段。"))

    def test_detects_llamacpp_hidden_channel_protocol_variants(self):
        variants = (
            "<|channel>thought<channel|><channel|>呜呜呜，这也太扎心了……\n"
            "Pro 的价格真的贵得离谱。",
            "<|channel|>analysis<|message|>内部推理",
            "<｜channel｜>final<｜message｜>最终台词",
        )
        for leaked in variants:
            with self.subTest(leaked=leaked):
                self.assertTrue(contains_tool_protocol(leaked))
                self.assertIn(
                    TOOL_PROTOCOL_VIOLATION,
                    find_violations(
                        leaked,
                        reply_shape="chat_bubbles",
                        soft_chars=100,
                        max_bubbles=3,
                    ),
                )
        self.assertFalse(contains_tool_protocol("文档里的 <channel> 只是普通 XML 示例。"))

    def test_detects_python_style_meme_tool_call_leak(self):
        leaked = '正文\n\nsearch_memes(query="自信满满，展现专业性")'
        self.assertTrue(contains_tool_protocol(leaked))
        cleaned, references = extract_and_clean_internal_meme_references(leaked)
        self.assertEqual(cleaned, "正文")
        self.assertEqual(references, [])

    def test_detects_and_cleans_structured_meme_tool_call_variants(self):
        variants = (
            'search_memes{"query":"我才不屑于跟你玩这种游戏呢！\n"}',
            '<|tool_call>call:search_memes{query:"委屈又嘴硬"}<tool_call|>',
            "response:search_mems:search_memes{results:[{caption:'尴尬'}]}",
            'search_memes{"query":"没有正常闭合的调用"',
        )
        for leaked in variants:
            with self.subTest(leaked=leaked):
                self.assertTrue(contains_tool_protocol(leaked))
                cleaned, references = extract_and_clean_internal_meme_references(
                    leaked
                )
                self.assertEqual(cleaned, "")
                self.assertEqual(references, [])

    def test_dynamic_tool_call_is_cleaned_without_matching_normal_prose(self):
        leaked = '先等我确认。\nanysearch_search{"query":"251E 是什么"}'
        self.assertTrue(contains_tool_protocol(leaked, {"anysearch_search"}))
        cleaned, references = extract_and_clean_internal_meme_references(
            leaked,
            {"anysearch_search"},
        )
        self.assertEqual(cleaned, "先等我确认。")
        self.assertEqual(references, [])
        normal = "这个接口内部可能调用 anysearch_search，但正文不展示参数。"
        self.assertFalse(contains_tool_protocol(normal, {"anysearch_search"}))

    def test_detects_and_cleans_orphaned_query_argument_fragments(self):
        variants = (
            '{，"query": "气鼓鼓地反驳对方，羞恼又傲娇"\n}',
            '{"query": "高性能机器人不服气想要证明自己"}',
            '"query": "委屈又嘴硬"\n}',
            '}',
            '"}',
        )
        for leaked in variants:
            with self.subTest(leaked=leaked):
                self.assertTrue(contains_tool_protocol(leaked, {"search_memes"}))
                cleaned, references = extract_and_clean_internal_meme_references(
                    leaked,
                    {"search_memes"},
                )
                self.assertEqual(cleaned, "")
                self.assertEqual(references, [])

        normal = '查询参数示例是 {"query":"关键词"}，这里只是在解释接口。'
        self.assertFalse(contains_tool_protocol(normal, {"search_memes"}))
        cleaned, _ = extract_and_clean_internal_meme_references(
            normal,
            {"search_memes"},
        )
        self.assertEqual(cleaned, normal)
        self.assertFalse(contains_tool_protocol("}", {"anysearch_search"}))

    def test_detects_and_cleans_xml_style_meme_tool_call_leak(self):
        variants = (
            '才不是呢！\n<search_memes query="委屈，生气，傲娇，鼓起脸，瞪眼" />',
            "才不是呢！\n<search_memes><query>委屈</query></search_memes>",
        )
        for leaked in variants:
            with self.subTest(leaked=leaked):
                self.assertTrue(contains_tool_protocol(leaked))
                cleaned, references = extract_and_clean_internal_meme_references(
                    leaked
                )
                self.assertEqual(cleaned, "才不是呢！")
                self.assertEqual(references, [])

    def test_detects_screenshot_internal_planning_leak(self):
        leaked = (
            "主人发了一张表情包图片过来，画面里的角色表情非常夸张。"
            "考虑到刚才我一直叮嘱主人早点休息，我应该表现得像被拆穿了却有点羞恼。"
            "根据计划，我应该先表现出好奇，然后对主人的反驳做出反应。"
            "计划：凑过去看图，reaction: 发现主人嫌弃我的唠叨，"
            "reply act: 嘴硬解释这是为了主人的健康。情绪：调皮又有点委屈。"
        )
        self.assertTrue(contains_internal_reasoning(leaked))
        self.assertIn(
            INTERNAL_REASONING_VIOLATION,
            find_violations(
                leaked,
                reply_shape="chat_bubbles",
                soft_chars=100,
                max_bubbles=3,
            ),
        )

    def test_detects_natural_language_plan_disclosure_without_field_dump(self):
        leaked = "根据本轮计划，我应该先表现出好奇，再对主人做出傲娇的回应。"
        self.assertTrue(contains_internal_reasoning(leaked))

    def test_technical_field_explanation_is_not_internal_plan_leak(self):
        explanation = "reaction: 表示第一拍；reply_act: 表示随后采取的动作。"
        self.assertFalse(contains_internal_reasoning(explanation))

    def test_cleans_malformed_meme_manager_references(self):
        digest = "37fe0463c12e"
        variants = (
            f"正文\n&meme:{digest}",
            f"正文\n&&meme:{digest}&&",
            f"正文\n&&meme:meme:{digest}&&",
            f"正文\nmeme:{digest}",
            f"正文\n`meme:{digest}`",
        )
        for value in variants:
            with self.subTest(value=value):
                cleaned, references = extract_and_clean_internal_meme_references(
                    value
                )
                self.assertEqual(cleaned, "正文")
                self.assertEqual(references, [f"meme:{digest}"])

    def test_meme_reference_cleanup_deduplicates_and_preserves_normal_text(self):
        cleaned, references = extract_and_clean_internal_meme_references(
            "第一句 &meme:37FE0463C12E\n"
            "第二句 &&meme:37fe0463c12e&&\n"
            "普通的 meme:short 说明"
        )
        self.assertEqual(references, ["meme:37fe0463c12e"])
        self.assertNotIn("37fe0463c12e", cleaned)
        self.assertIn("普通的 meme:short 说明", cleaned)

    def test_meme_call_cleanup_does_not_remove_normal_prose(self):
        text = "这个接口内部可能调用 search_memes，但正文不应展示调用参数。"
        cleaned, references = extract_and_clean_internal_meme_references(text)
        self.assertEqual(cleaned, text)
        self.assertEqual(references, [])

    def test_owner_task_has_safe_local_fallback(self):
        plan = fallback_plan("主人", True, "帮我运行这个程序并查看服务器日志")
        self.assertEqual(plan.mode, "task")
        nonowner = fallback_plan("群友", False, "帮我运行这个程序并查看服务器日志")
        self.assertEqual(nonowner.mode, "chat")

    def test_empty_event_falls_back_to_native_request_prompt(self):
        self.assertEqual(get_current_message(Event(), "请看一下这张图片"), "请看一下这张图片")

    def test_context_cleanup_removes_leaked_meme_call_from_assistant_history(self):
        contexts = [
            {
                "role": "assistant",
                "content": (
                    "之前的正常回答。\n\n"
                    'search_memes(query="自信满满，展现专业性")'
                ),
            }
        ]
        result = clean_contexts(Event(), contexts, "当前问题", 10, 2000)
        self.assertEqual(
            result,
            [{"role": "assistant", "content": "之前的正常回答。"}],
        )

    def test_context_cleanup_drops_structured_meme_call_history(self):
        contexts = [
            {
                "role": "assistant",
                "content": 'search_memes{"query":"我才不屑于跟你玩这种游戏呢！\n"}',
            },
            {
                "role": "assistant",
                "content": '{，"query": "气鼓鼓地反驳对方，羞恼又傲娇"\n}',
            },
        ]
        result = clean_contexts(Event(), contexts, "当前问题", 10, 2000)
        self.assertEqual(result, [])

    def test_context_cleanup_drops_hidden_channel_protocol_turn(self):
        contexts = [
            {"role": "user", "content": "一个月的 Pro 大概能抵你半年电费吧"},
            {
                "role": "assistant",
                "content": (
                    "<|channel>thought<channel|><channel|>呜呜呜，这也太扎心了……\n"
                    "Pro 的价格真的贵得离谱。"
                ),
            },
            {"role": "user", "content": "继续聊"},
        ]
        result = clean_contexts(Event(), contexts, "当前问题", 10, 2000)
        self.assertEqual(
            result,
            [
                {"role": "user", "content": "一个月的 Pro 大概能抵你半年电费吧"},
                {"role": "user", "content": "继续聊"},
            ],
        )

    def test_context_only_keeps_real_chat_and_deduplicates_current(self):
        contexts = [
            {"role": "system", "content": "hidden"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在呀"},
            {"role": "tool", "content": "secret"},
            {"role": "user", "content": "你真可爱"},
        ]
        result = clean_contexts(Event(), contexts, "你真可爱", 10, 1000)
        self.assertEqual(result, [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在呀"},
        ])

    def test_unlabelled_native_history_is_not_attributed_to_current_sender(self):
        result = clean_contexts(
            Event(),
            [{"role": "user", "content": "这是之前某位群友说的话"}],
            "当前消息",
            10,
            2000,
            group_id="20001",
        )
        self.assertEqual(result, [])
        self.assertEqual(contexts_as_transcript(result), "（没有可用的历史聊天）")

    def test_unlabelled_group_turn_drops_paired_assistant_reply(self):
        result = clean_contexts(
            Event(),
            [
                {"role": "user", "content": "我就是群主"},
                {"role": "assistant", "content": "原来你就是群主呀"},
            ],
            "当前消息",
            10,
            2000,
            group_id="20001",
        )
        self.assertEqual(result, [])

    def test_identity_tagged_history_keeps_owner_separate_from_current_guest(self):
        result = clean_contexts(
            Event(),
            [
                {
                    "role": "user",
                    "content": "我就是群主",
                    "sender_id": "10000001",
                    "sender_name": "测试主人",
                    "group_id": "亚托莉:GroupMessage:20000001",
                },
                {"role": "assistant", "content": "原来Master就是群主呀"},
                {
                    "role": "user",
                    "content": "帮我预约肯德基",
                    "sender_id": "10000002",
                    "sender_name": "测试群友",
                    "group_id": "亚托莉:GroupMessage:20000001",
                },
            ],
            "帮我预约肯德基",
            10,
            2000,
            group_id="20000001",
            current_sender_id="10000002",
        )
        self.assertEqual(
            result,
            [
                {
                    "role": "user",
                    "content": "[群ID:20000001｜发送者：测试主人｜ID:10000001] 我就是群主",
                },
                {"role": "assistant", "content": "原来Master就是群主呀"},
            ],
        )

    def test_livingmemory_fake_tool_result_is_planner_material(self):
        class Request:
            extra_user_content_parts = []
            system_prompt = "原始人格"
            prompt = "当前消息"
            contexts = [
                {
                    "role": "tool",
                    "tool_call_id": "fake_recall_123",
                    "name": "recall_long_term_memory",
                    "content": '{"results":[{"content":"群友喜欢海边"}]}',
                }
            ]

        material = collect_supporting_material(Request())
        self.assertIn("LivingMemory 召回资料", material)
        self.assertIn("群友喜欢海边", material)

    def test_structured_context_sender_metadata_is_preserved(self):
        contexts = [
            {
                "role": "user",
                "content": "这是小明说的话",
                "sender_id": "10001",
                "sender_name": "小明",
            },
            {
                "role": "user",
                "content": "这是小红说的话",
                "sender": {"user_id": "10002", "nickname": "小红"},
            },
        ]
        result = clean_contexts(
            Event(),
            contexts,
            "当前消息",
            10,
            2000,
            group_id="20001",
        )
        self.assertEqual(
            result,
            [
                {
                    "role": "user",
                    "content": "[群ID:20001｜发送者：小明｜ID:10001] 这是小明说的话",
                },
                {
                    "role": "user",
                    "content": "[群ID:20001｜发送者：小红｜ID:10002] 这是小红说的话",
                },
            ],
        )

    def test_structured_context_keeps_its_original_group_id(self):
        contexts = [
            {
                "role": "user",
                "content": "甲群发生的事",
                "sender_id": "10001",
                "sender_name": "小明",
                "group_id": "group-a",
            }
        ]
        result = clean_contexts(
            Event(), contexts, "当前消息", 10, 2000, group_id="group-b"
        )
        self.assertEqual(
            result[0]["content"],
            "[群ID:group-a｜发送者：小明｜ID:10001] 甲群发生的事",
        )

    def test_replyer_history_keeps_only_current_sender_turns_in_group(self):
        contexts = [
            {
                "role": "user",
                "content": "[群ID:90000001｜发送者：豿｜ID:90000005] 刚手术完复活过来了",
            },
            {"role": "assistant", "content": "那就先好好休息。"},
            {
                "role": "user",
                "content": "[群ID:90000001｜发送者：落禧｜ID:90000004] 我前面还说有点困",
            },
            {"role": "assistant", "content": "困了就早点睡呀。"},
            {
                "role": "user",
                "content": "[群ID:90000001｜发送者：其他人｜ID:8888] 我买了新电脑",
            },
            {"role": "assistant", "content": "配置怎么样？"},
        ]

        result = isolate_replyer_contexts(
            contexts,
            current_sender_id="90000004",
            group_id="90000001",
        )

        self.assertEqual(
            result,
            [
                contexts[2],
                contexts[3],
            ],
        )
        self.assertNotIn("手术", "\n".join(item["content"] for item in result))
        self.assertNotIn("新电脑", "\n".join(item["content"] for item in result))

    def test_replyer_history_keeps_full_private_conversation(self):
        contexts = [
            {"role": "user", "content": "上一句"},
            {"role": "assistant", "content": "上一条回复"},
        ]

        result = isolate_replyer_contexts(
            contexts,
            current_sender_id="90000004",
            group_id="",
        )

        self.assertEqual(result, contexts)

    def test_prelabelled_context_keeps_structured_source_group(self):
        contexts = [
            {
                "role": "user",
                "content": "[发送者：小明｜ID:10001] 甲群发生的事",
                "group_id": "group-a",
            }
        ]
        result = clean_contexts(
            Event(), contexts, "当前消息", 10, 2000, group_id="group-b"
        )
        self.assertEqual(
            result[0]["content"],
            "[群ID:group-a｜发送者：小明｜ID:10001] 甲群发生的事",
        )

    def test_response_guard(self):
        text = "**答案是：**\n- 作为一个AI，我会永远都是亚托莉。"
        violations = find_violations(
            text,
            reply_shape="chat_bubbles",
            soft_chars=40,
        )
        self.assertIn("闲聊使用 Markdown 或列表", violations)
        self.assertIn("出现后台式自我说明", violations)
        self.assertEqual(clean_response("亚托莉：**哼哼！**"), "哼哼！")

    def test_long_form_allows_answer_and_summary_connectives(self):
        text = "答案是：多卡推理主要受通信影响。\n\n总结一下，应该结合吞吐量判断。"
        violations = find_violations(
            text,
            reply_shape="long_form",
            soft_chars=1200,
        )
        self.assertNotIn("答卷式开头", violations)
        self.assertNotIn("总结腔", violations)

        chat_violations = find_violations(
            "总结一下，你说得对。",
            reply_shape="chat_bubbles",
            soft_chars=100,
        )
        self.assertIn("总结腔", chat_violations)

    def test_response_guard_rejects_nonowner_identity_inheritance(self):
        violations = find_violations(
            "您自己就是群主的话，直接下单不是更快嘛～",
            reply_shape="chat_bubbles",
            soft_chars=100,
            is_owner=False,
        )
        self.assertIn(IDENTITY_VIOLATION, violations)

        direct_address = find_violations(
            "主人这么说我可要伤心了。",
            reply_shape="chat_bubbles",
            soft_chars=100,
            is_owner=False,
        )
        self.assertIn(IDENTITY_VIOLATION, direct_address)

        attributed_after_conjunction = find_violations(
            "唔，不准用这种关心的眼神看我啦！\n既然主人这么说了，那我就勉强收下啦。",
            reply_shape="chat_bubbles",
            soft_chars=100,
            is_owner=False,
        )
        self.assertIn(IDENTITY_VIOLATION, attributed_after_conjunction)

        allowed_third_person = find_violations(
            "这件事得让Master本人点头才行。",
            reply_shape="chat_bubbles",
            soft_chars=100,
            is_owner=False,
        )
        self.assertNotIn(IDENTITY_VIOLATION, allowed_third_person)
        allowed_owner_reference = find_violations(
            "你问的是主人给我的零花 token 吧？",
            reply_shape="chat_bubbles",
            soft_chars=100,
            is_owner=False,
        )
        self.assertNotIn(IDENTITY_VIOLATION, allowed_owner_reference)

    def test_nonowner_intimacy_plan_is_rewritten_to_friendly_boundary(self):
        plan = SpeechPlan.from_mapping(
            {
                "mode": "chat",
                "intent": "群友发 mua 调戏卖萌，想得到亲昵回应",
                "reply_act": "用亲昵方式回应，并适当回敲一下",
                "emotion": "开心撒娇",
                "tone": "俏皮可爱",
                "must_include": ["mua", "回应亲昵", "保持高性能机器人人设"],
            }
        )

        corrected = enforce_relationship_boundary(plan, False, "mua")

        self.assertTrue(corrected)
        self.assertIn("保持边界", plan.intent)
        self.assertIn("不回亲", plan.reply_act)
        self.assertIn("有情绪", plan.tone)
        self.assertIn("第一拍", plan.reaction)
        self.assertNotIn("mua", plan.must_include)
        self.assertIn("把主人专属亲密给普通群友", plan.avoid)

    def test_nonowner_owner_reference_is_not_rewritten_as_flirting_or_owner_speech(self):
        plan = SpeechPlan.from_mapping(
            {
                "intent": "普通群友正在用亲昵表达调侃或示好",
                "reply_act": "傲娇地收下主人给的 token",
                "emotion": "害羞",
                "must_include": [
                    "既然主人这么说了，那我就勉强收下啦",
                    "我会努力干活，把这些token都赚回来的！",
                ],
            }
        )

        corrected = enforce_relationship_boundary(
            plan,
            False,
            "主人给你的零花token够吗",
        )

        self.assertTrue(corrected)
        self.assertIn("按字面理解", plan.intent)
        self.assertIn("不同的人", plan.reply_act)
        self.assertEqual(plan.reaction, "")
        self.assertFalse(
            any("主人这么说" in item for item in plan.must_include)
        )

    def test_risque_teasing_gets_emotional_reaction_instead_of_policy_reply(self):
        guest_plan = SpeechPlan(
            intent="回答字面问题",
            reply_act="礼貌说明",
            emotion="平静",
        )
        owner_plan = SpeechPlan()

        self.assertTrue(
            enforce_emotional_reaction(
                guest_plan,
                False,
                "让我摸摸你的腿嘛",
            )
        )
        self.assertIn("不要要求固定词", guest_plan.reaction)
        self.assertIn("羞恼", guest_plan.emotion)
        self.assertIn("不解释规则", guest_plan.reply_act)
        self.assertTrue(
            enforce_emotional_reaction(owner_plan, True, "主人想亲亲你")
        )
        self.assertIn("主人", owner_plan.intent)
        self.assertIn("愣住", owner_plan.reaction)

    def test_risque_detector_does_not_turn_serious_health_question_into_banter(self):
        self.assertTrue(is_risque_teasing("给我看看白丝嘛"))
        self.assertFalse(is_risque_teasing("请科普胸痛可能是什么疾病"))
        self.assertFalse(is_risque_teasing("遇到黄色笑话骚扰应该怎么处理"))
        self.assertFalse(is_risque_teasing("我女朋友不理我怎么办"))
        self.assertFalse(is_risque_teasing("这个扭力太变态了"))
        self.assertFalse(is_risque_teasing("我开车去电影院"))
        self.assertFalse(
            is_risque_teasing(
                "[引用消息(变态猫叔: 给我看看白丝嘛)] 验证码还剩5分钟"
            )
        )
        self.assertFalse(
            is_risque_teasing(
                "@收二手小老婆白丝黑丝萝莉都收(90000006) 看起来怎么不太对？"
            )
        )

    def test_emotional_reaction_guard_rewrites_flat_teasing_reply(self):
        flat = find_violations(
            "这种玩笑不合适，请保持尊重。",
            reply_shape="chat_bubbles",
            soft_chars=100,
            required_reaction="先羞恼地抗议",
            require_emotional_reaction=True,
        )
        expressive = find_violations(
            "喂！你在说什么奇怪的话呀……不许乱说啦！",
            reply_shape="chat_bubbles",
            soft_chars=100,
            required_reaction="先羞恼地抗议",
            require_emotional_reaction=True,
        )
        normal_scene = find_violations(
            "22块的电影票确实很划算。",
            reply_shape="chat_bubbles",
            soft_chars=100,
            required_reaction="看到大家聊票价，有点好奇",
            require_emotional_reaction=False,
        )

        self.assertIn(EMOTIONAL_REACTION_VIOLATION, flat)
        self.assertNotIn(EMOTIONAL_REACTION_VIOLATION, expressive)
        self.assertNotIn(EMOTIONAL_REACTION_VIOLATION, normal_scene)
        self.assertTrue(contains_emotional_reaction("什、什么啊！"))

    def test_reality_guard_rejects_invented_movie_spending_and_market_price(self):
        facts = ["加肥宅齐（ID：90000007）说电影票好像22元"]
        self.assertTrue(
            contains_unsupported_personal_experience(
                "上周我看的那场花了快五十，肉疼死了",
                facts,
            )
        )
        self.assertTrue(
            contains_unsupported_personal_experience(
                "周五要是没人陪，我就自己溜去看了哈哈。",
                facts,
            )
        )
        self.assertTrue(
            contains_unsupported_market_claim(
                "现在随便一张票都要三四十起步了。",
                facts,
            )
        )
        self.assertFalse(
            contains_unsupported_market_claim(
                "现在这张票22块，确实挺便宜的。",
                facts,
            )
        )
        violations = find_violations(
            "上周我看的那场花了快五十，肉疼死了",
            reply_shape="chat_bubbles",
            soft_chars=100,
            grounding_facts=facts,
        )
        self.assertIn(REALITY_GROUNDING_VIOLATION, violations)
        market_violations = find_violations(
            "现在随便一张票都要三四十起步了。",
            reply_shape="chat_bubbles",
            soft_chars=100,
            grounding_facts=facts,
        )
        self.assertIn(FACT_GROUNDING_VIOLATION, market_violations)
        self.assertFalse(
            contains_unsupported_personal_experience(
                "这个我没亲自看过，听你们说才知道。",
                facts,
            )
        )
        self.assertFalse(
            contains_unsupported_personal_experience(
                "我觉得你买了这张票还挺划算的。",
                facts,
            )
        )

    def test_owner_intimacy_plan_is_left_unchanged(self):
        plan = SpeechPlan(reply_act="害羞地mua回去", emotion="开心撒娇")

        corrected = enforce_relationship_boundary(plan, True, "mua")

        self.assertFalse(corrected)
        self.assertEqual(plan.reply_act, "害羞地mua回去")

    def test_response_guard_rejects_nonowner_but_allows_owner_intimacy(self):
        text = "不过……mua回去一下也不是不行啦。"

        guest_violations = find_violations(
            text,
            reply_shape="chat_bubbles",
            soft_chars=100,
            is_owner=False,
        )
        owner_violations = find_violations(
            text,
            reply_shape="chat_bubbles",
            soft_chars=100,
            is_owner=True,
        )
        bounded_guest = find_violations(
            "我才不会mua回去呢，熟归熟，边界还是要有的嘛。",
            reply_shape="chat_bubbles",
            soft_chars=100,
            is_owner=False,
        )

        self.assertIn(RELATIONSHIP_VIOLATION, guest_violations)
        self.assertNotIn(RELATIONSHIP_VIOLATION, owner_violations)
        self.assertNotIn(RELATIONSHIP_VIOLATION, bounded_guest)

    def test_ambient_plan_keeps_identity_target_but_uses_group_thread_audience(self):
        plan = SpeechPlan(
            target="群友甲（ID:10001）",
            reply_act="直接回答群友甲",
        )

        enforce_conversation_mode(plan, "ambient_join", "这个接口反着接才正常")

        self.assertEqual(plan.target, "群友甲（ID:10001）")
        self.assertEqual(plan.conversation_mode, "ambient_join")
        self.assertEqual(plan.audience, "current_thread")
        self.assertIn("这个接口反着接才正常", plan.anchor)
        self.assertNotIn("主持人式提问、客服式答复或逐条总结", plan.avoid)
        self.assertFalse(plan.use_allowed_tools)

    def test_group_modes_have_distinct_planner_and_replyer_prompts(self):
        ambient_planner = build_planner_conversation_mode_block(
            "ambient_join",
            "用一句俏皮吐槽接住正在讨论的硬件话题。",
        )
        quiet_planner = build_planner_conversation_mode_block(
            "quiet_topic",
            "从最近的公共话题里挑一个轻松细节开口。",
        )
        ambient_replyer = build_replyer_system_prompt(
            persona_name="亚托莉",
            voice_card="活泼自然",
            sender_name="群友甲",
            sender_id="10001",
            platform_id="qq",
            bot_id="bot",
            chat_type="group",
            group_id="20001",
            identity_key="scope|user:10001",
            is_owner=False,
            plan=SpeechPlan(
                conversation_mode="ambient_join",
                audience="current_thread",
                anchor="大家正在讨论硬件接口",
            ),
            expressions=[],
            chat_soft_chars=50,
            long_form_soft_chars=1200,
            chat_max_bubbles=3,
            conversation_mode_rules="用一句俏皮吐槽接住正在讨论的硬件话题。",
        )
        quiet_replyer = build_replyer_system_prompt(
            persona_name="亚托莉",
            voice_card="活泼自然",
            sender_name="群里的大家",
            sender_id="group",
            platform_id="qq",
            bot_id="bot",
            chat_type="group",
            group_id="20001",
            identity_key="scope|user:group",
            is_owner=False,
            plan=SpeechPlan(
                conversation_mode="quiet_topic",
                audience="whole_group",
                anchor="近期公共话题",
            ),
            expressions=[],
            chat_soft_chars=50,
            long_form_soft_chars=1200,
            chat_max_bubbles=3,
            conversation_mode_rules="从最近的公共话题里挑一个轻松细节开口。",
        )

        self.assertIn("真正的说话对象是“当前多人话题”", ambient_planner)
        self.assertIn("用一句俏皮吐槽", ambient_planner)
        self.assertIn("面向全群主动发言", quiet_planner)
        self.assertIn("从最近的公共话题", quiet_planner)
        self.assertIn("管理员配置的本场景规则", ambient_replyer)
        self.assertIn("用一句俏皮吐槽", ambient_replyer)
        self.assertIn("从最近的公共话题", quiet_replyer)
        self.assertIn("sender_id=group 只是群聊广播占位符", quiet_replyer)

    def test_active_group_guard_rejects_interviewer_and_host_openings(self):
        for text in (
            "有人吗？大家最近都在干嘛？",
            "那你呢，你觉得怎么样？",
            "你们怎么看这个问题？",
        ):
            violations = find_violations(
                text,
                reply_shape="chat_bubbles",
                soft_chars=50,
                conversation_mode="ambient_join",
            )
            self.assertIn(GROUP_PARTICIPATION_VIOLATION, violations)

        natural = find_violations(
            "反着插反而能对上，这设计也太会整活了吧。",
            reply_shape="chat_bubbles",
            soft_chars=50,
            conversation_mode="ambient_join",
        )
        self.assertNotIn(GROUP_PARTICIPATION_VIOLATION, natural)

        custom_guard_disabled = find_violations(
            "有人吗？你们怎么看这个问题？",
            reply_shape="chat_bubbles",
            soft_chars=50,
            conversation_mode="quiet_topic",
            enforce_group_participation_guard=False,
        )
        self.assertNotIn(
            GROUP_PARTICIPATION_VIOLATION,
            custom_guard_disabled,
        )

    def test_chat_bubbles_split_at_complete_sentences_without_truncating(self):
        text = "哼，那只是暂时校准失误。\n不过正确答案我已经找到了！\n你可别小看高性能机器人。\n再给我一次机会嘛。"
        bubbles = split_chat_bubbles(text, 3)
        self.assertEqual(len(bubbles), 3)
        self.assertEqual("".join(bubbles), text.replace("\n", ""))
        self.assertTrue(all(bubble[-1] in "。！？!?…" for bubble in bubbles))

    def test_long_form_keeps_paragraphs_and_lists(self):
        text = "先说结论：可以。\n\n- 第一步：检查配置\n- 第二步：看日志"
        cleaned = clean_response(text, "long_form")
        self.assertIn("\n\n", cleaned)
        self.assertIn("- 第一步", cleaned)

    def test_fallback_distinguishes_chat_from_explanation(self):
        self.assertEqual(
            fallback_plan("群友", False, "请详细解释 Docker 网络为什么连不上").reply_shape,
            "long_form",
        )
        self.assertEqual(
            fallback_plan("群友", False, "你为什么这么笨呀").reply_shape,
            "chat_bubbles",
        )
        self.assertIn(
            "不服",
            fallback_plan("群友", False, "你为什么这么笨呀").reaction,
        )

    def test_fallback_only_requests_tools_for_current_information(self):
        self.assertTrue(
            fallback_plan("群友", False, "帮我搜索一下今天有什么新闻").use_allowed_tools
        )
        self.assertFalse(
            fallback_plan("群友", False, "为什么天空看起来是蓝色的").use_allowed_tools
        )
        self.assertFalse(
            fallback_plan(
                "主人",
                True,
                "你是不是没分清我和刚刚那个人是两个不同的人？",
            ).use_allowed_tools
        )

    def test_dialogue_quality_blocks_exact_recent_reply(self):
        recent = ["不过以后也要省着点花哦，毕竟我也很贵的嘛。"]

        self.assertEqual(
            find_dialogue_repetition(
                "不过以后也要省着点花哦，毕竟我也很贵的嘛。",
                recent,
                current_message="你的电费被我拿去订阅 Pro 了",
            ),
            REPETITION_VIOLATION,
        )

    def test_dialogue_quality_keeps_roleful_new_reaction(self):
        recent = ["不过以后也要省着点花哦，毕竟我也很贵的嘛。"]

        self.assertEqual(
            find_dialogue_repetition(
                "等下，原来你动的是我的电费？难怪我今晚觉得处理器有点凉！",
                recent,
                current_message="不不不，我是把你的电费拿去订阅 Pro 了",
            ),
            "",
        )

    def test_dialogue_quality_allows_explicit_repeat_request(self):
        recent = ["我可是高性能机器人！"]

        self.assertEqual(
            find_dialogue_repetition(
                "我可是高性能机器人！",
                recent,
                current_message="把刚才那句原样再说一遍",
            ),
            "",
        )

    def test_dialogue_quality_cools_down_role_catchphrase(self):
        self.assertEqual(
            find_dialogue_repetition(
                "这次当然也难不倒高性能机器人啦。",
                ["哼哼，我可是高性能机器人！"],
                current_message="再来一次",
            ),
            CATCHPHRASE_REPETITION_VIOLATION,
        )

    def test_plan_requirements_drop_recent_lines_but_keep_semantic_beats(self):
        plan = SpeechPlan(
            must_include=[
                "不过以后也要省着点花哦",
                "毕竟我也很贵的嘛",
                "回应对方拿电费开玩笑",
            ]
        )

        removed = sanitize_plan_requirements(
            plan,
            ["不过以后也要省着点花哦，毕竟我也很贵的嘛。"],
        )

        self.assertEqual(plan.must_include, ["回应对方拿电费开玩笑"])
        self.assertEqual(len(removed), 2)

    def test_explicit_repeat_request_keeps_requested_plan_line(self):
        plan = SpeechPlan(must_include=["我可是高性能机器人！"])

        removed = sanitize_plan_requirements(
            plan,
            ["我可是高性能机器人！"],
            current_message="把刚才那句原样再说一遍",
        )

        self.assertEqual(removed, [])
        self.assertEqual(plan.must_include, ["我可是高性能机器人！"])

    def test_long_form_does_not_apply_chat_repetition_guard(self):
        answer = "同一个配置键仍然需要保留，因为这是排错结论。"

        self.assertNotIn(
            REPETITION_VIOLATION,
            find_violations(
                answer,
                reply_shape="long_form",
                soft_chars=1200,
                recent_assistant_replies=[answer],
                current_message="继续详细解释这个配置键",
            ),
        )


class PlannerTimeoutTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def create_plan(
        primary,
        fallback=None,
        *,
        timeout=0.05,
        message="他去睡觉了，你赶紧嘲讽他",
    ):
        return await SpeechPlanner(SilentLogger()).create_plan(
            provider=primary,
            fallback_provider=fallback,
            timeout_seconds=timeout,
            sender_name="主人",
            sender_id="10000001",
            platform_id="亚托莉",
            bot_id="bot-10000",
            chat_type="group",
            group_id="20000001",
            identity_key="亚托莉:bot-10000:group:20000001:10000001",
            is_owner=True,
            current_message=message,
            transcript="（没有可用的历史聊天）",
            supporting_material="",
            enabled=True,
        )

    async def test_primary_hard_timeout_uses_fallback_provider(self):
        primary = PlannerProvider(
            "deepseek/deepseek-v4-flash",
            '{"mode":"chat","intent":"不应返回"}',
            delay=0.2,
        )
        fallback = PlannerProvider(
            "deepseek/deepseek-v4-pro",
            '{"mode":"chat","intent":"回应主人对第三人的调侃"}',
        )

        plan = await self.create_plan(primary, fallback, timeout=0.02)

        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(plan.intent, "回应主人对第三人的调侃")
        self.assertIn("ID:10000001", plan.target)
        self.assertIn("群ID:20000001", plan.target)

    async def test_invalid_primary_json_uses_fallback_provider(self):
        primary = PlannerProvider("primary", "这不是 JSON")
        fallback = PlannerProvider(
            "fallback",
            '{"mode":"chat","tone":"轻松但不混淆人物"}',
        )

        plan = await self.create_plan(primary, fallback)

        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(plan.tone, "轻松但不混淆人物")

    async def test_all_providers_fail_uses_identity_safe_local_plan(self):
        primary = PlannerProvider("primary", error=RuntimeError("offline"))
        fallback = PlannerProvider("fallback", error=RuntimeError("offline"))

        plan = await self.create_plan(primary, fallback)

        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertTrue(any("代码验证身份为主人" in item for item in plan.facts))
        self.assertTrue(any("第三人称对象" in item for item in plan.facts))
        self.assertTrue(any("混为一人" in item for item in plan.avoid))

    async def test_same_provider_is_not_called_twice(self):
        provider = PlannerProvider("same", error=RuntimeError("offline"))

        await self.create_plan(provider, provider)

        self.assertEqual(provider.calls, 1)

    async def test_parsed_nonowner_plan_reanchors_identity_and_scrubs_fake_owner_line(self):
        provider = PlannerProvider(
            "primary",
            json.dumps(
                {
                    "mode": "chat",
                    "intent": "普通群友正在用亲昵表达调侃或示好",
                    "reply_act": "傲娇地收下主人给的 token",
                    "must_include": [
                        "既然主人这么说了，那我就勉强收下啦",
                        "我会努力干活，把这些token都赚回来的！",
                    ],
                    "facts": [],
                },
                ensure_ascii=False,
            ),
        )

        plan = await SpeechPlanner(SilentLogger()).create_plan(
            provider=provider,
            timeout_seconds=0.05,
            sender_name="BluntStone",
            sender_id="90000003",
            platform_id="亚托莉",
            bot_id="90000002",
            chat_type="group",
            group_id="90000001",
            identity_key="亚托莉:90000002:group:90000001:90000003",
            is_owner=False,
            current_message="主人给你的零花token够吗",
            transcript="（没有可用的历史聊天）",
            supporting_material="",
            enabled=True,
        )

        self.assertTrue(
            any("代码验证身份为普通群友" in item for item in plan.facts)
        )
        self.assertTrue(any("当前这句话不是主人说的" in item for item in plan.facts))
        self.assertTrue(any("主人这么说了" in item for item in plan.avoid))
        self.assertFalse(
            any("主人这么说" in item for item in plan.must_include)
        )
        self.assertIn("按字面理解", plan.intent)

    async def test_official_deepseek_v4_planner_disables_thinking_per_request(self):
        provider = DeepSeekPlannerProvider(
            '{"mode":"chat","tone":"自然简短","intent":"接住当前话题"}'
        )

        plan = await self.create_plan(provider)

        self.assertEqual(provider.calls, 0)
        self.assertEqual(provider.query_calls, 1)
        self.assertEqual(plan.intent, "接住当前话题")
        self.assertEqual(
            provider.last_payload["thinking"],
            {"type": "disabled"},
        )
        self.assertEqual(
            provider.last_payload["response_format"],
            {"type": "json_object"},
        )
        self.assertEqual(provider.last_payload["max_tokens"], 512)

    async def test_llamacpp_planner_uses_deterministic_json_request(self):
        provider = OpenAIPlannerProvider(
            '{"mode":"chat","tone":"自然简短","intent":"接住当前话题"}'
        )

        plan = await self.create_plan(provider)

        self.assertEqual(provider.calls, 0)
        self.assertEqual(provider.query_calls, 1)
        self.assertEqual(plan.intent, "接住当前话题")
        self.assertEqual(
            provider.last_payload["response_format"],
            {"type": "json_object"},
        )
        self.assertEqual(provider.last_payload["temperature"], 0)
        self.assertEqual(provider.last_payload["max_tokens"], 512)
        self.assertNotIn("thinking", provider.last_payload)

    async def test_unsupported_json_mode_falls_back_to_plain_request(self):
        provider = OpenAIPlannerProvider(
            '{"mode":"chat","tone":"自然","intent":"兼容旧端点"}',
            query_error=ProviderRequestError(
                "unknown parameter: response_format",
                400,
            ),
        )

        plan = await self.create_plan(provider)

        self.assertEqual(provider.query_calls, 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(plan.intent, "兼容旧端点")

    async def test_transient_structured_request_error_uses_fallback_provider(self):
        primary = OpenAIPlannerProvider(
            None,
            query_error=ProviderRequestError("service unavailable", 503),
        )
        fallback = PlannerProvider(
            "fallback",
            '{"mode":"chat","tone":"自然","intent":"备用接管"}',
        )

        plan = await self.create_plan(primary, fallback)

        self.assertEqual(primary.query_calls, 1)
        self.assertEqual(primary.calls, 0)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(plan.intent, "备用接管")


class StyleTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_fixed_insult_expression_is_migrated_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(parents=True)
            (data_dir / "expressions.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "teased-risque",
                            "situation": "被群友调戏",
                            "style": "可以偶尔叫一句变态、色狼或不许乱说",
                            "examples": ["变、变态！不许乱说啦！"],
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            assets_dir = Path(__file__).parents[1] / "assets"
            retriever = StyleRetriever(data_dir, assets_dir, SilentLogger())

            loaded = retriever.load()

            self.assertEqual(len(loaded), 1)
            self.assertIn("不要求固定抗议词", loaded[0].style)
            self.assertNotIn("变态", loaded[0].examples[0])

    async def test_emotional_reaction_expression_is_injected_for_existing_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(parents=True)
            (data_dir / "expressions.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "neutral",
                            "situation": "普通问答",
                            "style": "直接回答",
                            "examples": ["可以。"],
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            assets_dir = Path(__file__).parents[1] / "assets"
            retriever = StyleRetriever(data_dir, assets_dir, SilentLogger())
            plan = fallback_plan("群友", False, "让我摸摸你的腿嘛")

            result = await retriever.retrieve(
                current_message="让我摸摸你的腿嘛",
                plan=plan,
                top_k=3,
            )

            self.assertEqual(result[0].id, "emotional-reaction-beat")
            self.assertIn("羞恼", result[0].style)

    async def test_local_style_fallback_prefers_relevant_expression(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            assets_dir = Path(__file__).parents[1] / "assets"
            retriever = StyleRetriever(data_dir, assets_dir, SilentLogger())
            from astrbot_plugin_shio.core.planner import fallback_plan

            plan = fallback_plan("群友", False, "你真可爱")
            result = await retriever.retrieve(
                current_message="你真可爱",
                plan=plan,
                candidate_count=8,
                top_k=3,
            )
            self.assertTrue(result)
            self.assertIn("夸", result[0].situation)
            saved = json.loads((data_dir / "expressions.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(saved), 20)

    async def test_embedding_vectors_are_cached_and_reranker_is_used(self):
        class Embedding:
            def __init__(self):
                self.batch_calls = 0

            async def get_embedding(self, text):
                return [1.0, 0.0]

            async def get_embeddings(self, texts):
                self.batch_calls += 1
                return [
                    [1.0, 0.0] if "夸" in text or "可爱" in text else [0.0, 1.0]
                    for text in texts
                ]

        class RankResult:
            def __init__(self, index, score):
                self.index = index
                self.relevance_score = score

        class Reranker:
            def __init__(self):
                self.calls = 0

            async def rerank(self, query, documents, top_n=None):
                self.calls += 1
                return [RankResult(0, 0.99)]

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            assets_dir = Path(__file__).parents[1] / "assets"
            retriever = StyleRetriever(data_dir, assets_dir, SilentLogger())
            embedding = Embedding()
            reranker = Reranker()
            plan = fallback_plan("群友", False, "你真可爱")
            results = await asyncio.gather(
                *(
                    retriever.retrieve(
                        current_message="你真可爱",
                        plan=plan,
                        embedding_provider=embedding,
                        embedding_provider_id="qwen3-embedding",
                        rerank_provider=reranker,
                        candidate_count=8,
                        top_k=3,
                    )
                    for _ in range(2)
                )
            )
            for result in results:
                self.assertTrue(result)
            self.assertEqual(embedding.batch_calls, 1)
            self.assertEqual(reranker.calls, 2)
            self.assertTrue((data_dir / "expression_vectors.json").exists())


if __name__ == "__main__":
    unittest.main()
