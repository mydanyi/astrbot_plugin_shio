import unittest
from pathlib import Path

from astrbot_plugin_shio.core.affect import AffectTrigger, RelationshipDistance
from astrbot_plugin_shio.core.persona import (
    PRIMARY_BOND_EXCLUSIVE_ACTIONS,
    load_persona_package,
    validate_persona_package,
)


ASSET_PATH = Path(__file__).parents[1] / "assets" / "personas" / "atri.json"


class AtriPersonaPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = load_persona_package(ASSET_PATH)

    def test_migrated_package_is_valid_and_replaceable(self):
        report = validate_persona_package(self.package)

        self.assertTrue(report.is_valid, report.issues)
        self.assertEqual(self.package.package_id, "atri_default")
        self.assertEqual(self.package.language.primary_locale, "zh-CN")
        self.assertTrue(self.package.language.match_user_language_when_requested)

    def test_snapshot_of_role_structure(self):
        snapshot = {
            "traits": tuple(trait.id for trait in self.package.core_traits),
            "relations": tuple(
                rule.distance.value for rule in self.package.relationship_rules
            ),
            "emotion_triggers": tuple(
                rule.trigger.value for rule in self.package.emotion_rules
            ),
            "materials": tuple(
                material.id for material in self.package.expression_materials
            ),
            "sources": tuple(source.id for source in self.package.sources),
        }

        self.assertEqual(
            snapshot,
            {
                "traits": (
                    "earnest_commitment",
                    "high_performance_pride",
                    "lively_childlike_curiosity",
                    "emotionally_sincere",
                    "care_through_action",
                    "soft_stubbornness",
                ),
                "relations": (
                    "primary_bond",
                    "peer",
                    "unverified",
                ),
                "emotion_triggers": tuple(trigger.value for trigger in AffectTrigger),
                "materials": (
                    "praise_softening",
                    "mistake_repair",
                    "seen_through_arc",
                    "care_first",
                    "fact_answer_voice",
                    "peer_playful_boundary",
                    "lively_surprise",
                    "lightly_teased_dignity",
                    "playful_retry",
                    "primary_bond_intimacy",
                ),
                "sources": (
                    "core_prompt_v10",
                    "atri_lore_canon",
                    "atri_lore_continuation",
                    "voice_card",
                    "expression_asset",
                ),
            },
        )

    def test_exclusive_relationship_expression_only_exists_in_primary_bond(self):
        for rule in self.package.relationship_rules:
            exclusive = set(rule.allowed_action_ids).intersection(
                PRIMARY_BOND_EXCLUSIVE_ACTIONS
            )
            if rule.distance == RelationshipDistance.PRIMARY_BOND:
                self.assertTrue(exclusive)
            else:
                self.assertFalse(exclusive, (rule.distance, exclusive))

    def test_catchphrases_are_situational_and_low_frequency(self):
        for rule in self.package.language.catchphrases:
            with self.subTest(text=rule.text):
                self.assertTrue(rule.trigger_ids)
                self.assertLessEqual(rule.max_uses_in_recent_replies, 1)

    def test_every_fact_has_matching_named_provenance(self):
        sources = {source.id: source.source_kind for source in self.package.sources}

        for fact in self.package.character_facts:
            with self.subTest(fact=fact.id):
                self.assertIn(fact.source_ref, sources)
                self.assertEqual(fact.source_kind, sources[fact.source_ref])

    def test_persona_asset_contains_no_runtime_capability_declaration(self):
        text = ASSET_PATH.read_text(encoding="utf-8").lower()

        for token in (
            "allowed_tools",
            "sender_id",
            "owner_ids",
            "shell_exec",
            "artifact_write",
            "device_control",
            "<verified_access_control",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
