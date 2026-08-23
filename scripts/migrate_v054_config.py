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
    "meme_settings": (
        "meme_complement_enabled",
        "meme_complement_cadence_turns",
        "meme_complement_cooldown_turns",
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

NEW_PARTICIPATION_DEFAULTS = {
    "natural_group_participation_enabled": False,
    "natural_group_participation_min_context_messages": 2,
    "natural_group_participation_cooldown_seconds": 45,
    "natural_group_participation_window_minutes": 5,
    "natural_group_participation_max_joins_per_window": 2,
}


def migrate(source: dict[str, object]) -> dict[str, dict[str, object]]:
    if not isinstance(source, dict):
        raise ValueError("source_config_not_object")
    known_groups = set(GROUPS)
    if known_groups.intersection(source):
        if set(source) != known_groups:
            raise ValueError("partially_grouped_config_refused")
        for group, keys in GROUPS.items():
            items = source[group]
            if not isinstance(items, dict) or set(items) != set(keys):
                raise ValueError(f"grouped_config_key_mismatch:{group}")
        return {group: dict(source[group]) for group in GROUPS}

    expected_legacy = {
        key
        for keys in GROUPS.values()
        for key in keys
        if key not in NEW_PARTICIPATION_DEFAULTS
        and key != "natural_group_participation_allowlist"
    }
    unknown = set(source) - expected_legacy
    missing = expected_legacy - set(source)
    if unknown or missing:
        raise ValueError(
            "legacy_config_key_mismatch:"
            f"unknown={len(unknown)}:missing={len(missing)}"
        )

    values = dict(source)
    values.update(NEW_PARTICIPATION_DEFAULTS)
    values["natural_group_participation_allowlist"] = list(
        values.get("proactive_group_allowlist", []) or []
    )
    # Normalize values whose old runtime already clamped them to these minima.
    values["proactive_scheduler_interval_seconds"] = 60
    values["meme_complement_cadence_turns"] = max(
        1,
        int(values["meme_complement_cadence_turns"]),
    )
    values["meme_complement_cooldown_turns"] = max(
        1,
        int(values["meme_complement_cooldown_turns"]),
    )

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
