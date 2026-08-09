from __future__ import annotations

import weakref
from typing import Any

try:
    from astrbot.core.star.filter.custom_filter import CustomFilter
except ImportError:  # 仅供仓库轻量 Stub 测试使用。
    class CustomFilter:  # type: ignore[no-redef]
        def __init__(self, raise_error: bool = True, **kwargs: Any) -> None:
            self.raise_error = raise_error


_active_plugin: weakref.ReferenceType[Any] | None = None


def bind_participation_plugin(plugin: Any | None) -> None:
    global _active_plugin
    _active_plugin = weakref.ref(plugin) if plugin is not None else None


def unbind_participation_plugin(plugin: Any) -> None:
    global _active_plugin
    current = _active_plugin() if _active_plugin is not None else None
    if current is plugin:
        _active_plugin = None


class AmbientParticipationFilter(CustomFilter):
    """让星汐只唤醒自己的未点名群聊参与处理器。"""

    def filter(self, event: Any, cfg: Any) -> bool:
        plugin = _active_plugin() if _active_plugin is not None else None
        if plugin is None:
            return False
        try:
            return bool(plugin.ingest_ambient_event(event))
        except Exception as exc:
            plugin.log_ambient_filter_error(exc)
            return False
