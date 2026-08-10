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
from astrbot_plugin_shio.core.planner import (
    SpeechPlanner,
    fallback_plan,
    parse_json_object,
)
from astrbot_plugin_shio.core.response_guard import (
    IDENTITY_VIOLATION,
    TOOL_PROTOCOL_VIOLATION,
    clean_response,
    contains_tool_protocol,
    extract_and_clean_internal_meme_references,
    find_violations,
    split_chat_bubbles,
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

        allowed_third_person = find_violations(
            "这件事得让Master本人点头才行。",
            reply_shape="chat_bubbles",
            soft_chars=100,
            is_owner=False,
        )
        self.assertNotIn(IDENTITY_VIOLATION, allowed_third_person)

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
