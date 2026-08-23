from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from astrbot_plugin_shio.core.contracts import HistoryVisibility
from astrbot_plugin_shio.tests.harness.plugin_contracts import (
    ConformanceLevel,
    PluginId,
    PluginObservation,
    PluginState,
    evaluate_plugin,
    production_plugin_contracts,
)
from astrbot_plugin_shio.tests.harness.privacy import scan_fixture_tree

try:
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeStarTools,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeEvent,
        FakeProvider,
        FakeRequest,
        FakeStarTools,
        main,
    )


ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "p10"
FIXTURE_PATH = FIXTURE_ROOT / "plugin_multimodal.json"


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("p10_plugin_fixture_duplicate_or_invalid_key")
        result[key] = value
    return result


def _load_fixture():
    return json.loads(
        FIXTURE_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("p10_plugin_fixture_nonfinite")
        ),
    )


class P10PluginMultimodalTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = _load_fixture()

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def test_matrix_is_closed_complete_and_privacy_safe(self):
        self.assertEqual(
            set(self.fixture),
            {
                "schema_version",
                "privacy",
                "plugin_matrix",
                "gate_matrix",
                "flow_matrix",
                "production_observation",
            },
        )
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(
            self.fixture["privacy"],
            "production_shaped_redacted_only",
        )
        privacy = scan_fixture_tree(FIXTURE_ROOT)
        self.assertTrue(privacy.is_safe, privacy.issue_codes)
        self.assertEqual(
            {row["plugin_id"] for row in self.fixture["plugin_matrix"]},
            {plugin.value for plugin in PluginId},
        )
        states = {
            plugin.value: {
                row["state"]
                for row in self.fixture["plugin_matrix"]
                if row["plugin_id"] == plugin.value
            }
            for plugin in PluginId
        }
        for plugin_id, covered in states.items():
            with self.subTest(plugin=plugin_id):
                self.assertIn("present", covered)
                self.assertTrue(covered.intersection(
                    {"missing", "disabled", "timeout", "error", "interface_changed"}
                ))
        self.assertEqual(
            {row["surface"] for row in self.fixture["flow_matrix"]},
            {
                "direct_text",
                "quoted_text",
                "quoted_image",
                "image_only",
                "same_media_repair",
            },
        )

    def test_plugin_rows_run_through_real_conformance_evaluator(self):
        contracts = production_plugin_contracts()
        for row in self.fixture["plugin_matrix"]:
            plugin_id = PluginId(row["plugin_id"])
            state = PluginState(row["state"])
            contract = contracts[plugin_id]
            if state is PluginState.PRESENT:
                observation = PluginObservation(
                    plugin_id=plugin_id,
                    state=state,
                    interface_token=contract.interface_token,
                    registered_hooks=contract.required_hooks,
                    evidence_kind=contract.evidence_kind,
                    history_visibility=next(iter(contract.allowed_history)),
                    consumers=tuple(contract.allowed_consumers),
                    effects=tuple(contract.max_effect_counts),
                    safe_degradation=True,
                )
            else:
                observation = PluginObservation(
                    plugin_id=plugin_id,
                    state=state,
                    interface_token=(
                        "changed.v2"
                        if state is PluginState.INTERFACE_CHANGED
                        else ""
                    ),
                    registered_hooks=(),
                    evidence_kind=contract.evidence_kind,
                    history_visibility=HistoryVisibility.DROP,
                    consumers=(),
                    effects=(),
                    safe_degradation=True,
                )
            report = evaluate_plugin(contract, observation)
            with self.subTest(case=row["case_id"]):
                self.assertIs(report.level, ConformanceLevel(row["expected_level"]))

    def test_every_row_resolves_to_one_executable_regression(self):
        rows = (
            *self.fixture["plugin_matrix"],
            *self.fixture["gate_matrix"],
            *self.fixture["flow_matrix"],
        )
        for row in rows:
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromName(row["evidence_test_id"])
            with self.subTest(case=row["case_id"]):
                self.assertEqual(suite.countTestCases(), 1)
                self.assertEqual(tuple(getattr(loader, "errors", ())), ())

    def test_read_only_production_inventory_is_bounded_and_not_authority(self):
        observation = self.fixture["production_observation"]
        self.assertFalse(observation["candidate_deployed"])
        self.assertEqual(observation["container"], {
            "running": True,
            "restart_count": 0,
            "webui_status": 200,
        })
        self.assertEqual(
            {plugin["plugin_id"] for plugin in observation["plugins"]},
            {
                "livingmemory",
                "anysearch",
                "meme_manager",
                "reneban",
                "parser",
                "group_verification",
                "recall_cancel",
                "keywords_reply",
            },
        )
        self.assertTrue(all(plugin["present"] for plugin in observation["plugins"]))
        self.assertTrue(all(
            type(plugin["log_reference_count_24h"]) is int
            and plugin["log_reference_count_24h"] > 0
            for plugin in observation["plugins"]
        ))
        self.assertTrue(observation["meme_build"]["profile_matches"])
        self.assertIn(
            "inventory_presence_is_not_runtime_authority",
            observation["limitations"],
        )
        self.assertIn("x01_external_capture_timing", observation["limitations"])

    async def test_image_only_turn_uses_exact_production_media_chain(self):
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {
                "persona_name": "亚托莉",
                "prefer_livingmemory_group_history": False,
            },
        )
        event = FakeEvent("guest-image", "", group_id="p10-image-group")
        event.message_id = "p10-image-only-current"
        event.is_at_or_wake_command = True
        event.get_messages = lambda: [types.SimpleNamespace(type="Image")]
        request = FakeRequest("")
        request.image_urls = ["https://transport.invalid/p10-image-only.png"]
        try:
            await plugin.enforce_agent_permission(event, request)
            await plugin.build_persona_reply(event, request)

            adaptation = event.get_extra(main.SHIO_MEDIA_ADAPTATION)
            anchor = event.get_extra(main.SHIO_CURRENT_QUESTION_ANCHOR)
            intent = event.get_extra(main.SHIO_CONTENT_INTENT)
            composer = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
            self.assertTrue(event.get_extra(main.SHIO_TYPED_PIPELINE_ACTIVE))
            self.assertEqual(len(adaptation.context.items), 1)
            item = adaptation.context.items[0]
            self.assertEqual(item.origin.value, "direct")
            self.assertEqual(item.source_message_id, event.message_id)
            self.assertEqual(anchor.media_item_ids, (item.item_id,))
            self.assertEqual(intent.required_media_item_ids, (item.item_id,))
            self.assertIs(composer.media_context, adaptation.context)
            self.assertIn('"origin":"direct"', request.prompt)
            self.assertNotIn(request.image_urls[0], request.prompt)
            self.assertEqual(
                request.image_urls,
                ["https://transport.invalid/p10-image-only.png"],
            )
        finally:
            await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
