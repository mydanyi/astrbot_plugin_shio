"""R16 final-chain layout regressions for contiguous Plain runs."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

try:
    from test_name_semantic_provider_routing import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        MAIN,
    )
    import test_r14_reviewer_regressions as R14
    from test_r15_unicode_component_boundaries import _CLUSTER_FIXTURES
except ModuleNotFoundError:  # pragma: no cover
    from astrbot_plugin_shio.tests.test_name_semantic_provider_routing import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        MAIN,
    )
    from astrbot_plugin_shio.tests import test_r14_reviewer_regressions as R14
    from astrbot_plugin_shio.tests.test_r15_unicode_component_boundaries import (
        _CLUSTER_FIXTURES,
    )


def _allowed_cuts(clusters: tuple[str, ...]) -> set[int]:
    position = 0
    allowed = {0}
    for cluster in clusters:
        position += len(cluster)
        allowed.add(position)
    return allowed


def _run_texts(chain):
    runs: list[list[str]] = []
    current: list[str] = []
    for part in chain:
        if isinstance(part, MAIN.Plain):
            current.append(part.text)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _standard_visible_runs(chain):
    """Independently model AstrBot's per-Plain downstream ``strip`` effect."""
    visible: list[str] = []
    current: list[str] = []
    for part in chain:
        if isinstance(part, MAIN.Plain):
            current.append(part.text.strip())
        elif current:
            visible.append("".join(current))
            current = []
    if current:
        visible.append("".join(current))
    return visible


class _Event(R14._AuxiliaryEvent):
    def __init__(self, chain, *, origin="direct"):
        super().__init__()
        self._result = SimpleNamespace(chain=chain)
        self.set_extra(MAIN.SYS001_TURN_EXTRA, MAIN.create_snapshot(FakeEvent(), origin=origin))

    def get_result(self):
        return self._result


class _Image:
    pass


class _Reply:
    pass


class _AlternatePlain(MAIN.Plain):
    pass


