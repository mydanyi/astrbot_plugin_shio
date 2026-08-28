from __future__ import annotations

import json
import unittest

from astrbot_plugin_shio.core.memory_policy import (
    MemoryPolicy,
    MemoryScope,
    proactive_group_public_facts,
)
from astrbot_plugin_shio.core.plugin_adapters.livingmemory import (
    LivingMemoryAdapter,
)

from astrbot_plugin_shio.tests.test_memory_policy import (
    accepted_turn,
    provided_request,
)


class P8MemoryScopeTests(unittest.IsolatedAsyncioTestCase):
    async def _decide(self, *, chat_type: str, owner: bool, rows):
        admission, event = accepted_turn(chat_type=chat_type, owner=owner)
        result = await MemoryPolicy().decide(
            admission,
            event,
            adapter=LivingMemoryAdapter.verified(),
            provided_recall=provided_request(rows),
            include_recent=False,
            semantic_required=True,
            max_results=10,
        )
        return event, result

    async def test_three_scopes_are_exact_and_current_message_is_always_first(self):
        event, result = await self._decide(
            chat_type="private",
            owner=True,
            rows=(
                {
                    "id": "personal",
                    "sender_id": "owner-1",
                    "content": "当前主人普通个人偏好",
                    "scope": "personal",
                    "confidence": 0.95,
                    "score": 0.95,
                },
                {
                    "id": "owner-private",
                    "sender_id": "owner-1",
                    "content": "仅主人私聊可用的合成事实",
                    "scope": "owner_private",
                    "confidence": 0.95,
                    "score": 0.95,
                },
                {
                    "id": "public",
                    "content": "任何会话可用的公共资料",
                    "scope": "public",
                    "confidence": 0.95,
                    "score": 0.95,
                },
            ),
        )

        self.assertEqual(
            tuple(fact.scope for fact in result.personal_facts),
            (MemoryScope.PERSONAL.value,),
        )
        self.assertEqual(
            tuple(fact.scope for fact in result.owner_private_facts),
            (MemoryScope.OWNER_PRIVATE.value,),
        )
        self.assertEqual(
            tuple(fact.scope for fact in result.group_public_facts),
            (MemoryScope.PUBLIC.value,),
        )
        self.assertEqual(
            result.context_order,
            (
                "current_message",
                "memory_personal",
                "memory_owner_private",
                "memory_group_public",
            ),
        )
        self.assertTrue(result.current_message_precedence)
        self.assertEqual(result.decision.binding, event.binding)

    async def test_owner_private_requires_exact_owner_private_current_subject(self):
        cases = (
            ("private", False, "owner_private_nonowner"),
            ("group", True, "owner_private_group_forbidden"),
            ("group", False, "owner_private_nonowner"),
        )
        for chat_type, owner, reason in cases:
            with self.subTest(chat_type=chat_type, owner=owner):
                _event, result = await self._decide(
                    chat_type=chat_type,
                    owner=owner,
                    rows=(
                        {
                            "id": "owner-private",
                            "sender_id": "owner-1" if owner else "peer-1",
                            "content": "不可跨边界的主人私有合成事实",
                            "scope": "owner_private",
                            "confidence": 1.0,
                            "score": 1.0,
                        },
                    ),
                )
                self.assertEqual(result.owner_private_facts, ())
                self.assertEqual(result.decision.selected_facts, ())
                self.assertEqual(dict(result.exclusion_counts).get(reason), 1)

        _event, owner_private = await self._decide(
            chat_type="private",
            owner=True,
            rows=(
                {
                    "id": "foreign-owner-private",
                    "sender_id": "someone-else",
                    "content": "别人的私有事实",
                    "scope": "owner_private",
                    "confidence": 1.0,
                    "score": 1.0,
                },
            ),
        )
        self.assertEqual(owner_private.owner_private_facts, ())
        self.assertEqual(
            dict(owner_private.exclusion_counts).get("other_subject"),
            1,
        )

    async def test_personal_memory_is_current_subject_only_and_public_is_subjectless(self):
        event, result = await self._decide(
            chat_type="group",
            owner=False,
            rows=(
                {
                    "id": "mine",
                    "sender_id": "peer-1",
                    "content": "当前群友的合成偏好",
                    "scope": "personal",
                    "confidence": 0.95,
                    "score": 0.95,
                },
                {
                    "id": "other",
                    "sender_id": "peer-2",
                    "content": "另一群友的合成私密事实",
                    "scope": "personal",
                    "confidence": 1.0,
                    "score": 1.0,
                },
                {
                    "id": "group",
                    "content": "当前群的合成公共话题",
                    "scope": "group",
                    "confidence": 0.95,
                    "score": 0.95,
                },
            ),
        )

        self.assertEqual(
            tuple(fact.subject_key for fact in result.personal_facts),
            (event.principal.account_key,),
        )
        self.assertTrue(
            all(not fact.subject_key for fact in result.group_public_facts)
        )
        rendered = "\n".join(
            fact.content for fact in result.decision.selected_facts
        )
        self.assertNotIn("另一群友", rendered)
        self.assertEqual(dict(result.exclusion_counts).get("other_subject"), 1)

    async def test_proactive_projection_can_only_observe_group_public_memory(self):
        _event, result = await self._decide(
            chat_type="group",
            owner=True,
            rows=(
                {
                    "id": "personal",
                    "sender_id": "owner-1",
                    "content": "主人在群里的个人事实",
                    "scope": "personal",
                    "confidence": 0.95,
                    "score": 0.95,
                },
                {
                    "id": "owner-private",
                    "sender_id": "owner-1",
                    "content": "主人私有事实",
                    "scope": "owner_private",
                    "confidence": 0.95,
                    "score": 0.95,
                },
                {
                    "id": "group-public",
                    "session_id": "session-group",
                    "content": "群内公共话题",
                    "scope": "group",
                    "confidence": 0.95,
                    "score": 0.95,
                },
            ),
        )

        projected = proactive_group_public_facts(result)
        self.assertEqual(tuple(fact.content for fact in projected), ("群内公共话题",))
        self.assertTrue(all(not fact.subject_key for fact in projected))
        self.assertNotIn(
            "主人私有事实",
            json.dumps(result.trace_metadata(), ensure_ascii=False),
        )

        _event, private_result = await self._decide(
            chat_type="private",
            owner=True,
            rows=(
                {
                    "id": "public",
                    "content": "全局公共背景",
                    "scope": "public",
                    "confidence": 0.95,
                    "score": 0.95,
                },
            ),
        )
        self.assertEqual(proactive_group_public_facts(private_result), ())


if __name__ == "__main__":
    unittest.main()
