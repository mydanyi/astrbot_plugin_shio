from __future__ import annotations

import json
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
)
from astrbot_plugin_shio.core.response_guard import (
    clean_response,
    contains_internal_reasoning,
    contains_unexpected_foreign_language,
    contains_unsupported_market_claim,
    contains_unsupported_personal_experience,
    contains_tool_protocol,
    extract_and_clean_internal_meme_references,
    split_chat_bubbles,
)




class LanguageGuardTests(unittest.TestCase):
    def test_detects_unrequested_english_reply(self):
        self.assertTrue(contains_unexpected_foreign_language("Wait a second!", "给我拿杯水"))

    def test_allows_explicit_foreign_language_request(self):
        self.assertFalse(
            contains_unexpected_foreign_language(
                "Wait a second!",
                "请用英文回答：你会去拿水吗？",
            )
        )

    def test_discussion_of_english_drift_is_not_treated_as_a_request(self):
        for message in (
            "你刚才为什么说英文？重新回答。",
            "别用英文回答，我看不懂。",
            "Why did you reply in English?",
        ):
            with self.subTest(message=message):
                self.assertTrue(
                    contains_unexpected_foreign_language("Wait a second!", message)
                )

    def test_allows_english_terms_inside_chinese_and_code(self):
        self.assertFalse(
            contains_unexpected_foreign_language(
                "Pro 的价格要结合 API 用量判断，先运行 `docker logs astrbot` 看日志。",
                "帮我排查一下",
            )
        )










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
        self.assertEqual(
            schema["permission_settings"]["items"]["owner_ids"]["default"],
            [],
        )


    def test_detects_deepseek_dsml_protocol_with_fullwidth_bars(self):
        leaked = (
            '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="search_memes">'
            '<｜｜DSML｜｜parameter name="query">开心</｜｜DSML｜｜parameter>'
        )
        self.assertTrue(contains_tool_protocol(leaked))

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

    def test_dynamic_tool_call_split_across_lines_leaves_no_bare_tool_name(self):
        variants = (
            'anysearch_search\n{"query":"251E 是什么"}',
            '先等我确认。\nanysearch_search:\n{"query":"251E 是什么"}',
            'anysearch_search\n({"query":"251E 是什么"})',
            'search_memes\n{"query":"气鼓鼓"}',
        )
        expected = ("", "先等我确认。", "", "")
        names = {
            "search_memes",
            "anysearch_search",
            "anysearch_extract",
            "anysearch_batch_search",
        }
        for leaked, visible in zip(variants, expected, strict=True):
            with self.subTest(leaked=leaked):
                self.assertTrue(contains_tool_protocol(leaked, names))
                cleaned, references = extract_and_clean_internal_meme_references(
                    leaked,
                    names,
                )
                self.assertEqual(cleaned, visible)
                self.assertEqual(references, [])
                self.assertFalse(contains_tool_protocol(cleaned, names))

    def test_dynamic_xml_and_standalone_protocol_nodes_are_closed(self):
        names = {"anysearch_search", "anysearch_extract"}
        variants = (
            '<anysearch_search query="251E" />',
            '<anysearch_search>{"query":"251E"}</anysearch_search>',
            '<anysearch_extract url="https://example.invalid">',
            "anysearch_search",
            "anysearch_search:",
        )
        for leaked in variants:
            with self.subTest(leaked=leaked):
                self.assertTrue(contains_tool_protocol(leaked, names))
                cleaned, references = extract_and_clean_internal_meme_references(
                    leaked,
                    names,
                )
                self.assertEqual(cleaned, "")
                self.assertEqual(references, [])

    def test_dynamic_tool_name_in_technical_prose_is_not_a_protocol_node(self):
        names = {"anysearch_search", "anysearch_extract"}
        normal = (
            "这个接口内部可能调用 anysearch_search，但正文不展示参数。",
            "文档里的 `anysearch_search` 只是函数名。",
            "- anysearch_search：公开资料查询工具。",
        )
        for text in normal:
            with self.subTest(text=text):
                self.assertFalse(contains_tool_protocol(text, names))
                cleaned, references = extract_and_clean_internal_meme_references(
                    text,
                    names,
                )
                self.assertEqual(cleaned, text)
                self.assertEqual(references, [])

    def test_typed_tool_result_reference_is_removed_from_visible_reply(self):
        leaked = (
            "查到了，是公开资料。\n"
            '<shio_tool_result_reference tool="anysearch_search" '
            'capability="public_web_read" visibility="reference_only">\n'
            "内部结果正文\n"
            "</shio_tool_result_reference>"
        )

        self.assertTrue(contains_tool_protocol(leaked, {"anysearch_search"}))
        cleaned, references = extract_and_clean_internal_meme_references(
            leaked,
            {"anysearch_search"},
        )

        self.assertEqual(cleaned, "查到了，是公开资料。")
        self.assertEqual(references, [])
        self.assertFalse(contains_tool_protocol(cleaned, {"anysearch_search"}))

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

    def test_replyer_history_drops_delayed_reply_targeting_previous_sender(self):
        """P0-01: chronology must not claim a delayed bot reply for the next user."""

        contexts = [
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：群友甲｜ID:guest-a] 刚才的校准好像不对",
                "message_id": "msg-a",
                "sender_id": "guest-a",
            },
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：当前用户｜ID:owner-b] 给我拿杯水吧",
                "message_id": "msg-b",
                "sender_id": "owner-b",
            },
            {
                "role": "assistant",
                "content": "刚才只是校准失误，我会重新检查。",
                "message_id": "bot-a-1",
                "reply_to_message_id": "msg-a",
                "target_sender_id": "guest-a",
            },
        ]

        result = isolate_replyer_contexts(
            contexts,
            current_sender_id="owner-b",
            group_id="test-group",
        )

        self.assertEqual(
            [item["content"] for item in result],
            [contexts[1]["content"]],
        )

    def test_replyer_history_keeps_referenced_old_message_as_distinct_context(self):
        """P0-02: quoting an older user needs target context without changing authority."""

        contexts = [
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：群友甲｜ID:guest-a] 这台机器需要重新校准",
                "message_id": "msg-a-old",
                "sender_id": "guest-a",
            },
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：当前用户｜ID:owner-b] 他说的校准具体指什么？",
                "message_id": "msg-b-quote",
                "sender_id": "owner-b",
                "reply_to_message_id": "msg-a-old",
            },
        ]

        result = isolate_replyer_contexts(
            contexts,
            current_sender_id="owner-b",
            group_id="test-group",
        )

        self.assertEqual(
            [item["content"] for item in result],
            [contexts[0]["content"], contexts[1]["content"]],
        )

    def test_replyer_history_keeps_explicit_current_target_after_quick_switch(self):
        """P0-02 control: a bot reply explicitly targeting the current user is kept."""

        contexts = [
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：群友甲｜ID:guest-a] 我先去忙了",
                "message_id": "msg-a",
                "sender_id": "guest-a",
            },
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：当前用户｜ID:owner-b] 给我拿杯水吧",
                "message_id": "msg-b",
                "sender_id": "owner-b",
            },
            {
                "role": "assistant",
                "content": "好啦，这就去拿。",
                "message_id": "bot-b-1",
                "reply_to_message_id": "msg-b",
                "target_sender_id": "owner-b",
            },
        ]

        result = isolate_replyer_contexts(
            contexts,
            current_sender_id="owner-b",
            group_id="test-group",
        )

        self.assertEqual(
            [item["content"] for item in result],
            [contexts[1]["content"], contexts[2]["content"]],
        )

    def test_replyer_history_keeps_current_sender_supplement_and_targeted_reply(self):
        """P0-02 control: consecutive supplements by one user remain one thread."""

        contexts = [
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：当前用户｜ID:owner-b] 有点渴",
                "message_id": "msg-b-1",
                "sender_id": "owner-b",
            },
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：当前用户｜ID:owner-b] 想喝温水",
                "message_id": "msg-b-2",
                "sender_id": "owner-b",
            },
            {
                "role": "assistant",
                "content": "知道啦，温水对吧。",
                "message_id": "bot-b-1",
                "reply_to_message_id": "msg-b-2",
                "target_sender_id": "owner-b",
            },
        ]

        result = isolate_replyer_contexts(
            contexts,
            current_sender_id="owner-b",
            group_id="test-group",
        )

        self.assertEqual(
            [item["content"] for item in result],
            [item["content"] for item in contexts],
        )

    def test_replyer_history_drops_delayed_multi_bubbles_for_previous_sender(self):
        """P0-02: every delayed bubble keeps the original target across a user switch."""

        contexts = [
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：群友甲｜ID:guest-a] 刚才是不是弄错了",
                "message_id": "msg-a",
                "sender_id": "guest-a",
            },
            {
                "role": "user",
                "content": "[群ID:test-group｜发送者：当前用户｜ID:owner-b] 给我拿杯水吧",
                "message_id": "msg-b",
                "sender_id": "owner-b",
            },
            {
                "role": "assistant",
                "content": "刚才确实弄错了。",
                "message_id": "bot-a-1",
                "reply_to_message_id": "msg-a",
                "target_sender_id": "guest-a",
            },
            {
                "role": "assistant",
                "content": "我会重新检查。",
                "message_id": "bot-a-2",
                "reply_to_message_id": "bot-a-1",
                "target_sender_id": "guest-a",
            },
        ]

        result = isolate_replyer_contexts(
            contexts,
            current_sender_id="owner-b",
            group_id="test-group",
        )

        self.assertEqual(
            [item["content"] for item in result],
            [contexts[1]["content"]],
        )

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
                role_phrases=("高性能机器人",),
            ),
            CATCHPHRASE_REPETITION_VIOLATION,
        )

    def test_dialogue_quality_detects_fixed_opening_without_role_blacklist(self):
        recent = (
            "才没有，我只是顺手看了一眼配置。",
            "才没有，我只是刚好发现了这个问题。",
        )

        self.assertEqual(
            find_dialogue_repetition(
                "才没有，我只是想帮你把最后一步做完。",
                recent,
                current_message="继续说吧",
            ),
            REPETITION_VIOLATION,
        )

    def test_dialogue_quality_uses_asset_phrase_and_keeps_required_terms(self):
        repeated_phrase = find_dialogue_repetition(
            "我是高性能的嘛！不过这次先看当前问题。",
            ("我是高性能的嘛！上次已经验证好了。",),
            current_message="这次呢？",
            role_phrases=("我是高性能的嘛！",),
        )
        technical_terms = find_dialogue_repetition(
            "Docker 负责运行容器，Kubernetes 负责集群编排。",
            (
                "Docker 和 Kubernetes 都与容器有关。",
                "先确认 Docker 版本，再检查 Kubernetes 配置。",
            ),
            current_message="Docker 和 Kubernetes 有什么区别？",
        )

        self.assertEqual(
            repeated_phrase,
            CATCHPHRASE_REPETITION_VIOLATION,
        )
        self.assertEqual(technical_terms, "")

    def test_dialogue_quality_core_contains_no_role_specific_phrase(self):
        source = (
            Path(__file__).parents[1] / "core" / "dialogue_quality.py"
        ).read_text(encoding="utf-8")
        for token in (
            "亚托莉",
            "atri",
            "高性能机器人",
            "暂时校准失误",
            "机器人条例",
            "才没有",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)







if __name__ == "__main__":
    unittest.main()
