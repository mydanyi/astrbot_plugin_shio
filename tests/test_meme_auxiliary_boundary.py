"""Only Meme's auxiliary selector may supply IDs in auxiliary mode."""
from types import SimpleNamespace
import unittest

from test_name_semantic_provider_routing import MAIN


class MemeAuxiliaryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_square_marker_is_removed_from_text_and_plain_chain(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        event = SimpleNamespace(get_extra=lambda key, default=None: "llm" if key == "meme_manager_semantic_mode" else default)
        text = "Master 第一名啦！\n超厉害的！\n恭喜恭喜，今天一定超开心吧～\n[meme:1]"
        want = "Master 第一名啦！\n超厉害的！\n恭喜恭喜，今天一定超开心吧～"
        image = SimpleNamespace(type="image", file="selected-by-meme.png")
        response = SimpleNamespace(completion_text=text, result_chain=SimpleNamespace(chain=[MAIN.Plain(text), image]))
        await plugin.prepare_meme_auxiliary_response(event, response)
        self.assertEqual(want, response.completion_text)
        self.assertEqual(want, response.result_chain.chain[0].text)
        self.assertIs(image, response.result_chain.chain[1])

    async def test_square_marker_cleanup_does_not_leave_an_empty_line_between_sentences(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        event = SimpleNamespace(get_extra=lambda key, default=None: "llm" if key == "meme_manager_semantic_mode" else default)
        response = SimpleNamespace(completion_text="第一句。\n[meme:1]\n第二句。", result_chain=None)
        await plugin.prepare_meme_auxiliary_response(event, response)
        self.assertEqual("第一句。\n第二句。", response.completion_text)

    async def test_square_marker_examples_and_unrelated_brackets_are_preserved(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        samples = [
            ("示例 `[meme:1]`，不要当作配图。", ""),
            ("```text\n[meme:1]\n```", ""),
            ("缩进代码示例：\n\n    [meme:1]\n\n示例结束。", ""),
            ("缩进代码示例：\n\n\tvalue = [meme:1]\n\n示例结束。", ""),
            ("[meme:1]", "请原样输出 [meme:1]。"),
            ("[开心] [Image] meme 是个普通词。", ""),
        ]
        for text, user_text in samples:
            with self.subTest(text=text):
                event = SimpleNamespace(
                    get_extra=lambda key, default=None: "llm" if key == "meme_manager_semantic_mode" else default,
                    get_message_str=lambda: user_text,
                )
                response = SimpleNamespace(completion_text=text, result_chain=None)
                await plugin.prepare_meme_auxiliary_response(event, response)
                self.assertEqual(text, response.completion_text)

    async def test_tool_mode_does_not_let_shio_reinterpret_square_markers(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        event = SimpleNamespace(get_extra=lambda key, default=None: "tool" if key == "meme_manager_semantic_mode" else default)
        response = SimpleNamespace(completion_text="工具输出示例 [meme:1]", result_chain=None)
        await plugin.prepare_meme_auxiliary_response(event, response)
        self.assertEqual("工具输出示例 [meme:1]", response.completion_text)

    async def test_auxiliary_mode_does_not_accept_main_model_old_ids(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        event = SimpleNamespace(get_extra=lambda key, default=None: "llm" if key == "meme_manager_semantic_mode" else default)
        response = SimpleNamespace(completion_text="来了。\n&&meme:8ba2b520ca06&&", result_chain=None)
        await plugin.prepare_meme_auxiliary_response(event, response)
        self.assertEqual("来了。", response.completion_text)

    async def test_tool_mode_keeps_main_model_selection(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        event = SimpleNamespace(get_extra=lambda key, default=None: "tool" if key == "meme_manager_semantic_mode" else default)
        response = SimpleNamespace(completion_text="来了。\n&&meme:8ba2b520ca06&&", result_chain=None)
        await plugin.prepare_meme_auxiliary_response(event, response)
        self.assertEqual("来了。\n&&meme:8ba2b520ca06&&", response.completion_text)

    async def test_absent_meme_does_not_clean_user_facing_text(self):
        plugin = MAIN.ShioPlugin.__new__(MAIN.ShioPlugin)
        event = SimpleNamespace(get_extra=lambda key, default=None: default)
        response = SimpleNamespace(completion_text="示例 &&meme:abc123&&", result_chain=None)
        await plugin.prepare_meme_auxiliary_response(event, response)
        self.assertEqual("示例 &&meme:abc123&&", response.completion_text)
