"""BUG-003 current-instance ownership tests for official event references."""

from __future__ import annotations

import gc
import weakref

from astrbot_plugin_shio.core.active_event_fence import ActiveEventFence


class _Event:
    def __init__(self) -> None:
        self.stop_count = 0
        self.extras = {}

    def stop_event(self) -> None:
        self.stop_count += 1

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value) -> None:
        self.extras[key] = value


def test_close_stops_only_registered_current_instance_events_and_is_idempotent():
    old = ActiveEventFence()
    new = ActiveEventFence()
    active = _Event()
    completed = _Event()
    other_instance = _Event()

    assert old.register(active) is True
    assert old.register(completed) is True
    assert new.register(other_instance) is True
    assert old.owns(active) is True
    assert new.owns(active) is False
    assert new.owns(other_instance) is True
    assert old.is_managed(active) is True
    assert new.is_managed(active) is True
    old.discard(completed)
    assert old.owns(completed) is False
    assert old.is_managed(completed) is False

    old.close()
    old.close()

    assert old.owns(active) is False
    assert old.is_managed(active) is True
    assert new.is_managed(active) is True
    assert active.stop_count == 1
    assert completed.stop_count == 0
    assert other_instance.stop_count == 0

    late = _Event()
    assert old.register(late) is False
    assert late.stop_count == 1
    assert new.register(late) is True
    new.discard(late)
    new.close()
    assert other_instance.stop_count == 1
    assert late.stop_count == 1


def test_discard_is_identity_scoped_and_unknown_events_are_noops():
    fence = ActiveEventFence()
    registered = _Event()
    unknown = _Event()

    assert fence.is_managed(unknown) is False
    assert fence.register(registered) is True
    fence.discard(unknown)
    fence.close()

    assert registered.stop_count == 1
    assert unknown.stop_count == 0


def test_register_rejects_an_event_with_another_live_or_stale_owner():
    old = ActiveEventFence()
    new = ActiveEventFence()
    event = _Event()

    assert old.register(event) is True
    assert new.register(event) is False
    assert event.stop_count == 1
    assert old.owns(event) is True
    assert new.owns(event) is False

    old.close()
    assert event.stop_count == 2
    assert new.register(event) is False
    assert event.stop_count == 3
    assert new.is_managed(event) is True


def test_registration_does_not_extend_completed_event_lifetime():
    fence = ActiveEventFence()
    event = _Event()
    observed = weakref.ref(event)
    assert fence.register(event) is True

    del event
    gc.collect()

    assert observed() is None
    fence.close()
