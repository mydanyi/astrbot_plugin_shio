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
)
from astrbot_plugin_shio.core.planner import fallback_plan, parse_json_object
from astrbot_plugin_shio.core.response_guard import (
    clean_response,
    find_violations,
    split_chat_bubbles,
)
from astrbot_plugin_shio.core.style_retriever import StyleRetriever


class SilentLogger:
    def warning(self, *args, **kwargs):
        pass


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

    def test_owner_task_has_safe_local_fallback(self):
        plan = fallback_plan("主人", True, "帮我运行这个程序并查看服务器日志")
        self.assertEqual(plan.mode, "task")
        nonowner = fallback_plan("群友", False, "帮我运行这个程序并查看服务器日志")
        self.assertEqual(nonowner.mode, "chat")

    def test_empty_event_falls_back_to_native_request_prompt(self):
        self.assertEqual(get_current_message(Event(), "请看一下这张图片"), "请看一下这张图片")

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
        self.assertEqual(result, [{"role": "user", "content": "这是之前某位群友说的话"}])
        self.assertEqual(
            contexts_as_transcript(result),
            "未标注身份的历史用户：这是之前某位群友说的话",
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


class StyleTests(unittest.IsolatedAsyncioTestCase):
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
