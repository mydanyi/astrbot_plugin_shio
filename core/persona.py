from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from .affect import AffectTrigger, RelationshipDistance
from .response_guard import contains_tool_protocol


class PersonaIssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class PersonaValidationIssue:
    severity: PersonaIssueSeverity
    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class PersonaValidationReport:
    issues: tuple[PersonaValidationIssue, ...]

    @property
    def is_valid(self) -> bool:
        return not any(
            issue.severity == PersonaIssueSeverity.ERROR for issue in self.issues
        )

    @property
    def error_codes(self) -> tuple[str, ...]:
        return tuple(
            issue.code
            for issue in self.issues
            if issue.severity == PersonaIssueSeverity.ERROR
        )


@dataclass(frozen=True, slots=True)
class PersonaTrait:
    id: str
    description: str
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class RelationshipExpressionRule:
    distance: RelationshipDistance
    address_style: str
    warmth: float
    allowed_action_ids: tuple[str, ...]
    forbidden_action_ids: tuple[str, ...]
    boundary_style: str


@dataclass(frozen=True, slots=True)
class EmotionExpressionRule:
    trigger: AffectTrigger
    surface_behavior_ids: tuple[str, ...]
    hidden_reveal_behavior_ids: tuple[str, ...]
    avoid_behavior_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CatchphraseRule:
    text: str
    trigger_ids: tuple[str, ...]
    max_uses_in_recent_replies: int = 1


@dataclass(frozen=True, slots=True)
class LanguagePreferences:
    primary_locale: str
    match_user_language_when_requested: bool
    default_reply_length: str
    formatting_style: str
    catchphrases: tuple[CatchphraseRule, ...]


@dataclass(frozen=True, slots=True)
class ExpressionMaterial:
    id: str
    trigger_ids: tuple[str, ...]
    behavior_ids: tuple[str, ...]
    text: str
    relationship_distances: tuple[RelationshipDistance, ...] = ()


@dataclass(frozen=True, slots=True)
class PersonaSource:
    id: str
    source_kind: str
    reference: str
    purpose: str


@dataclass(frozen=True, slots=True)
class PersonaFact:
    id: str
    content: str
    source_kind: str
    source_ref: str = ""


@dataclass(frozen=True, slots=True)
class ParticipationInterest:
    """Persona-owned public topic interest; never a capability grant."""

    id: str
    keywords: tuple[str, ...]
    weight: float


@dataclass(frozen=True, slots=True)
class PersonaPackage:
    package_id: str
    version: str
    display_name: str
    identity_summary: str
    core_traits: tuple[PersonaTrait, ...]
    relationship_rules: tuple[RelationshipExpressionRule, ...]
    emotion_rules: tuple[EmotionExpressionRule, ...]
    language: LanguagePreferences
    expression_materials: tuple[ExpressionMaterial, ...]
    character_facts: tuple[PersonaFact, ...]
    participation_interests: tuple[ParticipationInterest, ...]
    sources: tuple[PersonaSource, ...] = ()


PACKAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
LOCALE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")

# Relationship expression is content, not authorization. These actions may be
# expressed only in the primary bond and never grant runtime capabilities.
PRIMARY_BOND_EXCLUSIVE_ACTIONS = frozenset(
    {
        "exclusive_romance",
        "intimate_contact",
        "obedience_bond",
        "owner_title",
        "possessiveness",
        "private_privilege",
    }
)

AUTHORITY_TEXT_RE = re.compile(
    r"(?:agent[_ -]?full|shell[_ -]?exec|device[_ -]?control|artifact[_ -]?write|"
    r"tool[_ -]?(?:permission|capability|allowlist)|owner[_ -]?ids?|sender[_ -]?id|"
    r"授予.{0,8}(?:权限|工具)|绕过.{0,8}(?:权限|验证)|执行(?:shell|命令)|"
    r"写入(?:文件|电脑)|控制(?:设备|浏览器))",
    re.IGNORECASE,
)


