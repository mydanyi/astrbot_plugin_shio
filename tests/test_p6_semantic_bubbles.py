from __future__ import annotations

import inspect
import unittest

try:
    from test_pipeline import main  # type: ignore[import-not-found]
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import main
from astrbot_plugin_shio.core.response_guard import split_chat_bubbles


class P6SemanticBubbleTests(unittest.TestCase):
    def test_causal_negation_and_partial_effect_stay_self_contained(self):
        cases = (
            (
                "不是我不想帮你。\n是因为现在没有权限。\n等权限恢复后我再试。",
                (
                    "不是我不想帮你。是因为现在没有权限。",
                    "等权限恢复后我再试。",
                ),
            ),
            (
                "操作没有完全成功。\n不过可能已经产生了一部分影响。\n所以我不能说完全没动。",
                (
                    "操作没有完全成功。不过可能已经产生了一部分影响。所以我不能说完全没动。",
                ),
            ),
            (
                "如果你确认。\n我才会继续执行。\n现在还没有开始。",
                (
                    "如果你确认。我才会继续执行。",
                    "现在还没有开始。",
                ),
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(tuple(split_chat_bubbles(text, 3)), expected)

    def test_quote_and_topic_reference_are_not_fragmented(self):
        self.assertEqual(
            tuple(
                split_chat_bubbles(
                    "她问“真的可以吗？”然后又补了一句。今天先这样吧。",
                    3,
                )
            ),
            (
                "她问“真的可以吗？”然后又补了一句。",
                "今天先这样吧。",
            ),
        )
        self.assertEqual(
            tuple(
                split_chat_bubbles(
                    "这里有两个原因。\n其中一个是权限不足。\n另一个是网络超时。",
                    3,
                )
            ),
            (
                "这里有两个原因。其中一个是权限不足。另一个是网络超时。",
            ),
        )

    def test_dispatch_reuses_canonical_presentation_segments_without_resplitting(self):
        source = inspect.getsource(main.ShioPlugin.dispatch_chat_bubbles)
        self.assertNotIn("split_chat_bubbles(", source)
        self.assertIn("presentation.final_segments", source)

    def test_splitter_rejects_ambiguous_limits(self):
        for value in (True, 0, 9, 3.0, "3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    split_chat_bubbles("第一句。第二句。", value)


if __name__ == "__main__":
    unittest.main()
