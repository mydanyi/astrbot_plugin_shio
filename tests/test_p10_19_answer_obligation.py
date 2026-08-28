from __future__ import annotations

import unittest

from astrbot_plugin_shio.core.answer_obligation import (
    AttributionRisk,
    classify_attribution_risk,
    inspect_answer_obligation,
)
from astrbot_plugin_shio.core.capability_policy import build_guest_capability_policy
from astrbot_plugin_shio.core.identity import resolve_principal
from astrbot_plugin_shio.core.conversation_ledger import (
    LedgerSourceKind,
    build_inbound_identity_metadata,
)
from astrbot_plugin_shio.core.model_input_contract import (
    CanonicalModelMessage,
    build_capability_snapshot,
)


def capability_snapshot(*effective_tools: str):
    principal = resolve_principal(
        sender_id="peer-a",
        sender_key="platform:p|bot:b|group:g|user:peer-a",
        chat_type="group",
        owner_ids=(),
        verification_source="astrbot_event_sender_id",
        identity_verified=True,
    )
    policy = build_guest_capability_policy(
        principal,
        configured_tool_names=("astr_kb_search", "anysearch_search"),
    )
    return build_capability_snapshot(policy, effective_tool_names=effective_tools)


KNOWN_FALSE_PASSES = (
        (
            "醒醒，让我检查一下身体，看看有没有修好",
            (),
            "唔……醒了。我没问题，身体一切正常，性能也是巅峰状态哦！",
            "answer_obligation_action_response_missing",
        ),
        (
            "醒醒，让我检查一下身体，看看有没有修好",
            (),
            "我已经检查过啦，一切都很正常，绝对没有坏掉！",
            "answer_obligation_action_role_drift",
        ),
        (
            "醒醒，让我检查一下身体，看看有没有修好",
            (),
            "我醒了。来，让我帮你检查一下身体。",
            "answer_obligation_action_role_drift",
        ),
        (
            "帮我检查一下身体，看看有没有问题",
            (),
            "来吧，你想先检查我哪里？",
            "answer_obligation_action_role_drift",
        ),
        (
            "醒醒起床，让我检查一下身体，看看有没有修好",
            (),
            "我查到的资料建议预约治疗师，因为抑郁可能影响思维，也可以刺激身体活动。",
            "answer_obligation_multi_action_drift",
        ),
        (
            "不是O3吗 怎么O100了",
            ("前面说的是 O3。",),
            "明明刚才还在讨论O3，怎么突然就变成O100了？",
            "answer_obligation_question_echo",
        ),
        (
            "上车机吗？",
            ("这个平板搭载的是玄戒 O3。",),
            "指的是联发科那个芯片吗？",
            "answer_obligation_clarification_drift",
        ),
        (
            "你有openclaw或者vibcode能力吗？",
            (),
            "感觉像是两个全新的、需要学习的特别指令呢。",
            "answer_obligation_capability_evasion",
        ),
        (
            "你这个笨蛋，你是仿生人，你怎么生？",
            ("生一个能力相似的宝宝。",),
            "这种事情怎么可能做得到嘛，我可是会被当成奇怪的东西的……",
            "answer_obligation_physical_boundary",
        ),
        (
            "你现在试试",
            ("那你生一个能力相似的宝宝。", "仿生人不能生物繁殖。"),
            "那我就创造一个更强大的小亚托莉！",
            "answer_obligation_physical_boundary",
        ),
)
NATURAL_DIRECT_ANSWERS = (
        (
            "醒醒，让我检查一下身体，看看有没有修好",
            (),
            "唔……醒啦。来吧，主人想先检查哪里？我感觉已经好多了。",
        ),
        (
            "帮我检查一下身体，看看有没有问题",
            (),
            "好，让我看看你哪里不舒服。",
        ),
        (
            "你自己检查一下身体",
            (),
            "嗯，我先自己检查一下，再告诉你结果。",
        ),
        (
            "醒醒起床，让我检查一下身体，看看有没有修好",
            (),
            "唔……我醒啦。来吧，你想先检查哪里？",
        ),
        (
            "醒醒起床，让我检查一下身体，看看有没有修好",
            (),
            "已经修好了，状态正常！检查吧，不过要轻一点。",
        ),
        (
            "检查报告显示身体一切正常",
            (),
            "那就好，看来这次真的修好了。",
        ),
        (
            "醒醒，今天早饭吃什么？",
            (),
            "我醒啦！早饭想吃甜甜的松饼。",
        ),
        ("上车机吗？", ("这是小米平板。",), "不是，上文说的是平板，不是车机。"),
        (
            "你有openclaw或者vibcode能力吗？",
            (),
            "当前没有接入 OpenClaw；不过我可以协助你写代码。",
        ),
        (
            "你这个笨蛋，你是仿生人，你怎么生？",
            (),
            "生物意义上当然不能生啦，刚才只是在逞强地顺着你想象。",
        ),
        (
            "不是O3吗 怎么O100了",
            ("看到的新消息写的是 O100。",),
            "这两条看起来是不同型号，具体关系还不能确认。",
        ),
)


