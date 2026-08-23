from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class P4PersonaReplacementHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp.name)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _composer_for(self, persona_name: str, *, now: float):
        plugin = main.ShioPlugin(
            FakeContext(FakeProvider([])),
            {"persona_name": persona_name},
        )
        event = FakeEvent(
            "peer-a",
            "为什么这个配置会失效？",
            group_id="group-persona-replacement",
        )
        event.message_id = "persona-replacement-current"
        event.is_at_or_wake_command = True
        request = FakeRequest(event.message)
        with patch("astrbot_plugin_shio.main.time.time", return_value=now):
            await plugin.enforce_agent_permission(event, request)
            await plugin.build_persona_reply(event, request)
        return event.get_extra(main.SHIO_REPLY_COMPOSER_REQUEST)

    async def test_three_personas_use_the_same_production_hot_path(self):
        atri = await self._composer_for("亚托莉", now=1_000.0)
        alternate = await self._composer_for("苏澄", now=1_001.0)
        neutral = await self._composer_for("中性基准", now=1_002.0)
        requests = (atri, alternate, neutral)

        self.assertEqual(
            tuple(request.package_id for request in requests),
            ("atri_default", "su_cheng_test", "neutral_minimal_test"),
        )
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
                    atri.content_seed.intent.kind,
                    atri.content_seed.intent.answer_language,
                    atri.content_seed.intent.required_atoms,
                    atri.content_seed.intent.forbidden_atoms,
                    atri.content_seed.intent.required_media_item_ids,
                    atri.content_seed.intent.grounding_facts,
                ),
            )
            self.assertEqual(request.planned_action.kind, atri.planned_action.kind)
            self.assertEqual(
                request.planned_action.action.reply_target,
                atri.planned_action.action.reply_target,
            )
            self.assertEqual(request.capability_policy, atri.capability_policy)
            self.assertEqual(request.system_prompt, atri.system_prompt)
            self.assertEqual(request.call_budget, atri.call_budget)
            self.assertEqual(
                request.user_prompt.split("[人格与表达]", 1)[0],
                atri.user_prompt.split("[人格与表达]", 1)[0],
            )

        persona_sections = tuple(
            request.user_prompt.split("[人格与表达]", 1)[1]
            for request in requests
        )
        self.assertEqual(len(set(persona_sections)), 3)
        self.assertNotIn("亚托莉", neutral.user_prompt)
        self.assertNotIn("苏澄", neutral.user_prompt)

    def test_hot_path_has_no_persona_specific_branch(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        for token in (
            "atri_default",
            "su_cheng_test",
            "neutral_minimal_test",
            "苏澄",
            "中性基准",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
