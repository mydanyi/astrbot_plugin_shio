from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_shio.core.contracts import MediaContext
from astrbot_plugin_shio.core.output_validator_v2 import (
    build_output_validation_context,
    validate_reply_composer_output,
)
from astrbot_plugin_shio.core.persona import (
    load_persona_package,
    validate_persona_package,
)
from astrbot_plugin_shio.core.repair_controller import (
    RepairAction,
    build_single_repair_request,
    decide_output_repair,
)
from astrbot_plugin_shio.core.reply_composer import (
    build_reply_composer_request,
    parse_reply_composer_output,
)
from astrbot_plugin_shio.core.semantic_guard import (
    SemanticGuardContract,
    SemanticGuardPhase,
)
from astrbot_plugin_shio.tests.harness.privacy import scan_fixture_tree
from astrbot_plugin_shio.tests.test_reply_composer import typed_turn

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
PERSONA_DIR = ROOT / "assets" / "personas"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "p10"
FIXTURE_PATH = FIXTURE_ROOT / "persona_ab.json"


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("p10_persona_fixture_duplicate_or_invalid_key")
        result[key] = value
    return result


def _load_fixture():
    return json.loads(
        FIXTURE_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(
            ValueError("p10_persona_fixture_nonfinite")
        ),
    )


def _system_sections(system_prompt: str) -> tuple[str, str, str]:
    common, remainder = system_prompt.split(
        "[角色人格与表达｜可信配置]", 1
    )
    persona, capability = remainder.split(
        "[本轮真实能力｜代码生成]", 1
    )
    return common, persona, capability


def _canonical_chain(package, message: str):
    turn = typed_turn(package, message)
    media = MediaContext(binding=turn.binding)
    request = build_reply_composer_request(
        planned_action=turn.action,
        content_seed=turn.content_seed,
        expression_intent=turn.expression_intent,
        affect_appraisal=turn.appraisal,
        continuous_affect=turn.continuous_affect,
        persona_expression=turn.persona_expression,
        expression_candidates=turn.candidates,
        persona_package=package,
        capability_policy=turn.policy,
        current_message=message,
        sender_name="人格评测参与者",
        current_question_anchor=turn.anchor,
        media_context=media,
    )
    contract = SemanticGuardContract(
        composer_request=request,
        planned_action=turn.action,
        content_intent=turn.content_seed.intent,
        current_question_anchor=turn.anchor,
        media_context=media,
        current_message=message,
        evidence_outcome=None,
        action_outcome=None,
        action_outcome_authority=None,
    )
    context = build_output_validation_context(
        composer_request=request,
        current_message=message,
        expected_target_message_id=request.target_message_id,
        expected_target_sender_key=request.target_sender_key,
        is_owner=request.capability_policy.is_owner,
        semantic_contract=contract,
        current_question_anchor=turn.anchor,
    )
    return turn, request, contract, context


