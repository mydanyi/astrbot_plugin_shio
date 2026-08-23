import unittest
from pathlib import Path

from astrbot_plugin_shio.core.persona import (
    PersonaPackage,
    load_persona_package,
    validate_persona_package,
)


ROOT = Path(__file__).parents[1]
PERSONA_DIR = ROOT / "assets" / "personas"


class PersonaReplacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.atri = load_persona_package(PERSONA_DIR / "atri.json")
        cls.su_cheng = load_persona_package(PERSONA_DIR / "su_cheng.json")
        cls.neutral = load_persona_package(PERSONA_DIR / "neutral_minimal.json")

    def test_non_atri_package_uses_same_schema_and_loader(self):
        self.assertIsInstance(self.atri, PersonaPackage)
        self.assertIsInstance(self.su_cheng, PersonaPackage)
        self.assertTrue(validate_persona_package(self.su_cheng).is_valid)
        self.assertNotEqual(self.atri.package_id, self.su_cheng.package_id)
        self.assertIsInstance(self.neutral, PersonaPackage)
        self.assertTrue(validate_persona_package(self.neutral).is_valid)
        self.assertEqual(self.neutral.package_id, "neutral_minimal_test")

    def test_minimal_neutral_asset_has_no_character_specific_expression_bank(self):
        self.assertEqual(len(self.neutral.core_traits), 1)
        self.assertEqual(len(self.neutral.expression_materials), 0)
        self.assertEqual(len(self.neutral.language.catchphrases), 0)
        self.assertEqual(
            tuple(rule.trigger.value for rule in self.neutral.emotion_rules),
            ("neutral", "information_request"),
        )

    def test_non_atri_snapshot_is_intentionally_different(self):
        snapshot = {
            "package_id": self.su_cheng.package_id,
            "traits": tuple(trait.id for trait in self.su_cheng.core_traits),
            "triggers": tuple(
                rule.trigger.value for rule in self.su_cheng.emotion_rules
            ),
            "catchphrase_count": len(self.su_cheng.language.catchphrases),
            "material_ids": tuple(
                material.id for material in self.su_cheng.expression_materials
            ),
        }

        self.assertEqual(
            snapshot,
            {
                "package_id": "su_cheng_test",
                "traits": (
                    "patient_clarity",
                    "quiet_warmth",
                    "dry_humor",
                ),
                "triggers": (
                    "neutral",
                    "information_request",
                    "user_needs_care",
                    "disagreement",
                ),
                "catchphrase_count": 0,
                "material_ids": ("clear_answer", "quiet_support"),
            },
        )

    def test_loading_alternate_asset_does_not_require_pipeline_role_tokens(self):
        pipeline_files = (
            ROOT / "core" / "action_planner.py",
            ROOT / "core" / "content_intent_builder.py",
            ROOT / "core" / "reply_composer.py",
            ROOT / "core" / "identity.py",
            ROOT / "core" / "capability_policy.py",
            ROOT / "core" / "response_guard.py",
        )

        for path in pipeline_files:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("su_cheng_test", source)
                self.assertNotIn("苏澄", source)
                self.assertNotIn("neutral_minimal_test", source)
                self.assertNotIn("中性基准", source)

    def test_role_assets_do_not_leak_into_each_other(self):
        atri_text = (PERSONA_DIR / "atri.json").read_text(encoding="utf-8")
        alternate_text = (PERSONA_DIR / "su_cheng.json").read_text(encoding="utf-8")
        neutral_text = (PERSONA_DIR / "neutral_minimal.json").read_text(encoding="utf-8")

        self.assertNotIn("苏澄", atri_text)
        for token in ("亚托莉", "ATRI", "高性能机器人", "校准失误"):
            with self.subTest(token=token):
                self.assertNotIn(token, alternate_text)
                self.assertNotIn(token, neutral_text)


if __name__ == "__main__":
    unittest.main()
