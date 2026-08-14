from __future__ import annotations

import unittest

from astrbot_plugin_shio.core.name_wake import classify_name_wake


class NameWakeTests(unittest.TestCase):
    def test_sentence_end_name_is_direct(self):
        decision = classify_name_wake("这个你怎么看亚托莉", ["亚托莉"])
        self.assertTrue(decision.is_direct)
        self.assertEqual(decision.alias, "亚托莉")

    def test_middle_name_with_question_is_direct(self):
        decision = classify_name_wake("这个问题，亚托莉你能解释一下吗？", ["亚托莉"])
        self.assertTrue(decision.is_direct)

    def test_plain_character_mention_is_not_direct(self):
        decision = classify_name_wake("这个表情很像亚托莉原作里的样子", ["亚托莉"])
        self.assertEqual(decision.kind, "mention")

    def test_title_url_and_code_do_not_force_wake(self):
        self.assertEqual(classify_name_wake("我买了《ATRI》", ["ATRI"]).kind, "mention")
        self.assertEqual(
            classify_name_wake("https://example.com/ATRI", ["ATRI"]).kind,
            "none",
        )
        self.assertEqual(classify_name_wake("`persona=ATRI`", ["ATRI"]).kind, "none")

    def test_ascii_alias_does_not_match_inside_another_word(self):
        self.assertEqual(classify_name_wake("matrix", ["ATRI"]).kind, "none")

    def test_contains_mode_is_explicitly_more_aggressive(self):
        decision = classify_name_wake(
            "我刚才提到了ATRi的发型",
            ["ATRI"],
            mode="contains",
        )
        self.assertTrue(decision.is_direct)


if __name__ == "__main__":
    unittest.main()
