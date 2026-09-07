"""Only Meme's auxiliary selector may supply IDs in auxiliary mode."""
from types import SimpleNamespace
import unittest

from test_name_semantic_provider_routing import MAIN


class MemeAuxiliaryBoundaryTests(unittest.IsolatedAsyncioTestCase):
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