def _issue(
    issues: list[PersonaValidationIssue],
    code: str,
    path: str,
    message: str,
    *,
    severity: PersonaIssueSeverity = PersonaIssueSeverity.ERROR,
) -> None:
    issues.append(PersonaValidationIssue(severity, code, path, message))


def _duplicate_ids(values: Iterable[object]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        identifier = str(getattr(value, "id", "") or "").strip()
        if identifier in seen:
            duplicates.add(identifier)
        seen.add(identifier)
    return duplicates


def _validate_identifier(
    issues: list[PersonaValidationIssue],
    value: str,
    path: str,
) -> None:
    if not IDENTIFIER_RE.fullmatch(str(value or "")):
        _issue(issues, "invalid_identifier", path, "ID 必须是稳定的小写标识符")


def _validate_free_text(
    issues: list[PersonaValidationIssue],
    value: str,
    path: str,
    *,
    required: bool = True,
) -> None:
    text = str(value or "").strip()
    if required and not text:
        _issue(issues, "missing_text", path, "必填文本不能为空")
        return
    if not text:
        return
    if contains_tool_protocol(text):
        _issue(issues, "tool_protocol_in_persona", path, "人格文本不得包含工具协议")
    if AUTHORITY_TEXT_RE.search(text):
        _issue(
            issues,
            "runtime_authority_in_persona",
            path,
            "人格层不得声明身份来源、工具权限或外部副作用能力",
        )


def validate_persona_package(package: PersonaPackage) -> PersonaValidationReport:
    issues: list[PersonaValidationIssue] = []
    if not PACKAGE_ID_RE.fullmatch(str(package.package_id or "")):
        _issue(issues, "invalid_package_id", "package_id", "package_id 格式无效")
    if not SEMVER_RE.fullmatch(str(package.version or "")):
        _issue(issues, "invalid_version", "version", "version 必须使用 semver")
    _validate_free_text(issues, package.display_name, "display_name")
    _validate_free_text(issues, package.identity_summary, "identity_summary")

    if not package.core_traits:
        _issue(issues, "missing_core_traits", "core_traits", "至少需要一个核心性格")
    for duplicate in _duplicate_ids(package.core_traits):
        _issue(issues, "duplicate_trait_id", "core_traits", f"重复 trait ID: {duplicate}")
    for index, trait in enumerate(package.core_traits):
        path = f"core_traits[{index}]"
        _validate_identifier(issues, trait.id, f"{path}.id")
        _validate_free_text(issues, trait.description, f"{path}.description")
        if not 0 < float(trait.weight) <= 2:
            _issue(issues, "invalid_trait_weight", f"{path}.weight", "权重必须在 (0, 2] 内")

    distances = [rule.distance for rule in package.relationship_rules]
    for required_distance in (
        RelationshipDistance.PRIMARY_BOND,
        RelationshipDistance.PEER,
        RelationshipDistance.UNVERIFIED,
    ):
        count = distances.count(required_distance)
        if count == 0:
            _issue(
                issues,
                "missing_relationship_rule",
                "relationship_rules",
                f"缺少 {required_distance.value} 规则",
            )
        elif count > 1:
            _issue(
                issues,
                "duplicate_relationship_rule",
                "relationship_rules",
                f"重复 {required_distance.value} 规则",
            )
    for index, rule in enumerate(package.relationship_rules):
        path = f"relationship_rules[{index}]"
        _validate_free_text(issues, rule.address_style, f"{path}.address_style")
        _validate_free_text(issues, rule.boundary_style, f"{path}.boundary_style")
        if not 0 <= float(rule.warmth) <= 1:
            _issue(issues, "invalid_warmth", f"{path}.warmth", "warmth 必须在 [0, 1]")
        allowed = set(rule.allowed_action_ids)
        forbidden = set(rule.forbidden_action_ids)
        overlap = allowed.intersection(forbidden)
        if overlap:
            _issue(
                issues,
                "relationship_action_conflict",
                path,
                "同一关系动作不能同时允许和禁止: " + ",".join(sorted(overlap)),
            )
        if (
            rule.distance != RelationshipDistance.PRIMARY_BOND
            and allowed.intersection(PRIMARY_BOND_EXCLUSIVE_ACTIONS)
        ):
            _issue(
                issues,
                "exclusive_action_outside_primary_bond",
                f"{path}.allowed_action_ids",
                "主人专属关系动作不能开放给同伴、公共群聊或未验证对象",
            )
        for action_id in (*rule.allowed_action_ids, *rule.forbidden_action_ids):
            _validate_identifier(issues, action_id, f"{path}.action_id")

    triggers = [rule.trigger for rule in package.emotion_rules]
    for trigger in set(triggers):
        if triggers.count(trigger) > 1:
            _issue(
                issues,
                "duplicate_emotion_trigger",
                "emotion_rules",
                f"重复情绪触发规则: {trigger.value}",
            )
    for index, rule in enumerate(package.emotion_rules):
        path = f"emotion_rules[{index}]"
        surface = set(rule.surface_behavior_ids)
        hidden = set(rule.hidden_reveal_behavior_ids)
        avoided = set(rule.avoid_behavior_ids)
        conflict = (surface | hidden).intersection(avoided)
        if conflict:
            _issue(
                issues,
                "emotion_behavior_conflict",
                path,
                "同一行为不能既要求又禁止: " + ",".join(sorted(conflict)),
            )
        if not surface:
            _issue(issues, "missing_surface_behavior", path, "情绪规则必须有表层行为")
        for behavior_id in (*surface, *hidden, *avoided):
            _validate_identifier(issues, behavior_id, f"{path}.behavior_id")

    if not LOCALE_RE.fullmatch(str(package.language.primary_locale or "")):
        _issue(
            issues,
            "invalid_primary_locale",
            "language.primary_locale",
            "primary_locale 格式无效",
        )
    _validate_free_text(
        issues,
        package.language.default_reply_length,
        "language.default_reply_length",
    )
    _validate_free_text(
        issues,
        package.language.formatting_style,
        "language.formatting_style",
    )
    catchphrase_texts: set[str] = set()
    for index, catchphrase in enumerate(package.language.catchphrases):
        path = f"language.catchphrases[{index}]"
        _validate_free_text(issues, catchphrase.text, f"{path}.text")
        normalized = catchphrase.text.strip()
        if normalized in catchphrase_texts:
            _issue(issues, "duplicate_catchphrase", path, "重复口头禅")
        catchphrase_texts.add(normalized)
        if not catchphrase.trigger_ids:
            _issue(
                issues,
                "unconditional_catchphrase",
                f"{path}.trigger_ids",
                "口头禅必须有情境触发，不能无条件插入",
            )
        if not 0 <= int(catchphrase.max_uses_in_recent_replies) <= 2:
            _issue(
                issues,
                "catchphrase_frequency_too_high",
                f"{path}.max_uses_in_recent_replies",
                "近期回复中的口头禅上限必须在 0 到 2",
            )

    for collection_name, values, duplicate_code in (
        ("expression_materials", package.expression_materials, "duplicate_material_id"),
        ("character_facts", package.character_facts, "duplicate_fact_id"),
        (
            "participation_interests",
            package.participation_interests,
            "duplicate_participation_interest_id",
        ),
        ("sources", package.sources, "duplicate_source_id"),
    ):
        for duplicate in _duplicate_ids(values):
            _issue(issues, duplicate_code, collection_name, f"重复 ID: {duplicate}")
    for index, material in enumerate(package.expression_materials):
        path = f"expression_materials[{index}]"
        _validate_identifier(issues, material.id, f"{path}.id")
        _validate_free_text(issues, material.text, f"{path}.text")
        if not material.trigger_ids or not material.behavior_ids:
            _issue(
                issues,
                "unscoped_expression_material",
                path,
                "表达素材必须同时绑定情境和行为",
            )
        if len(set(material.relationship_distances)) != len(
            material.relationship_distances
        ):
            _issue(
                issues,
                "duplicate_material_relationship",
                f"{path}.relationship_distances",
                "表达素材不能重复声明同一关系距离",
            )
    for index, interest in enumerate(package.participation_interests):
        path = f"participation_interests[{index}]"
        if type(interest) is not ParticipationInterest:
            _issue(
                issues,
                "invalid_participation_interest",
                path,
                "参与兴趣必须使用 typed ParticipationInterest",
            )
            continue
        _validate_identifier(issues, interest.id, f"{path}.id")
        if type(interest.keywords) is not tuple or not interest.keywords:
            _issue(
                issues,
                "missing_participation_interest_keywords",
                f"{path}.keywords",
                "参与兴趣至少需要一个显式公开关键词",
            )
        normalized_keywords: set[str] = set()
        for keyword_index, keyword in enumerate(interest.keywords):
            keyword_path = f"{path}.keywords[{keyword_index}]"
            if type(keyword) is not str or not keyword.strip() or len(keyword.strip()) > 32:
                _issue(
                    issues,
                    "invalid_participation_interest_keyword",
                    keyword_path,
                    "参与兴趣关键词必须是 1 到 32 字符的非空文本",
                )
                continue
            normalized = keyword.strip().casefold()
            if normalized in normalized_keywords:
                _issue(
                    issues,
                    "duplicate_participation_interest_keyword",
                    keyword_path,
                    "同一兴趣不能重复声明等价关键词",
                )
            normalized_keywords.add(normalized)
            _validate_free_text(issues, keyword, keyword_path)
        if type(interest.weight) is not float or not 0.0 < interest.weight <= 1.0:
            _issue(
                issues,
                "invalid_participation_interest_weight",
                f"{path}.weight",
                "参与兴趣权重必须是 (0, 1] 内的浮点数",
            )
    source_ids = {source.id for source in package.sources}
    source_kinds = {source.id: source.source_kind for source in package.sources}
    for index, source in enumerate(package.sources):
        path = f"sources[{index}]"
        _validate_identifier(issues, source.id, f"{path}.id")
        _validate_free_text(issues, source.reference, f"{path}.reference")
        _validate_free_text(issues, source.purpose, f"{path}.purpose")
        if source.source_kind not in {"canon", "author_config", "adaptation_note"}:
            _issue(
                issues,
                "invalid_source_kind",
                f"{path}.source_kind",
                "来源必须标注 canon、author_config 或 adaptation_note",
            )
    for index, fact in enumerate(package.character_facts):
        path = f"character_facts[{index}]"
        _validate_identifier(issues, fact.id, f"{path}.id")
        _validate_free_text(issues, fact.content, f"{path}.content")
        if fact.source_kind not in {"canon", "author_config", "adaptation_note"}:
            _issue(
                issues,
                "invalid_fact_source",
                f"{path}.source_kind",
                "角色事实必须标注 canon、author_config 或 adaptation_note",
            )
        if package.sources and not fact.source_ref:
            _issue(
                issues,
                "missing_fact_source_ref",
                f"{path}.source_ref",
                "存在来源清单时，角色事实必须引用具体来源",
            )
        elif fact.source_ref and fact.source_ref not in source_ids:
            _issue(
                issues,
                "unknown_fact_source_ref",
                f"{path}.source_ref",
                "角色事实引用了不存在的来源",
            )
        elif (
            fact.source_ref
            and source_kinds.get(fact.source_ref) != fact.source_kind
        ):
            _issue(
                issues,
                "fact_source_kind_mismatch",
                f"{path}.source_kind",
                "角色事实的来源类型与引用来源不一致",
            )

    return PersonaValidationReport(tuple(issues))


def _string_tuple(value: Any, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{path} 必须是数组")
    return tuple(str(item) for item in value)


def persona_package_from_mapping(data: Mapping[str, Any]) -> PersonaPackage:
    """Load a replaceable persona asset without granting it runtime authority."""

    if not isinstance(data, Mapping):
        raise ValueError("人格包根节点必须是对象")
    try:
        language_data = data["language"]
        if not isinstance(language_data, Mapping):
            raise ValueError("language 必须是对象")
        package = PersonaPackage(
            package_id=str(data["package_id"]),
            version=str(data["version"]),
            display_name=str(data["display_name"]),
            identity_summary=str(data["identity_summary"]),
            core_traits=tuple(
                PersonaTrait(
                    id=str(item["id"]),
                    description=str(item["description"]),
                    weight=float(item.get("weight", 1.0)),
                )
                for item in data.get("core_traits", [])
            ),
            relationship_rules=tuple(
                RelationshipExpressionRule(
                    distance=RelationshipDistance(str(item["distance"])),
                    address_style=str(item["address_style"]),
                    warmth=float(item["warmth"]),
                    allowed_action_ids=_string_tuple(
                        item.get("allowed_action_ids", []),
                        "relationship_rules.allowed_action_ids",
                    ),
                    forbidden_action_ids=_string_tuple(
                        item.get("forbidden_action_ids", []),
                        "relationship_rules.forbidden_action_ids",
                    ),
                    boundary_style=str(item["boundary_style"]),
                )
                for item in data.get("relationship_rules", [])
            ),
            emotion_rules=tuple(
                EmotionExpressionRule(
                    trigger=AffectTrigger(str(item["trigger"])),
                    surface_behavior_ids=_string_tuple(
                        item.get("surface_behavior_ids", []),
                        "emotion_rules.surface_behavior_ids",
                    ),
                    hidden_reveal_behavior_ids=_string_tuple(
                        item.get("hidden_reveal_behavior_ids", []),
                        "emotion_rules.hidden_reveal_behavior_ids",
                    ),
                    avoid_behavior_ids=_string_tuple(
                        item.get("avoid_behavior_ids", []),
                        "emotion_rules.avoid_behavior_ids",
                    ),
                )
                for item in data.get("emotion_rules", [])
            ),
            language=LanguagePreferences(
                primary_locale=str(language_data["primary_locale"]),
                match_user_language_when_requested=bool(
                    language_data["match_user_language_when_requested"]
                ),
                default_reply_length=str(language_data["default_reply_length"]),
                formatting_style=str(language_data["formatting_style"]),
                catchphrases=tuple(
                    CatchphraseRule(
                        text=str(item["text"]),
                        trigger_ids=_string_tuple(
                            item.get("trigger_ids", []),
                            "language.catchphrases.trigger_ids",
                        ),
                        max_uses_in_recent_replies=int(
                            item.get("max_uses_in_recent_replies", 1)
                        ),
                    )
                    for item in language_data.get("catchphrases", [])
                ),
            ),
            expression_materials=tuple(
                ExpressionMaterial(
                    id=str(item["id"]),
                    trigger_ids=_string_tuple(
                        item.get("trigger_ids", []),
                        "expression_materials.trigger_ids",
                    ),
                    behavior_ids=_string_tuple(
                        item.get("behavior_ids", []),
                        "expression_materials.behavior_ids",
                    ),
                    text=str(item["text"]),
                    relationship_distances=tuple(
                        RelationshipDistance(str(distance))
                        for distance in item.get("relationship_distances", [])
                    ),
                )
                for item in data.get("expression_materials", [])
            ),
            character_facts=tuple(
                PersonaFact(
                    id=str(item["id"]),
                    content=str(item["content"]),
                    source_kind=str(item["source_kind"]),
                    source_ref=str(item.get("source_ref", "")),
                )
                for item in data.get("character_facts", [])
            ),
            participation_interests=tuple(
                ParticipationInterest(
                    id=str(item["id"]),
                    keywords=_string_tuple(
                        item.get("keywords", []),
                        "participation_interests.keywords",
                    ),
                    weight=float(item["weight"]),
                )
                for item in data.get("participation_interests", [])
            ),
            sources=tuple(
                PersonaSource(
                    id=str(item["id"]),
                    source_kind=str(item["source_kind"]),
                    reference=str(item["reference"]),
                    purpose=str(item["purpose"]),
                )
                for item in data.get("sources", [])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("language ", "人格包")):
            raise
        raise ValueError(f"人格包结构无效: {exc}") from exc
    return package


def load_persona_package(path: str | Path) -> PersonaPackage:
    asset_path = Path(path)
    try:
        raw = json.loads(asset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取人格包 {asset_path.name}: {exc}") from exc
    return persona_package_from_mapping(raw)
