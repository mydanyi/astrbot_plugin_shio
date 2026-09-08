"""Regression: a QQ bubble must not consist of a detached code fence."""
import unittest
import test_name_semantic_provider_routing as fixtures

M = fixtures.MAIN


class CodeFenceBubblesTests(unittest.TestCase):
    def test_real_reply_shape_keeps_fence_and_body_together(self):
        text = '可以这样：\n\n```python\nnums = [3, 1, 2, 3, 1]\nprint(list(dict.fromkeys(nums)))\n```\n\n保留原顺序。'
        self.assertEqual(['可以这样：\n\n', '```python\nnums = [3, 1, 2, 3, 1]\nprint(list(dict.fromkeys(nums)))\n```\n\n', '保留原顺序。'],
                         M.split_text_components(text, maximum=3))

    def test_code_block_never_splits_to_meet_minimum(self):
        text = '```python\nprint("hello!")\n```'
        self.assertEqual([text], M.split_text_components(text, minimum=3, maximum=3))

    def test_unclosed_and_tilde_fences_and_embedded_shorter_fences(self):
        blocks = ['```python\nprint("hello!")\nx = 2', '~~~python\nprint("hello!")\n~~~',
                  '````markdown\n```python\nx = 2\n```\n````', '```python\nx = 2\n``` # not a closing fence\ny = 3\n```']
        for block in blocks:
            with self.subTest(block=block):
                self.assertEqual([block], M.split_text_components(block, maximum=3))

    def test_model_and_existing_components_cannot_cut_inside_code(self):
        text = '可以这样：\n\n```python\nx = 1\n```'
        self.assertFalse(M.text_component_boundaries_are_safe(text, ['可以这样：\n\n', '```python\n', 'x = 1\n```']))
        self.assertTrue(M.text_component_boundaries_are_safe(text, ['可以这样：\n\n', '```python\nx = 1\n```']))

    def test_surrounding_prose_still_splits_and_code_contents_stay_exact(self):
        text = '前句。后句。\n```python\n  x = "?!"\n\n  y = "。"\n```\n结束。'
        pieces = M.split_text_components(text, maximum=5)
        self.assertEqual(['前句。', '后句。\n', '```python\n  x = "?!"\n\n  y = "。"\n```\n', '结束。'], pieces)
        self.assertEqual(text, ''.join(pieces))

    def test_inline_code_does_not_turn_rest_of_message_into_fenced_block(self):
        text = '使用 `dict.fromkeys`。然后打印。'
        self.assertEqual(['使用 `dict.fromkeys`。', '然后打印。'], M.split_text_components(text, maximum=3))