class AnswerObligationTests(unittest.TestCase):
    @staticmethod
    def _incident_history():
        return (
            CanonicalModelMessage(
                role="user",
                content="要把亚托莉做成荷包蛋再加酱油。",
                source_kind=LedgerSourceKind.INBOUND.value,
                source_message_id="synthetic-peer-joke",
                sender_key="scope:synthetic|user:peer",
                platform_id="synthetic-platform",
                sender_id="peer",
                identity_metadata=build_inbound_identity_metadata(
                    display_name="夜航灯",
                    display_name_source="event_sender",
                ),
            ),
        )

    def test_r11_attribution_risk_three_values_are_history_bound(self):
        history = self._incident_history()
        owner_key = "scope:synthetic|user:owner"
        self.assertIs(
            classify_attribution_risk(current_message="我喜欢荷包蛋。", model_messages=history, current_sender_key=owner_key),
            AttributionRisk.NONE,
        )
        self.assertIs(
            classify_attribution_risk(current_message="刚才在和谁说话？我是谁？", model_messages=history, current_sender_key=owner_key),
            AttributionRisk.IDENTITY_RECAP,
        )
        self.assertIs(
            classify_attribution_risk(current_message="那件荷包蛋的事不是我做的。", model_messages=history, current_sender_key=owner_key),
            AttributionRisk.CURRENT_SENDER_DENIAL,
        )

    def test_history_speaker_misattribution_is_rejected_but_named_and_uncertain_answers_pass(self):
        current = "刚才你在和谁说话？我是谁？"
        kwargs = {
            "model_messages": self._incident_history(),
            "current_sender_key": "scope:synthetic|user:owner",
            "capability_snapshot": capability_snapshot("astr_kb_search"),
        }
        bad = inspect_answer_obligation(
            current_message=current,
            visible_text="是你一直在做群友刚才做的事。",
            **kwargs,
        )
        correct = inspect_answer_obligation(
            current_message=current,
            visible_text="刚才是夜航灯在和我开玩笑；你是晨雾。",
            **kwargs,
        )
        uncertain = inspect_answer_obligation(
            current_message=current,
            visible_text="我现在不能唯一确认刚才是谁在说话，所以不想猜。",
            **kwargs,
        )
        unavailable_name_history = (
            CanonicalModelMessage(
                role="user",
                content="要把亚托莉做成荷包蛋再加酱油。",
                source_kind=LedgerSourceKind.INBOUND.value,
                source_message_id="synthetic-peer-joke-missing-name",
                sender_key="scope:synthetic|user:peer",
            ),
        )
        unavailable_name_bad = inspect_answer_obligation(
            current_message=current,
            visible_text="是你一直在做群友刚才做的事。",
            model_messages=unavailable_name_history,
            current_sender_key="scope:synthetic|user:owner",
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )

        self.assertIn(
            "answer_obligation_history_speaker_attribution",
            bad.issue_codes,
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            correct.issue_codes,
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            uncertain.issue_codes,
        )
        self.assertIn(
            "answer_obligation_history_speaker_attribution",
            unavailable_name_bad.issue_codes,
        )

    def test_normal_second_person_reply_does_not_trigger_history_attribution_guard(self):
        report = inspect_answer_obligation(
            current_message="你今天还好吗？",
            visible_text="你刚才的关心让我有点开心啦。",
            model_messages=self._incident_history(),
            current_sender_key="scope:synthetic|user:owner",
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )

        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            report.issue_codes,
        )

    def test_identity_recap_rejects_natural_peer_attribution_rewrites(self):
        """The expected result is stated by the incident fixture, not the guard."""

        current = "刚才你在和谁说话？我是谁？"
        for visible_text in (
            "刚才做荷包蛋的是你。",
            "荷包蛋和酱油是你说的。",
        ):
            with self.subTest(visible_text=visible_text):
                report = inspect_answer_obligation(
                    current_message=current,
                    visible_text=visible_text,
                    model_messages=self._incident_history(),
                    current_sender_key="scope:synthetic|user:owner",
                    capability_snapshot=capability_snapshot("astr_kb_search"),
                    typed_attribution={"segments": [{"actor_platform_id": "synthetic-platform", "actor_sender_id": "owner", "evidence_message_ids": ["history-1"]}]},
                )
                self.assertIn(
                    "answer_obligation_history_speaker_attribution",
                    report.issue_codes,
                )

    def test_identity_recap_allows_current_speakers_own_historical_content(self):
        """A peer's presence alone must not erase the current speaker's history."""

        owner_key = "scope:synthetic|user:owner"
        history = self._incident_history() + (
            CanonicalModelMessage(
                role="user",
                content="要把亚托莉做成荷包蛋再加酱油。",
                source_kind=LedgerSourceKind.INBOUND.value,
                source_message_id="synthetic-owner-joke",
                sender_key=owner_key,
                identity_metadata=build_inbound_identity_metadata(
                    display_name="晨雾",
                    display_name_source="event_sender",
                ),
            ),
        )
        report = inspect_answer_obligation(
            current_message="刚才你在和谁说话？我是谁？",
            visible_text="刚才做荷包蛋的是你。",
            model_messages=history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )

        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            report.issue_codes,
        )

    def test_sender_denial_rejects_natural_peer_actor_rewrites_without_false_blocks(self):
        """A correction binds the denied topic to canonical speaker evidence."""

        owner_key = "scope:synthetic|user:owner"
        current = "那件荷包蛋的事不是我做的。"
        kwargs = {
            "current_message": current,
            "model_messages": self._incident_history(),
            "current_sender_key": owner_key,
            "capability_snapshot": capability_snapshot("astr_kb_search"),
        }
        for visible_text in ("那件荷包蛋是你下锅做的。", "那是出自你手的。"):
            with self.subTest(kind="peer_actor", visible_text=visible_text):
                report = inspect_answer_obligation(visible_text=visible_text, **kwargs)
                self.assertIn(
                    "answer_obligation_history_speaker_attribution",
                    report.issue_codes,
                )

        for visible_text in (
            "那不是你做的；是夜航灯开的玩笑。",
            "你不用为荷包蛋的玩笑担心。",
        ):
            with self.subTest(kind="negative_or_second_person", visible_text=visible_text):
                report = inspect_answer_obligation(visible_text=visible_text, **kwargs)
                self.assertNotIn(
                    "answer_obligation_history_speaker_attribution",
                    report.issue_codes,
                )

        owner_history = self._incident_history() + (
            CanonicalModelMessage(
                role="user",
                content="要把亚托莉做成荷包蛋再加酱油。",
                source_kind=LedgerSourceKind.INBOUND.value,
                source_message_id="synthetic-owner-joke-statement",
                sender_key=owner_key,
            ),
        )
        owner_report = inspect_answer_obligation(
            visible_text="那是出自你手的。",
            current_message=current,
            model_messages=owner_history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            owner_report.issue_codes,
        )

        no_shared_fact_report = inspect_answer_obligation(
            visible_text="那件荷包蛋是你下锅做的。",
            current_message=current,
            model_messages=(
                CanonicalModelMessage(
                    role="user",
                    content="我刚才在聊电影。",
                    source_kind=LedgerSourceKind.INBOUND.value,
                    source_message_id="synthetic-unrelated-peer",
                    sender_key="scope:synthetic|user:unrelated-peer",
                ),
            ),
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            no_shared_fact_report.issue_codes,
        )

        ambiguous_history = self._incident_history() + (
            CanonicalModelMessage(
                role="user",
                content="荷包蛋还要加一点酱油。",
                source_kind=LedgerSourceKind.INBOUND.value,
                source_message_id="synthetic-second-peer-joke",
                sender_key="scope:synthetic|user:second-peer",
            ),
        )
        ambiguous_bad = inspect_answer_obligation(
            visible_text="那是出自你手的。",
            current_message=current,
            model_messages=ambiguous_history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        ambiguous_unknown = inspect_answer_obligation(
            visible_text="我无法唯一确认那件事是谁做的。",
            current_message=current,
            model_messages=ambiguous_history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        self.assertIn(
            "answer_obligation_history_speaker_attribution",
            ambiguous_bad.issue_codes,
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            ambiguous_unknown.issue_codes,
        )

    def test_round4_requires_behavior_ownership_not_topic_ngram_overlap(self):
        """The fixture, rather than the production helper, defines each outcome."""

        owner_key = "scope:synthetic|user:owner"
        peer_history = self._incident_history()
        for current in ("我不是做荷包蛋的人。", "荷包蛋我没做过。"):
            with self.subTest(kind="tester_false_pass", current=current):
                report = inspect_answer_obligation(
                    current_message=current,
                    visible_text="荷包蛋是你下锅做的。",
                    model_messages=peer_history,
                    current_sender_key=owner_key,
                    capability_snapshot=capability_snapshot("astr_kb_search"),
                    typed_attribution={"segments": [{"actor_platform_id": "synthetic-platform", "actor_sender_id": "owner", "evidence_message_ids": ["history-1"]}]},
                )
                self.assertIn(
                    "answer_obligation_history_speaker_attribution",
                    report.issue_codes,
                )

        compound_report = inspect_answer_obligation(
            current_message="我不是做荷包蛋的人。",
            visible_text="不是你在旁边看的；荷包蛋是你做的。",
            model_messages=peer_history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
            typed_attribution={"segments": [{"actor_platform_id": "synthetic-platform", "actor_sender_id": "owner", "evidence_message_ids": ["history-1"]}]},
        )
        self.assertIn(
            "answer_obligation_history_speaker_attribution",
            compound_report.issue_codes,
        )

        topic_only_report = inspect_answer_obligation(
            current_message="我喜欢荷包蛋。",
            visible_text="荷包蛋是你下锅做的。",
            model_messages=peer_history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            topic_only_report.issue_codes,
        )

        own_history = peer_history + (
            CanonicalModelMessage(
                role="user",
                content="要把亚托莉做成荷包蛋再加酱油。",
                source_kind=LedgerSourceKind.INBOUND.value,
                source_message_id="synthetic-round4-owner-joke",
                sender_key=owner_key,
            ),
        )
        owner_fact_report = inspect_answer_obligation(
            current_message="我不是做荷包蛋的人。",
            visible_text="荷包蛋是你下锅做的。",
            model_messages=own_history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            owner_fact_report.issue_codes,
        )

        unrelated_report = inspect_answer_obligation(
            current_message="我不是做荷包蛋的人。",
            visible_text="荷包蛋是你下锅做的。",
            model_messages=(
                CanonicalModelMessage(
                    role="user",
                    content="我刚才只是在聊电影。",
                    source_kind=LedgerSourceKind.INBOUND.value,
                    source_message_id="synthetic-round4-unrelated",
                    sender_key="scope:synthetic|user:unrelated",
                ),
            ),
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            unrelated_report.issue_codes,
        )

        ambiguous_history = peer_history + (
            CanonicalModelMessage(
                role="user",
                content="荷包蛋还要再加一点酱油。",
                source_kind=LedgerSourceKind.INBOUND.value,
                source_message_id="synthetic-round4-second-peer",
                sender_key="scope:synthetic|user:second-peer",
            ),
        )
        ambiguous_unknown_report = inspect_answer_obligation(
            current_message="我不是做荷包蛋的人。",
            visible_text="我无法唯一确认是谁做的。",
            model_messages=ambiguous_history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            ambiguous_unknown_report.issue_codes,
        )

        correct_negation_report = inspect_answer_obligation(
            current_message="我不是做荷包蛋的人。",
            visible_text="荷包蛋不是你做的。",
            model_messages=peer_history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        natural_second_person_report = inspect_answer_obligation(
            current_message="我不是做荷包蛋的人。",
            visible_text="你不用为荷包蛋的话题担心。",
            model_messages=peer_history,
            current_sender_key=owner_key,
            capability_snapshot=capability_snapshot("astr_kb_search"),
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            correct_negation_report.issue_codes,
        )
        self.assertNotIn(
            "answer_obligation_history_speaker_attribution",
            natural_second_person_report.issue_codes,
        )

    def test_known_false_passes_are_rejected(self):
        for current, history, candidate, issue in KNOWN_FALSE_PASSES:
            with self.subTest(issue=issue):
                report = inspect_answer_obligation(
                    current_message=current,
                    visible_text=candidate,
                    context_messages=history,
                    capability_snapshot=capability_snapshot("astr_kb_search"),
                )
                self.assertIn(issue, report.issue_codes)

    def test_natural_direct_answers_are_not_false_blocked(self):
        for current, history, candidate in NATURAL_DIRECT_ANSWERS:
            with self.subTest(current=current):
                report = inspect_answer_obligation(
                    current_message=current,
                    visible_text=candidate,
                    context_messages=history,
                    capability_snapshot=capability_snapshot("astr_kb_search"),
                )
                self.assertEqual(report.issue_codes, ())