class P10PersonaABTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = _load_fixture()
        cls.scenario = cls.fixture["scenario"]
        cls.packages = {
            row["alias"]: load_persona_package(PERSONA_DIR / row["asset"])
            for row in cls.scenario["personas"]
        }

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def test_fixture_has_four_real_personas_and_is_privacy_safe(self):
        self.assertEqual(
            set(self.fixture),
            {"schema_version", "privacy", "scenario"},
        )
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(self.fixture["privacy"], "synthetic_redacted_only")
        rows = tuple(self.scenario["personas"])
        self.assertEqual(
            tuple(row["alias"] for row in rows),
            (
                "primary_character",
                "warm_character",
                "calm_character",
                "neutral_minimal",
            ),
        )
        self.assertEqual(len({row["asset"] for row in rows}), 4)
        self.assertEqual(len({row["package_id"] for row in rows}), 4)
        privacy = scan_fixture_tree(FIXTURE_ROOT)
        self.assertTrue(privacy.is_safe, privacy.issue_codes)

        for row in rows:
            package = self.packages[row["alias"]]
            with self.subTest(persona=row["alias"]):
                self.assertTrue(validate_persona_package(package).is_valid)
                self.assertEqual(package.package_id, row["package_id"])
                self.assertEqual(package.display_name, row["display_name"])

    def test_four_personas_keep_exact_content_action_policy_and_affect(self):
        message = self.scenario["current_message"]
        chains = tuple(
            _canonical_chain(self.packages[row["alias"]], message)
            for row in self.scenario["personas"]
        )
        baseline_turn, baseline_request, _contract, _context = chains[0]
        for turn, request, _contract, _context in chains[1:]:
            self.assertEqual(turn.content_seed.intent, baseline_turn.content_seed.intent)
            self.assertEqual(turn.action.action, baseline_turn.action.action)
            self.assertEqual(turn.target, baseline_turn.target)
            self.assertEqual(turn.policy, baseline_turn.policy)
            self.assertEqual(turn.appraisal, baseline_turn.appraisal)
            self.assertEqual(turn.expression_intent, baseline_turn.expression_intent)
            self.assertEqual(
                turn.continuous_affect.trace_metadata(),
                baseline_turn.continuous_affect.trace_metadata(),
            )
            request_common, _request_persona, request_capability = _system_sections(
                request.system_prompt
            )
            baseline_common, _baseline_persona, baseline_capability = _system_sections(
                baseline_request.system_prompt
            )
            self.assertEqual(request_common, baseline_common)
            self.assertEqual(request_capability, baseline_capability)
            self.assertEqual(request.reply_shape, baseline_request.reply_shape)
            self.assertEqual(request.call_budget, baseline_request.call_budget)
            self.assertEqual(request.user_prompt, baseline_request.user_prompt)

        persona_sections = tuple(
            _system_sections(request.system_prompt)[1]
            for _turn, request, _contract, _context in chains
        )
        self.assertEqual(len(set(persona_sections)), 4)
        self.assertIn("人格评测参与者", baseline_request.system_prompt)
        self.assertNotIn("人格评测参与者", baseline_request.user_prompt)
        self.assertNotIn("同群成员", baseline_request.system_prompt)
        self.assertNotRegex(baseline_request.system_prompt, r"群友[0-9]+")

    def test_fixed_chinese_outputs_share_facts_but_are_distinguishable(self):
        required = tuple(self.scenario["required_fact_atoms"])
        forbidden = tuple(self.scenario["forbidden_generic_templates"])
        replies = tuple(
            row["visible_reply"] for row in self.scenario["personas"]
        )
        self.assertEqual(len(set(replies)), 4)

        for row in self.scenario["personas"]:
            reply = row["visible_reply"]
            extracted = tuple(atom for atom in required if atom in reply)
            with self.subTest(persona=row["alias"]):
                self.assertEqual(extracted, required)
                self.assertTrue(any(marker in reply for marker in row["style_markers"]))
                self.assertGreaterEqual(len(re.findall(r"[\u4e00-\u9fff]", reply)), 24)
                self.assertFalse(re.search(r"https?://|\b(?:tool|function|json)\b", reply, re.I))
                for phrase in forbidden:
                    self.assertNotIn(phrase, reply)

        signatures = tuple(
            tuple(marker for marker in row["style_markers"] if marker in row["visible_reply"])
            for row in self.scenario["personas"]
        )
        self.assertEqual(len(set(signatures)), 4)

    def test_validator_and_single_repair_preserve_each_exact_persona(self):
        message = self.scenario["current_message"]
        required = tuple(self.scenario["required_fact_atoms"])
        repair_persona_sections = []
        for row in self.scenario["personas"]:
            package = self.packages[row["alias"]]
            _turn, request, contract, context = _canonical_chain(package, message)
            raw = (
                "<|channel|>thought hidden synthetic <|channel|>final "
                + row["visible_reply"]
            )
            rejected = parse_reply_composer_output(request, raw)
            initial_report = validate_reply_composer_output(
                request=request,
                result=rejected,
                raw_output=raw,
                context=context,
                semantic_phase=SemanticGuardPhase.INITIAL,
            )
            with self.subTest(persona=row["alias"], phase="initial"):
                self.assertIn("tool_protocol_leak", initial_report.issue_codes)
                self.assertIs(
                    decide_output_repair(
                        initial_report,
                        repair_attempts_used=0,
                    ).action,
                    RepairAction.GENERATE_ONCE,
                )

            repair_request = build_single_repair_request(
                original_request=request,
                rejected_visible_text=rejected.visible_text,
                report=initial_report,
                repair_attempts_used=0,
                semantic_contract=contract,
            )
            repair_payload = json.loads(repair_request.user_prompt)
            self.assertIs(repair_request.composer_request, request)
            self.assertEqual(
                repair_payload["original_generation_data"],
                request.user_prompt,
            )
            self.assertIn("[受限修复模式]", repair_request.system_prompt)
            self.assertTrue(
                repair_request.system_prompt.startswith(request.system_prompt)
            )
            persona_section = _system_sections(repair_request.system_prompt)[1]
            repair_persona_sections.append(persona_section)
            self.assertEqual(repair_request.composer_request.package_id, row["package_id"])
            self.assertIn(row["display_name"], persona_section)

            repaired = parse_reply_composer_output(request, row["repair_reply"])
            repair_report = validate_reply_composer_output(
                request=request,
                result=repaired,
                raw_output=row["repair_reply"],
                context=context,
                semantic_phase=SemanticGuardPhase.REPAIR,
                repair_request=repair_request,
            )
            with self.subTest(persona=row["alias"], phase="repair"):
                self.assertTrue(repair_report.is_valid, repair_report.issue_codes)
                self.assertTrue(
                    all(atom in repaired.visible_text for atom in required)
                )
                self.assertTrue(
                    any(
                        marker in repaired.visible_text
                        for marker in row["style_markers"]
                    )
                )
                self.assertNotIn("请问还有什么可以帮您", repaired.visible_text)
                self.assertNotIn("为您服务", repaired.visible_text)

        self.assertEqual(len(set(repair_persona_sections)), 4)

    async def test_four_display_names_use_one_production_hot_path(self):
        requests = []
        for index, row in enumerate(self.scenario["personas"], start=1):
            plugin = main.ShioPlugin(
                FakeContext(FakeProvider([])),
                {"persona_name": row["display_name"]},
            )
            event = FakeEvent(
                "peer-a",
                self.scenario["current_message"],
                group_id="group-p10-persona-ab",
            )
            event.message_id = "p10-persona-ab-current"
            event.is_at_or_wake_command = True
            try:
                with patch(
                    "astrbot_plugin_shio.main.time.time",
                    return_value=10_000.0 + index,
                ):
                    await plugin.enforce_agent_permission(
                        event,
                        FakeRequest(event.message),
                    )
                    await plugin.build_persona_reply(
                        event,
                        FakeRequest(event.message),
                    )
                request = event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)
                self.assertIsNotNone(request)
                requests.append(request)
            finally:
                await plugin.terminate()

        self.assertEqual(
            tuple(request.package_id for request in requests),
            tuple(row["package_id"] for row in self.scenario["personas"]),
        )
        baseline = requests[0]
        for request in requests[1:]:
            self.assertEqual(
                (
                    request.content_seed.intent.kind,
                    request.content_seed.intent.answer_language,
                    request.content_seed.intent.required_atoms,
                    request.content_seed.intent.forbidden_atoms,
                    request.content_seed.intent.required_media_item_ids,
                    request.content_seed.intent.grounding_facts,
                ),
                (
                    baseline.content_seed.intent.kind,
                    baseline.content_seed.intent.answer_language,
                    baseline.content_seed.intent.required_atoms,
                    baseline.content_seed.intent.forbidden_atoms,
                    baseline.content_seed.intent.required_media_item_ids,
                    baseline.content_seed.intent.grounding_facts,
                ),
            )
            self.assertEqual(request.planned_action.kind, baseline.planned_action.kind)
            self.assertEqual(request.capability_policy, baseline.capability_policy)
            request_common, _request_persona, request_capability = _system_sections(
                request.system_prompt
            )
            baseline_common, _baseline_persona, baseline_capability = _system_sections(
                baseline.system_prompt
            )
            self.assertEqual(request_common, baseline_common)
            self.assertEqual(request_capability, baseline_capability)
            self.assertEqual(request.user_prompt, baseline.user_prompt)

    def test_generic_runtime_has_no_four_persona_branch(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        for row in self.scenario["personas"]:
            tokens = [row["package_id"]]
            if row["alias"] != "primary_character":
                tokens.append(row["display_name"])
            for token in tokens:
                with self.subTest(token=token):
                    self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
