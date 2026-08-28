from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


GROUPS: dict[str, tuple[str, ...]] = {
    "basic_settings": (
        "enabled",
        "persona_name",
        "replyer_provider_id",
        "enable_chat_bubbles",
        "chat_max_bubbles",
        "bubble_interval_min_ms",
        "bubble_interval_max_ms",
    ),
    "wake_settings": (
        "natural_name_wake_enabled",
        "natural_name_wake_mode",
        "natural_name_wake_aliases",
        "natural_name_wake_group_whitelist",
    ),
    "participation_settings": (
        "natural_group_participation_enabled",
        "natural_group_participation_rules",
        "natural_group_participation_allowlist",
        "natural_group_participation_min_context_messages",
        "natural_group_participation_cooldown_seconds",
        "natural_group_participation_window_minutes",
        "natural_group_participation_max_joins_per_window",
        "social_feedback_enabled",
        "social_feedback_window_minutes",
    ),
    "proactive_settings": (
        "proactive_initiation_enabled",
        "proactive_initiation_rules",
        "proactive_min_bubbles",
        "proactive_group_allowlist",
        "proactive_active_hour_start",
        "proactive_active_hour_end",
        "proactive_timezone_offset_minutes",
        "proactive_observation_minutes",
        "proactive_idle_minutes",
        "proactive_cooldown_minutes",
        "proactive_daily_limit",
        "proactive_scheduler_interval_seconds",
    ),
    "context_settings": (
        "prefer_livingmemory_group_history",
        "max_context_messages",
        "max_context_chars",
        "inject_verified_context",
    ),
    "permission_settings": (
        "owner_ids",
        "trusted_bot_identities",
        "permission_guard_enabled",
        "guest_allowed_tools",
        "permission_audit_log",
        "owner_action_enabled",
        "owner_action_artifact_read_exact_enabled",
        "owner_action_artifact_grep_enabled",
        "owner_action_memory_write_literal_enabled",
        "owner_action_sandbox_shell_once_enabled",
        "owner_action_artifact_root",
        "owner_action_artifact_path_flavor",
        "owner_action_shell_family",
    ),
    "performance_settings": (
        "inference_max_parallel",
        "inference_max_waiters",
        "inference_queue_timeout_seconds",
        "inference_active_timeout_seconds",
        "performance_window_samples",
        "continuity_max_scopes",
        "continuity_max_subjects",
        "debug_log",
    ),
}

REMOVED_MANAGER_OWNED_KEYS = frozenset(
    {
        "meme_complement_enabled",
        "meme_complement_cadence_turns",
        "meme_complement_cooldown_turns",
    }
)

NEW_PARTICIPATION_DEFAULTS = {
    "natural_group_participation_enabled": False,
    "natural_group_participation_min_context_messages": 2,
    "natural_group_participation_cooldown_seconds": 45,
    "natural_group_participation_window_minutes": 5,
    "natural_group_participation_max_joins_per_window": 2,
}

NATURAL_GROUP_PARTICIPATION_RULES = (
    "像已经在群里的普通成员一样，从当前多人话题中间顺势插一句。\n"
    "直接接最想回应的一点，可用接梗、短吐槽、补充、附和或轻微反驳。\n"
    "不要打招呼、复述题目、逐条总结或变成客服。\n"
    "不要强行追问某个人，不要把多人话题收束成一对一私聊。\n"
    "通常说一至两条短消息，让其他群友都能继续接话。"
)
PROACTIVE_INITIATION_RULES = (
    "面向整个群自然续上最近的公开话题，不主持、不采访、不用客服式暖场。\n"
    "优先回应近期讨论中仍有延续空间的一点，可说短感想、联想、吐槽、补充或轻量分享。\n"
    "没有可靠公开话题、检索失败或证据不足时保持沉默，不为找话题而搜索。\n"
    "不要求问句；确实自然时最多带一个容易接的问题。\n"
    "不要用‘大家好’‘有人吗’‘你们怎么看’‘今天过得怎么样’‘有什么想聊的吗’。\n"
    "不要解释为什么突然说话、群安静多久或后台机制。\n"
    "通常说一至两条短消息。"
)
NEW_RULE_DEFAULTS = {
    "natural_group_participation_rules": NATURAL_GROUP_PARTICIPATION_RULES,
    "proactive_initiation_rules": PROACTIVE_INITIATION_RULES,
}
NEW_PROACTIVE_DEFAULTS = {
    "proactive_min_bubbles": 2,
}
LEGACY_RULE_ALIASES = {
    "ambient_participation_rules": "natural_group_participation_rules",
    "quiet_topic_rules": "proactive_initiation_rules",
}


