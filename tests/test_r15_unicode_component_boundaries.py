"""R15 regression: Shio must not cut inside an extended grapheme cluster.

The oracle deliberately lists fixed UAX #29 cluster fixtures.  Permitted
code-point boundaries are derived from those recorded clusters, rather than
from Shio's conservative standard-library classifier.
"""
from __future__ import annotations

import unittest

try:
    from test_name_semantic_provider_routing import MAIN, SYS001
    import test_r14_reviewer_regressions as R14
except ModuleNotFoundError:  # pragma: no cover - package discovery path
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        MAIN,
        SYS001,
    )
    from astrbot_plugin_shio.tests import test_r14_reviewer_regressions as R14


# These are explicit recorded UAX #29 cluster examples.  Keycap samples have
# a keycap cluster followed by ordinary ``2``; every other fixture below is a
# single cluster.  The test derives legal cumulative cut positions directly.
_CLUSTER_FIXTURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("keycap", ("1\u20e3", "2")),
    ("keycap_with_vs", ("1\ufe0f\u20e3", "2")),
    ("hangul_lvt_jamo", ("\u1100\u1161\u11a8",)),
    ("hangul_lv_plus_t", ("\uac00\u11a8",)),
    (
        "emoji_tag",
        (
            "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e"
            "\U000e0067\U000e007f",
        ),
    ),
    ("spacing_mark_mc", ("\u0915\u093e",)),
    ("enclosing_mark_me", ("1\u20e3",)),
    ("zwj", ("\U0001f469\u200d\U0001f4bb",)),
    ("regional_indicators", ("\U0001f1e8\U0001f1f3",)),
    ("variation_selector", ("\u2708\ufe0f",)),
    ("emoji_modifier", ("\U0001f44d\U0001f3fd",)),
    ("combining_mark", ("e\u0301",)),
    ("crlf", ("\r\n",)),
)


def _actual_boundaries(pieces: list[str]) -> set[int]:
    """Return produced code-point cuts, excluding the two text endpoints."""
    position = 0
    boundaries: set[int] = set()
    for piece in pieces[:-1]:
        position += len(piece)
        boundaries.add(position)
    return boundaries


def _fixture_boundaries(clusters: tuple[str, ...]) -> frozenset[int]:
    """Derive legal cuts from the fixed recorded UAX #29 cluster sequence."""
    position = 0
    boundaries = {0}
    for cluster in clusters:
        position += len(cluster)
        boundaries.add(position)
    return frozenset(boundaries)


class R15UnicodeComponentBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_cluster_fixtures_allow_no_internal_plain_boundary(self):
        for name, clusters in _CLUSTER_FIXTURES:
            with self.subTest(name=name):
                text = "".join(clusters)
                allowed = _fixture_boundaries(clusters)
                pieces = SYS001.split_text_components(text, minimum=2, maximum=3)
                self.assertEqual(text, "".join(pieces))
                self.assertTrue(all(pieces))
                self.assertTrue(
                    _actual_boundaries(pieces).issubset(allowed),
                    f"{name} was cut at {_actual_boundaries(pieces) - allowed}",
                )

    def test_ascii_and_cjk_still_reach_the_global_minimum_losslessly(self):
        for text in ("abcdef", "甲。乙甲乙"):
            with self.subTest(text=text):
                pieces = SYS001.split_text_components(text, minimum=3, maximum=3)
                self.assertEqual(3, len(pieces))
                self.assertEqual(text, "".join(pieces))
                self.assertTrue(all(pieces))
                self.assertTrue(SYS001.text_components_survive_standard_strip(pieces))

    def test_whitespace_sensitive_text_degrades_to_one_plain_without_loss(self):
        text = " a b "
        pieces = SYS001.split_text_components(text, minimum=3, maximum=3)
        self.assertEqual([text], pieces)

    async def test_layout_degrades_complex_plugin_and_model_boundaries_in_place(self):
        text = "1\u20e32"
        before = object()
        after = object()
        for origin in ("private", "name", "natural"):
            with self.subTest(mode="plugin", origin=origin):
                event = R14.R14LifecycleAndPresentationTests._layout_event(
                    text, origin=origin, before=(before,), after=(after,)
                )
                await R14.R14LifecycleAndPresentationTests._layout_plugin(
                    "plugin", 2, 3
                ).layout_text_components(event)
                self.assertEqual([before, event.get_result().chain[1], after], event.get_result().chain)
                self.assertEqual(text, event.get_result().chain[1].text)
                self.assertEqual(
                    "degraded_single",
                    event.get_extra("shio.sys001.text_component_layout_status"),
                )

            with self.subTest(mode="model", origin=origin):
                event = R14.R14LifecycleAndPresentationTests._layout_event(
                    text, origin=origin, before=(before,), after=(after,)
                )
                plugin = R14.R14LifecycleAndPresentationTests._layout_plugin(
                    "model", 2, 3
                )

                async def unsafe_model(*_args, **_kwargs):
                    return ["1", "\u20e32"]

                plugin._model_text_components = unsafe_model
                await plugin.layout_text_components(event)
                self.assertEqual([before, event.get_result().chain[1], after], event.get_result().chain)
                self.assertEqual(text, event.get_result().chain[1].text)
                self.assertEqual(
                    "degraded_single",
                    event.get_extra("shio.sys001.text_component_layout_status"),
                )
