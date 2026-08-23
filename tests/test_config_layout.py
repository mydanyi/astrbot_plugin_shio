from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    from test_pipeline import (  # type: ignore[import-not-found]
        FakeContext,
        FakeProvider,
        FakeStarTools,
        main,
    )
except ModuleNotFoundError:
    from astrbot_plugin_shio.tests.test_pipeline import (
        FakeContext,
        FakeProvider,
        FakeStarTools,
        main,
    )

from astrbot_plugin_shio.scripts.migrate_v054_config import (
    GROUPS,
    NEW_PARTICIPATION_DEFAULTS,
    migrate,
)


class ConfigLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(
            (Path(main.__file__).parent / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )

    def test_schema_runtime_and_migration_share_one_closed_group_layout(self):
        self.assertEqual(tuple(self.schema), tuple(GROUPS))
        schema_keys = {
            group: tuple(section["items"])
            for group, section in self.schema.items()
        }
        self.assertEqual(schema_keys, GROUPS)
        self.assertEqual(main._CONFIG_GROUP_BY_KEY, {
            key: group for group, keys in GROUPS.items() for key in keys
        })
        self.assertEqual(sum(map(len, GROUPS.values())), 57)

    def test_legacy_values_migrate_without_reactivating_either_proactive_flow(self):
        new_only = {
            *NEW_PARTICIPATION_DEFAULTS,
            "natural_group_participation_allowlist",
        }
        legacy = {
            key: field["default"]
            for section in self.schema.values()
            for key, field in section["items"].items()
            if key not in new_only
        }
        legacy["proactive_group_allowlist"] = ["group-marker"]
        legacy["proactive_initiation_enabled"] = False
        legacy["proactive_scheduler_interval_seconds"] = 3600
        legacy["meme_complement_cadence_turns"] = 0
        legacy["meme_complement_cooldown_turns"] = 0

        migrated = migrate(legacy)

        self.assertFalse(
            migrated["proactive_settings"]["proactive_initiation_enabled"]
        )
        self.assertFalse(
            migrated["participation_settings"][
                "natural_group_participation_enabled"
            ]
        )
        self.assertEqual(
            migrated["participation_settings"][
                "natural_group_participation_allowlist"
            ],
            ["group-marker"],
        )
        self.assertEqual(
            migrated["proactive_settings"][
                "proactive_scheduler_interval_seconds"
            ],
            60,
        )
        self.assertEqual(
            migrated["meme_settings"]["meme_complement_cadence_turns"],
            1,
        )
        self.assertEqual(
            migrated["meme_settings"]["meme_complement_cooldown_turns"],
            1,
        )

    def test_runtime_reads_grouped_config_first_and_keeps_flat_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            FakeStarTools.data_dir = Path(root)
            plugin = main.ShioPlugin(
                FakeContext(FakeProvider([])),
                {
                    "proactive_settings": {
                        "proactive_initiation_enabled": False,
                        "proactive_group_allowlist": [],
                        "proactive_scheduler_interval_seconds": 60,
                    },
                    "proactive_initiation_enabled": True,
                    "basic_settings": {"persona_name": "亚托莉"},
                    "max_context_messages": 23,
                },
            )
            self.assertFalse(plugin._config("proactive_initiation_enabled", True))
            self.assertEqual(
                plugin._config("proactive_scheduler_interval_seconds", 0),
                60,
            )
            self.assertEqual(plugin._config("max_context_messages", 0), 23)


if __name__ == "__main__":
    unittest.main()