def migrate(source: dict[str, object]) -> dict[str, dict[str, object]]:
    if not isinstance(source, dict):
        raise ValueError("source_config_not_object")
    known_groups = set(GROUPS)
    if known_groups.intersection(source):
        source_groups = set(source)
        accepted_group_sets = (known_groups, known_groups | {"meme_settings"})
        if source_groups not in accepted_group_sets:
            raise ValueError("partially_grouped_config_refused")
        if "meme_settings" in source:
            removed = source["meme_settings"]
            if (
                not isinstance(removed, dict)
                or set(removed) != REMOVED_MANAGER_OWNED_KEYS
            ):
                raise ValueError("grouped_config_key_mismatch:meme_settings")
        upgraded: dict[str, dict[str, object]] = {}
        for group, keys in GROUPS.items():
            items = source[group]
            if not isinstance(items, dict):
                raise ValueError(f"grouped_config_key_mismatch:{group}")
            missing = set(keys) - set(items)
            unknown = set(items) - set(keys)
            if unknown or not missing.issubset(
                set(NEW_RULE_DEFAULTS) | set(NEW_PROACTIVE_DEFAULTS)
            ):
                raise ValueError(f"grouped_config_key_mismatch:{group}")
            upgraded[group] = dict(items)
            for key in missing:
                upgraded[group][key] = (
                    NEW_RULE_DEFAULTS[key]
                    if key in NEW_RULE_DEFAULTS
                    else NEW_PROACTIVE_DEFAULTS[key]
                )
        return upgraded

    expected_legacy = {
        key
        for keys in GROUPS.values()
        for key in keys
        if key not in NEW_PARTICIPATION_DEFAULTS
        and key not in NEW_RULE_DEFAULTS
        and key not in NEW_PROACTIVE_DEFAULTS
        and key != "natural_group_participation_allowlist"
    }
    unknown = (
        set(source)
        - expected_legacy
        - set(NEW_RULE_DEFAULTS)
        - set(LEGACY_RULE_ALIASES)
        - REMOVED_MANAGER_OWNED_KEYS
    )
    missing = expected_legacy - set(source)
    if unknown or missing:
        raise ValueError(
            "legacy_config_key_mismatch:"
            f"unknown={len(unknown)}:missing={len(missing)}"
        )

    values = dict(source)
    values.update(NEW_PARTICIPATION_DEFAULTS)
    values.update(NEW_PROACTIVE_DEFAULTS)
    for legacy_key, new_key in LEGACY_RULE_ALIASES.items():
        legacy_value = values.pop(legacy_key, None)
        current_value = values.get(new_key)
        values[new_key] = (
            current_value.strip()
            if isinstance(current_value, str) and current_value.strip()
            else
            legacy_value.strip()
            if isinstance(legacy_value, str) and legacy_value.strip()
            else NEW_RULE_DEFAULTS[new_key]
        )
    values["natural_group_participation_allowlist"] = list(
        values.get("proactive_group_allowlist", []) or []
    )
    # Normalize values whose old runtime already clamped them to these minima.
    values["proactive_scheduler_interval_seconds"] = 60
    for removed_key in REMOVED_MANAGER_OWNED_KEYS:
        values.pop(removed_key, None)

    migrated = {
        group: {key: values[key] for key in keys}
        for group, keys in GROUPS.items()
    }
    if sum(len(items) for items in migrated.values()) != 57:
        raise ValueError("migrated_leaf_count_invalid")
    return migrated


def write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source = json.loads(args.source.read_text(encoding="utf-8-sig"))
    write_atomic(args.destination, migrate(source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