class R16MultiPlainLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_repairs_cross_plain_uax_fixture_boundaries(self):
        for name, clusters in _CLUSTER_FIXTURES:
            with self.subTest(name=name):
                text = "".join(clusters)
                event = _Event([MAIN.Plain(char) for char in text])
                plugin = R14.R14LifecycleAndPresentationTests._layout_plugin("plugin", 1, 3)

                await plugin.layout_text_components(event)

                runs = _run_texts(event.get_result().chain)
                self.assertEqual([text], ["".join(run) for run in runs])
                offset = 0
                cuts = set()
                for piece in runs[0][:-1]:
                    offset += len(piece)
                    cuts.add(offset)
                self.assertTrue(cuts.issubset(_allowed_cuts(clusters)))

    async def test_plugin_and_model_meet_global_minimum_without_crossing_image(self):
        for mode in ("plugin", "model"):
            with self.subTest(mode=mode):
                image = _Image()
                event = _Event([MAIN.Plain("abc"), image, MAIN.Plain("def")])
                plugin = R14.R14LifecycleAndPresentationTests._layout_plugin(mode, 3, 3)
                if mode == "model":
                    # Exercise the actual model auxiliary path.  The first
                    # run receives a reversible boundary; the second invalid
                    # candidate is rejected without requiring another route.
                    provider = FakeProvider('{"segments":["a","bc"]}', "not json")
                    plugin.context = FakeContext(current=provider)
                    plugin.config["sys001"]["presentation"][
                        "model_segment_timeout_seconds"
                    ] = 1
                    plugin._auxiliary_epoch = 1
                    plugin._auxiliary_terminated = False
                    plugin._natural_generations = {}
                    plugin._natural_candidates = {}

                await plugin.layout_text_components(event)

                chain = event.get_result().chain
                self.assertEqual(3, sum(isinstance(part, MAIN.Plain) for part in chain))
                self.assertIs(image, chain[2])
                self.assertEqual("abc", "".join(part.text for part in chain[:2]))
                self.assertEqual("def", chain[3].text)
                if mode == "model":
                    self.assertEqual([None, None], [call["func_tool"] for call in provider.calls])

    async def test_impossible_global_maximum_degrades_without_moving_nontext(self):
        image = _Image()
        reply = _Reply()
        chain = [MAIN.Plain("abc"), image, MAIN.Plain("def"), reply, MAIN.Plain("ghi")]
        event = _Event(chain)

        await R14.R14LifecycleAndPresentationTests._layout_plugin("plugin", 1, 2).layout_text_components(event)

        result = event.get_result().chain
        self.assertIs(image, result[1])
        self.assertIs(reply, result[3])
        self.assertEqual(["abc", "def", "ghi"], ["".join(run) for run in _run_texts(result)])
        self.assertEqual("degraded_single", event.get_extra("shio.sys001.text_component_layout_status"))

    async def test_empty_and_mixed_plain_runs_degrade_idempotently_without_strip_loss(self):
        image = _Image()
        event = _Event([
            MAIN.Plain("1"), _AlternatePlain("\u20e3"), MAIN.Plain("2"),
            image, MAIN.Plain(" a "), MAIN.Plain(""),
        ])
        plugin = R14.R14LifecycleAndPresentationTests._layout_plugin("plugin", 3, 3)

        await plugin.layout_text_components(event)
        first = event.get_result().chain
        self.assertIs(image, first[1])
        self.assertEqual(["1\u20e32", " a "], ["".join(run) for run in _run_texts(first)])
        self.assertEqual("degraded_single", event.get_extra("shio.sys001.text_component_layout_status"))
        first_ids = [id(part) for part in first]

        await plugin.layout_text_components(event)
        self.assertEqual(first_ids, [id(part) for part in event.get_result().chain])

    async def test_safe_existing_runs_keep_identity_and_standard_visible_text(self):
        image = _Image()
        first = MAIN.Plain("a")
        second = MAIN.Plain("b")
        third = MAIN.Plain("c")
        original = [first, second, image, third]
        event = _Event(original)
        plugin = R14.R14LifecycleAndPresentationTests._layout_plugin("plugin", 2, 3)

        await plugin.layout_text_components(event)

        chain = event.get_result().chain
        self.assertEqual([first, second, image, third], chain)
        self.assertEqual(["ab", "c"], _standard_visible_runs(original))
        self.assertEqual(["ab", "c"], _standard_visible_runs(chain))
        first_ids = [id(part) for part in chain]
        await plugin.layout_text_components(event)
        self.assertEqual(first_ids, [id(part) for part in event.get_result().chain])

    async def test_safe_existing_segments_over_maximum_merge_to_exact_global_range(self):
        """A feasible down-merge stays inside its visible Plain runs."""
        image = _Image()
        first, second = MAIN.Plain("a"), MAIN.Plain("b")
        third, fourth = MAIN.Plain("c"), MAIN.Plain("d")
        original = [first, second, image, third, fourth]
        event = _Event(original)
        plugin = R14.R14LifecycleAndPresentationTests._layout_plugin("plugin", 3, 3)

        await plugin.layout_text_components(event)

        chain = event.get_result().chain
        plains = [part for part in chain if isinstance(part, MAIN.Plain)]
        self.assertEqual(3, len(plains))
        self.assertIs(image, chain[1])
        self.assertEqual("ab", chain[0].text)
        self.assertEqual("cd", "".join(part.text for part in chain[2:]))
        self.assertIsNone(event.get_extra("shio.sys001.text_component_layout_status"))
        first_ids = [id(part) for part in chain]
        await plugin.layout_text_components(event)
        self.assertEqual(first_ids, [id(part) for part in event.get_result().chain])

    async def test_degraded_whitespace_runs_preserve_standard_visible_text_and_nontext_side(self):
        image = _Image()
        original = [MAIN.Plain(" alpha "), MAIN.Plain(" "), image, MAIN.Plain(" beta "), MAIN.Plain("")]
        event = _Event(original)
        plugin = R14.R14LifecycleAndPresentationTests._layout_plugin("plugin", 3, 3)
        expected_visible = _standard_visible_runs(original)

        await plugin.layout_text_components(event)

        chain = event.get_result().chain
        self.assertEqual(expected_visible, _standard_visible_runs(chain))
        self.assertIs(image, chain[1])
        self.assertEqual("degraded_single", event.get_extra("shio.sys001.text_component_layout_status"))
