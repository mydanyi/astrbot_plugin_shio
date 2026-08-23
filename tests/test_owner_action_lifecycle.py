from __future__ import annotations

import gc
import inspect
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_shio.core import owner_action_lifecycle as lifecycle_module
from astrbot_plugin_shio.core.contracts import (
    ActionEffectState,
    ActionReceiptStatus,
    OwnerActionOperation,
)
from astrbot_plugin_shio.core.owner_action_lifecycle import (
    LifecyclePhase,
    LifecycleReserveDisposition,
    LifecycleStoreDisabled,
    LifecycleTransitionRejected,
    OwnerActionLifecycleStore,
)


SECRET = bytes(range(32))


def _digest(index: int) -> str:
    return f"{index:064x}"


def _close_if_supported(store: OwnerActionLifecycleStore) -> None:
    close = getattr(store, "close", None)
    if close is not None:
        close()


def _hold_lifecycle_store(root: str, ready: object, release: object) -> None:
    store = OwnerActionLifecycleStore(
        root,
        install_secret=SECRET,
        recovery_now=10.0,
    )
    ready.put((store.enabled, store.failure_code))
    release.get(timeout=20.0)
    _close_if_supported(store)


class OwnerActionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._stores: list[OwnerActionLifecycleStore] = []

    @contextmanager
    def _root(self):
        first_store = len(self._stores)
        with tempfile.TemporaryDirectory() as root:
            try:
                yield root
            finally:
                for store in reversed(self._stores[first_store:]):
                    _close_if_supported(store)
                del self._stores[first_store:]

    def _store(
        self,
        root: str,
        *,
        secret: bytes = SECRET,
        now: float = 10.0,
        max_records: int = 256,
        ttl: float = 900.0,
    ) -> OwnerActionLifecycleStore:
        store = OwnerActionLifecycleStore(
            root,
            install_secret=secret,
            recovery_now=now,
            max_records=max_records,
            tombstone_ttl_seconds=ttl,
        )
        self._stores.append(store)
        return store

    def test_redacted_minimal_journal_and_happy_lifecycle(self) -> None:
        with self._root() as root:
            store = self._store(root)
            claim = store.reserve(
                OwnerActionOperation.MEMORY_WRITE_LITERAL,
                request_digest=_digest(1),
                now=11.0,
            )
            self.assertIs(
                claim.disposition,
                LifecycleReserveDisposition.RESERVED,
            )
            started = store.mark_in_progress(claim.handle, now=12.0)
            self.assertTrue(started.claimed)
            terminal = store.mark_terminal(
                claim.handle,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.COMMITTED,
                attempted=True,
                now=13.0,
            )
            self.assertIs(terminal.phase, LifecyclePhase.TERMINAL)
            acked = store.acknowledge_delivery(claim.handle, now=14.0)
            self.assertIs(acked.phase, LifecyclePhase.DELIVERY_ACK)

            journal = (Path(root) / "owner_action_lifecycle.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(_digest(1), journal)
            self.assertNotIn(SECRET.hex(), journal)
            for forbidden in (
                "command",
                "path",
                "memory",
                "tool_output",
                "sender_id",
                "message_id",
                "request_digest",
            ):
                self.assertNotIn(f'"{forbidden}"', journal)
            payload = json.loads(journal)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(len(payload["records"]), 1)
            self.assertEqual(
                set(payload["records"][0]),
                {
                    "attempted",
                    "created_at",
                    "delivery_ack_at",
                    "effect",
                    "fingerprint",
                    "operation",
                    "phase",
                    "result_status",
                    "terminal_at",
                    "updated_at",
                },
            )

    def test_restart_recovers_reserved_as_stale_not_started(self) -> None:
        with self._root() as root:
            first = self._store(root)
            first.reserve(
                OwnerActionOperation.MEMORY_WRITE_LITERAL,
                request_digest=_digest(2),
                now=11.0,
            )
            _close_if_supported(first)

            recovered = self._store(root, now=20.0)
            claim = recovered.reserve(
                OwnerActionOperation.MEMORY_WRITE_LITERAL,
                request_digest=_digest(2),
                now=21.0,
            )
            self.assertIs(
                claim.disposition,
                LifecycleReserveDisposition.EXISTING_TERMINAL,
            )
            self.assertIs(claim.snapshot.result_status, ActionReceiptStatus.STALE)
            self.assertIs(claim.snapshot.effect_state, ActionEffectState.NOT_STARTED)
            self.assertFalse(claim.snapshot.attempted)
            self.assertFalse(
                recovered.mark_in_progress(claim.handle, now=22.0).claimed
            )

    def test_restart_recovers_read_in_progress_as_failed_no_effect(self) -> None:
        with self._root() as root:
            first = self._store(root)
            claim = first.reserve(
                OwnerActionOperation.ARTIFACT_READ_EXACT,
                request_digest=_digest(3),
                now=11.0,
            )
            first.mark_in_progress(claim.handle, now=12.0)
            _close_if_supported(first)

            recovered = self._store(root, now=20.0)
            existing = recovered.reserve(
                OwnerActionOperation.ARTIFACT_READ_EXACT,
                request_digest=_digest(3),
                now=21.0,
            )
            self.assertIs(existing.snapshot.result_status, ActionReceiptStatus.FAILED)
            self.assertIs(
                existing.snapshot.effect_state,
                ActionEffectState.NO_SIDE_EFFECT,
            )
            self.assertTrue(existing.snapshot.attempted)

    def test_restart_recovers_mutation_in_progress_as_effect_unknown(self) -> None:
        for operation in (
            OwnerActionOperation.MEMORY_WRITE_LITERAL,
            OwnerActionOperation.SANDBOX_SHELL_ONCE,
        ):
            with self.subTest(operation=operation.value), self._root() as root:
                first = self._store(root)
                claim = first.reserve(operation, request_digest=_digest(4), now=11.0)
                first.mark_in_progress(claim.handle, now=12.0)
                _close_if_supported(first)

                recovered = self._store(root, now=20.0)
                existing = recovered.reserve(
                    operation,
                    request_digest=_digest(4),
                    now=21.0,
                )
                self.assertIs(
                    existing.snapshot.result_status,
                    ActionReceiptStatus.EFFECT_UNKNOWN,
                )
                self.assertIs(
                    existing.snapshot.effect_state,
                    ActionEffectState.UNKNOWN,
                )
                self.assertTrue(existing.snapshot.attempted)

    def test_terminal_survives_restart_and_never_reclaims_execution(self) -> None:
        with self._root() as root:
            first = self._store(root)
            claim = first.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(5),
                now=11.0,
            )
            first.mark_in_progress(claim.handle, now=12.0)
            first.mark_terminal(
                claim.handle,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                attempted=True,
                now=13.0,
            )
            _close_if_supported(first)

            recovered = self._store(root, now=20.0)
            existing = recovered.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(5),
                now=21.0,
            )
            self.assertIs(existing.snapshot.result_status, ActionReceiptStatus.SUCCEEDED)
            self.assertFalse(
                recovered.mark_in_progress(existing.handle, now=22.0).claimed
            )

    def test_bad_schema_corruption_and_secret_mismatch_fail_closed_without_rewrite(self) -> None:
        scenarios: list[tuple[str, callable]] = [
            (
                "unknown_schema",
                lambda path: path.write_bytes(
                    (
                        json.dumps(
                            {
                                **json.loads(path.read_text(encoding="utf-8")),
                                "schema_version": 999,
                            },
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                ),
            ),
            ("corrupt", lambda path: path.write_text("{broken", encoding="utf-8")),
        ]
        for name, mutator in scenarios:
            with self.subTest(name=name), self._root() as root:
                original = self._store(root)
                _close_if_supported(original)
                path = Path(root) / "owner_action_lifecycle.json"
                mutator(path)
                before = path.read_bytes()
                disabled = self._store(root, now=20.0)
                self.assertFalse(disabled.enabled)
                with self.assertRaises(LifecycleStoreDisabled):
                    disabled.reserve(
                        OwnerActionOperation.ARTIFACT_GREP,
                        request_digest=_digest(6),
                        now=21.0,
                    )
                self.assertEqual(path.read_bytes(), before)
                repeated = self._store(root, now=21.0)
                self.assertFalse(repeated.enabled)
                self.assertNotEqual(repeated.failure_code, "lifecycle_store_locked")

        with self._root() as root:
            original = self._store(root)
            _close_if_supported(original)
            path = Path(root) / "owner_action_lifecycle.json"
            before = path.read_bytes()
            disabled = self._store(root, secret=b"x" * 32, now=20.0)
            self.assertFalse(disabled.enabled)
            self.assertEqual(disabled.failure_code, "install_secret_mismatch")
            self.assertEqual(path.read_bytes(), before)
            recovered = self._store(root, now=21.0)
            self.assertTrue(recovered.enabled)

    def test_concurrent_reserve_and_start_have_one_winner(self) -> None:
        with self._root() as root:
            store = self._store(root)
            barrier = threading.Barrier(32)
            claims = []
            starts = []
            failures = []
            lock = threading.Lock()

            def worker() -> None:
                try:
                    barrier.wait()
                    claim = store.reserve(
                        OwnerActionOperation.ARTIFACT_READ_EXACT,
                        request_digest=_digest(7),
                        now=11.0,
                    )
                    started = store.mark_in_progress(claim.handle, now=12.0)
                    with lock:
                        claims.append(claim)
                        starts.append(started)
                except BaseException as exc:  # pragma: no cover - failure capture
                    with lock:
                        failures.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(32)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            self.assertEqual(
                sum(
                    claim.disposition is LifecycleReserveDisposition.RESERVED
                    for claim in claims
                ),
                1,
            )
            self.assertEqual(sum(start.claimed for start in starts), 1)

    def test_copied_handle_and_conflicting_terminal_are_rejected(self) -> None:
        with self._root() as root:
            store = self._store(root)
            claim = store.reserve(
                OwnerActionOperation.MEMORY_WRITE_LITERAL,
                request_digest=_digest(8),
                now=11.0,
            )
            with self.assertRaises(LifecycleTransitionRejected):
                store.mark_in_progress(replace(claim.handle), now=12.0)
            store.mark_in_progress(claim.handle, now=12.0)
            store.mark_terminal(
                claim.handle,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.COMMITTED,
                attempted=True,
                now=13.0,
            )
            with self.assertRaises(LifecycleTransitionRejected):
                store.mark_terminal(
                    claim.handle,
                    status=ActionReceiptStatus.FAILED,
                    effect_state=ActionEffectState.NOT_COMMITTED,
                    attempted=True,
                    now=14.0,
                )

    def test_invalid_terminal_semantics_fail_closed(self) -> None:
        invalid = (
            (
                ActionReceiptStatus.SUCCEEDED,
                ActionEffectState.COMMITTED,
                True,
                OwnerActionOperation.ARTIFACT_READ_EXACT,
            ),
            (
                ActionReceiptStatus.DENIED,
                ActionEffectState.NOT_STARTED,
                True,
                OwnerActionOperation.MEMORY_WRITE_LITERAL,
            ),
            (
                ActionReceiptStatus.EFFECT_UNKNOWN,
                ActionEffectState.UNKNOWN,
                False,
                OwnerActionOperation.MEMORY_WRITE_LITERAL,
            ),
        )
        for index, (status, effect, attempted, operation) in enumerate(invalid, 20):
            with self.subTest(status=status.value), self._root() as root:
                store = self._store(root)
                claim = store.reserve(operation, request_digest=_digest(index), now=11.0)
                if attempted:
                    store.mark_in_progress(claim.handle, now=12.0)
                with self.assertRaises(LifecycleTransitionRejected):
                    store.mark_terminal(
                        claim.handle,
                        status=status,
                        effect_state=effect,
                        attempted=attempted,
                        now=13.0,
                    )

    def test_atomic_replace_failure_keeps_previous_journal(self) -> None:
        with self._root() as root:
            store = self._store(root)
            path = Path(root) / "owner_action_lifecycle.json"
            before = path.read_bytes()
            with patch(
                "astrbot_plugin_shio.core.owner_action_lifecycle.os.replace",
                side_effect=OSError("simulated crash"),
            ):
                with self.assertRaises(LifecycleStoreDisabled):
                    store.reserve(
                        OwnerActionOperation.ARTIFACT_GREP,
                        request_digest=_digest(9),
                        now=11.0,
                    )
            self.assertEqual(path.read_bytes(), before)
            recovered = self._store(root, now=20.0)
            self.assertTrue(recovered.enabled)
            self.assertEqual(recovered.trace_metadata()["record_count"], 0)
            self.assertEqual(list(Path(root).glob("*.tmp")), [])

    def test_count_bound_prune_and_1024_rounds(self) -> None:
        with self._root() as root:
            store = self._store(root, max_records=8, ttl=900.0)
            for index in range(1024):
                now = 1000.0 * index + 11.0
                claim = store.reserve(
                    OwnerActionOperation.ARTIFACT_READ_EXACT,
                    request_digest=_digest(index + 100),
                    now=now,
                )
                self.assertTrue(store.mark_in_progress(claim.handle, now=now + 1).claimed)
                store.mark_terminal(
                    claim.handle,
                    status=ActionReceiptStatus.SUCCEEDED,
                    effect_state=ActionEffectState.NO_SIDE_EFFECT,
                    attempted=True,
                    now=now + 2,
                )
                store.acknowledge_delivery(claim.handle, now=now + 3)
                store.prune(now=now + 904)
                if index in {511, 1023}:
                    self.assertLessEqual(store.trace_metadata()["record_count"], 1)
            del claim
            gc.collect()
            self.assertEqual(store.trace_metadata()["record_count"], 0)
            self.assertEqual(len(store._handles), 0)

        with self._root() as root:
            store = self._store(root, max_records=1)
            active = store.reserve(
                OwnerActionOperation.ARTIFACT_READ_EXACT,
                request_digest=_digest(700),
                now=11.0,
            )
            with self.assertRaises(LifecycleTransitionRejected):
                store.reserve(
                    OwnerActionOperation.ARTIFACT_READ_EXACT,
                    request_digest=_digest(701),
                    now=100_000.0,
                )
            store.mark_in_progress(active.handle, now=100_001.0)
            store.mark_terminal(
                active.handle,
                status=ActionReceiptStatus.SUCCEEDED,
                effect_state=ActionEffectState.NO_SIDE_EFFECT,
                attempted=True,
                now=100_002.0,
            )
            with self.assertRaises(LifecycleTransitionRejected):
                store.reserve(
                    OwnerActionOperation.ARTIFACT_READ_EXACT,
                    request_digest=_digest(702),
                    now=200_000.0,
                )
            store.acknowledge_delivery(active.handle, now=200_001.0)
            with self.assertRaises(LifecycleTransitionRejected):
                store.reserve(
                    OwnerActionOperation.ARTIFACT_READ_EXACT,
                    request_digest=_digest(703),
                    now=200_899.0,
                )

    def test_repr_and_trace_are_redacted(self) -> None:
        with self._root() as root:
            store = self._store(root)
            claim = store.reserve(
                OwnerActionOperation.ARTIFACT_READ_EXACT,
                request_digest=_digest(800),
                now=11.0,
            )
            rendered = " ".join(
                (
                    repr(store),
                    repr(claim),
                    repr(claim.handle),
                    repr(claim.snapshot),
                    repr(store.trace_metadata()),
                )
            )
            self.assertNotIn(root, rendered)
            self.assertNotIn(_digest(800), rendered)
            self.assertNotIn(SECRET.hex(), rendered)
            self.assertNotIn("fingerprint", rendered.lower())
            self.assertFalse(store.trace_metadata()["request_material_visible"])
            self.assertFalse(store.trace_metadata()["install_secret_visible"])

    def test_terminal_semantics_exactly_match_canonical_receipt_matrix(self) -> None:
        reads = {
            OwnerActionOperation.ARTIFACT_READ_EXACT,
            OwnerActionOperation.ARTIFACT_GREP,
        }
        mutations = {
            OwnerActionOperation.MEMORY_WRITE_LITERAL,
            OwnerActionOperation.SANDBOX_SHELL_ONCE,
        }

        def expected(
            operation: OwnerActionOperation,
            status: ActionReceiptStatus,
            effect: ActionEffectState,
            attempted: bool,
        ) -> bool:
            if status is ActionReceiptStatus.CONFIRMATION_REQUIRED:
                return False
            if status in {ActionReceiptStatus.DENIED, ActionReceiptStatus.CANCELLED}:
                return not attempted and effect is ActionEffectState.NOT_STARTED
            if status is ActionReceiptStatus.SUCCEEDED:
                return attempted and (
                    (operation in reads and effect is ActionEffectState.NO_SIDE_EFFECT)
                    or (operation in mutations and effect is ActionEffectState.COMMITTED)
                )
            if status is ActionReceiptStatus.FAILED:
                return attempted and (
                    (operation in reads and effect is ActionEffectState.NO_SIDE_EFFECT)
                    or (
                        operation in mutations
                        and effect
                        in {ActionEffectState.NOT_COMMITTED, ActionEffectState.PARTIAL}
                    )
                )
            if status is ActionReceiptStatus.STALE:
                return (
                    not attempted and effect is ActionEffectState.NOT_STARTED
                ) or (
                    attempted
                    and (
                        (operation in reads and effect is ActionEffectState.NO_SIDE_EFFECT)
                        or (
                            operation in mutations
                            and effect
                            in {
                                ActionEffectState.NOT_COMMITTED,
                                ActionEffectState.COMMITTED,
                                ActionEffectState.PARTIAL,
                                ActionEffectState.UNKNOWN,
                            }
                        )
                    )
                )
            if status is ActionReceiptStatus.TIMED_OUT:
                return attempted and (
                    (operation in reads and effect is ActionEffectState.NO_SIDE_EFFECT)
                    or (
                        operation in mutations
                        and effect
                        in {
                            ActionEffectState.NOT_COMMITTED,
                            ActionEffectState.PARTIAL,
                            ActionEffectState.UNKNOWN,
                        }
                    )
                )
            return (
                status is ActionReceiptStatus.EFFECT_UNKNOWN
                and attempted
                and operation in mutations
                and effect is ActionEffectState.UNKNOWN
            )

        from astrbot_plugin_shio.core.owner_action_lifecycle import (
            _validate_terminal_semantics,
        )

        for operation in OwnerActionOperation:
            for status in ActionReceiptStatus:
                for effect in ActionEffectState:
                    for attempted in (False, True):
                        with self.subTest(
                            operation=operation.value,
                            status=status.value,
                            effect=effect.value,
                            attempted=attempted,
                        ):
                            if expected(operation, status, effect, attempted):
                                _validate_terminal_semantics(
                                    operation, status, effect, attempted
                                )
                            else:
                                with self.assertRaises(LifecycleTransitionRejected):
                                    _validate_terminal_semantics(
                                        operation, status, effect, attempted
                                    )

    def test_request_digest_requires_exact_str(self) -> None:
        class RedirectingDigest(str):
            def encode(self, *_args: object, **_kwargs: object) -> bytes:
                return _digest(902).encode("ascii")

        with self._root() as root:
            store = self._store(root)
            with self.assertRaises(LifecycleTransitionRejected):
                store.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=RedirectingDigest(_digest(901)),
                    now=11.0,
                )
            self.assertIsNone(
                store.snapshot_for(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(902),
                )
            )

    def test_single_writer_lock_rejects_second_live_store_without_recovery(self) -> None:
        with self._root() as root:
            first = self._store(root)
            claim = first.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(903),
                now=11.0,
            )
            before = (Path(root) / "owner_action_lifecycle.json").read_bytes()
            second = self._store(root, now=20.0)
            self.assertFalse(second.enabled)
            self.assertEqual(second.failure_code, "lifecycle_store_locked")
            self.assertEqual(
                (Path(root) / "owner_action_lifecycle.json").read_bytes(), before
            )
            self.assertTrue(first.mark_in_progress(claim.handle, now=12.0).claimed)
            _close_if_supported(first)

    def test_single_writer_lock_is_cross_process_and_nonblocking(self) -> None:
        with self._root() as root:
            context = multiprocessing.get_context("spawn")
            ready = context.Queue()
            release = context.Queue()
            process = context.Process(
                target=_hold_lifecycle_store,
                args=(root, ready, release),
            )
            process.start()
            self.assertEqual(ready.get(timeout=20.0), (True, ""))
            try:
                second = self._store(root, now=20.0)
                self.assertFalse(second.enabled)
                self.assertEqual(second.failure_code, "lifecycle_store_locked")
            finally:
                release.put(True)
                process.join(timeout=20.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5.0)
            self.assertEqual(process.exitcode, 0)

    def test_exact_numeric_types_reject_subclasses_and_bound_bypass(self) -> None:
        class RedirectingInt(int):
            def __ge__(self, _other: object) -> bool:
                return True

            def __le__(self, _other: object) -> bool:
                return True

            def __lt__(self, _other: object) -> bool:
                return False

            def __gt__(self, _other: object) -> bool:
                return False

        class IntTime(int):
            pass

        class FloatTime(float):
            pass

        with self._root() as root:
            with self.assertRaises(LifecycleTransitionRejected):
                OwnerActionLifecycleStore(
                    root,
                    install_secret=SECRET,
                    recovery_now=10.0,
                    max_records=RedirectingInt(100_000),
                )
            with self.assertRaises(LifecycleTransitionRejected):
                OwnerActionLifecycleStore(
                    root,
                    install_secret=SECRET,
                    recovery_now=IntTime(10),
                )
            with self.assertRaises(LifecycleTransitionRejected):
                OwnerActionLifecycleStore(
                    root,
                    install_secret=SECRET,
                    recovery_now=10.0,
                    tombstone_ttl_seconds=FloatTime(900.0),
                )

            store = self._store(root)
            for value in (True, IntTime(11), FloatTime(11.0), 10**10_000):
                with self.subTest(now_type=type(value).__name__):
                    with self.assertRaises(LifecycleTransitionRejected):
                        store.reserve(
                            OwnerActionOperation.ARTIFACT_GREP,
                            request_digest=_digest(950),
                            now=value,
                        )
            claim = store.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(953),
                now=11.0,
            )
            store.mark_in_progress(claim.handle, now=12.0)
            with self.assertRaises(LifecycleTransitionRejected):
                store.mark_terminal(
                    claim.handle,
                    status=ActionReceiptStatus.FAILED,
                    effect_state=ActionEffectState.NO_SIDE_EFFECT,
                    attempted=1,  # type: ignore[arg-type]
                    now=13.0,
                )

    def test_strict_canonical_json_rejects_ambiguous_or_oversized_input(self) -> None:
        mutators = {
            "duplicate_key": lambda raw: raw.replace(
                b"{", b'{"schema_version":1,', 1
            ),
            "bool_schema": lambda raw: raw.replace(
                b'"schema_version":1', b'"schema_version":true'
            ),
            "bool_generation": lambda raw: raw.replace(
                b'"generation":1', b'"generation":true'
            ),
            "nan": lambda raw: raw.replace(b'"generation":1', b'"generation":NaN'),
            "padding": lambda raw: b" " + raw,
            "oversized": lambda raw: (b" " * (5 * 1024 * 1024)) + raw,
        }
        for name, mutator in mutators.items():
            with self.subTest(name=name), self._root() as root:
                original = self._store(root)
                _close_if_supported(original)
                path = Path(root) / "owner_action_lifecycle.json"
                path.write_bytes(mutator(path.read_bytes()))
                before = path.read_bytes()
                disabled = self._store(root, now=20.0)
                self.assertFalse(disabled.enabled)
                self.assertEqual(path.read_bytes(), before)

        with self._root() as root:
            original = self._store(root)
            original.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(910),
                now=11.0,
            )
            _close_if_supported(original)
            path = Path(root) / "owner_action_lifecycle.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["records"][0]["created_at"] = 11
            path.write_bytes(
                (
                    json.dumps(
                        payload,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
            disabled = self._store(root, now=20.0)
            self.assertFalse(disabled.enabled)
            self.assertEqual(disabled.failure_code, "created_at_invalid")

        with self._root() as root:
            original = self._store(root)
            original.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(955),
                now=11.0,
            )
            original.close()
            path = Path(root) / "owner_action_lifecycle.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["records"][0]["attempted"] = 1
            path.write_bytes(
                (
                    json.dumps(
                        payload,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
            disabled = self._store(root, now=20.0)
            self.assertFalse(disabled.enabled)
            self.assertEqual(disabled.failure_code, "record_attempted_invalid")

    @unittest.skipIf(os.name == "nt", "POSIX permission and inode semantics")
    def test_posix_permissions_and_late_journal_hardlink_fail_closed(self) -> None:
        with self._root() as base:
            permissive_root = Path(base) / "permissive"
            permissive_root.mkdir(mode=0o700)
            os.chmod(permissive_root, 0o755)
            disabled = self._store(str(permissive_root))
            self.assertFalse(disabled.enabled)
            self.assertEqual(
                disabled.failure_code,
                "lifecycle_root_permissions_invalid",
            )

        with self._root() as root:
            original = self._store(root)
            original.close()
            journal = Path(root) / "owner_action_lifecycle.json"
            os.chmod(journal, 0o666)
            disabled = self._store(root, now=20.0)
            self.assertFalse(disabled.enabled)
            self.assertEqual(disabled.failure_code, "journal_permissions_invalid")
            self.assertEqual(journal.stat().st_mode & 0o777, 0o666)

        with self._root() as root:
            original = self._store(root)
            original.close()
            lock_path = Path(root) / ".owner_action_lifecycle.lock"
            os.chmod(lock_path, 0o666)
            disabled = self._store(root, now=20.0)
            self.assertFalse(disabled.enabled)
            self.assertEqual(
                disabled.failure_code,
                "lifecycle_lock_permissions_invalid",
            )
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o666)

        with self._root() as root:
            original = self._store(root)
            original.close()
            journal = Path(root) / "owner_action_lifecycle.json"
            late_link = Path(root) / "late-journal-hardlink"
            real_open = os.open

            def open_after_hardlink(
                path: str | os.PathLike[str],
                flags: int,
                *args: object,
            ) -> int:
                if Path(path) == journal and not late_link.exists():
                    os.link(journal, late_link)
                return real_open(path, flags, *args)

            with patch(
                "astrbot_plugin_shio.core.owner_action_lifecycle.os.open",
                side_effect=open_after_hardlink,
            ):
                disabled = self._store(root, now=20.0)
            self.assertFalse(disabled.enabled)
            self.assertEqual(disabled.failure_code, "journal_not_regular")
            self.assertEqual(journal.stat().st_nlink, 2)

    @unittest.skipIf(os.name == "nt", "POSIX filesystem object semantics")
    def test_posix_root_journal_and_lock_shapes_fail_closed(self) -> None:
        with self._root() as base:
            target = Path(base) / "target"
            target.mkdir(mode=0o700)
            os.chmod(target, 0o700)
            linked_root = Path(base) / "linked-root"
            linked_root.symlink_to(target, target_is_directory=True)
            disabled = self._store(str(linked_root))
            self.assertFalse(disabled.enabled)
            self.assertEqual(
                disabled.failure_code,
                "lifecycle_root_symlink_rejected",
            )

        with self._root() as base:
            file_root = Path(base) / "file-root"
            file_root.write_bytes(b"not-a-directory")
            disabled = self._store(str(file_root))
            self.assertFalse(disabled.enabled)

        for object_name in ("journal_fifo", "lock_fifo"):
            with self.subTest(object_name=object_name), self._root() as base:
                root = Path(base) / "root"
                root.mkdir(mode=0o700)
                os.chmod(root, 0o700)
                path = root / (
                    "owner_action_lifecycle.json"
                    if object_name == "journal_fifo"
                    else ".owner_action_lifecycle.lock"
                )
                os.mkfifo(path)
                disabled = self._store(str(root))
                self.assertFalse(disabled.enabled)

        with self._root() as base:
            root = Path(base) / "root"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            external = Path(base) / "external-lock"
            external.write_bytes(b"")
            (root / ".owner_action_lifecycle.lock").symlink_to(external)
            disabled = self._store(str(root))
            self.assertFalse(disabled.enabled)
            self.assertEqual(
                disabled.failure_code,
                "lifecycle_lock_symlink_rejected",
            )

        with self._root() as base:
            root = Path(base) / "root"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            source = Path(base) / "hard-linked-lock"
            source.write_bytes(b"")
            os.link(source, root / ".owner_action_lifecycle.lock")
            disabled = self._store(str(root))
            self.assertFalse(disabled.enabled)
            self.assertEqual(
                disabled.failure_code,
                "lifecycle_lock_not_regular",
            )

    def test_post_replace_baseexception_disables_and_reconciles_from_disk(self) -> None:
        for exception_type in (RuntimeError, KeyboardInterrupt, SystemExit):
            with self.subTest(exception_type=exception_type.__name__), self._root() as root:
                store = self._store(root)
                journal = Path(root) / "owner_action_lifecycle.json"
                before = journal.read_bytes()
                real_replace = os.replace

                def replace_then_interrupt(
                    source: str | os.PathLike[str],
                    destination: str | os.PathLike[str],
                ) -> None:
                    real_replace(source, destination)
                    raise exception_type("interrupted after durable replace")

                with patch(
                    "astrbot_plugin_shio.core.owner_action_lifecycle.os.replace",
                    side_effect=replace_then_interrupt,
                ):
                    with self.assertRaises(exception_type) as caught:
                        store.reserve(
                            OwnerActionOperation.ARTIFACT_GREP,
                            request_digest=_digest(951),
                            now=11.0,
                        )
                self.assertNotIn(root, str(caught.exception))
                self.assertNotIn("interrupted after durable replace", str(caught.exception))

                self.assertNotEqual(journal.read_bytes(), before)
                self.assertFalse(store.enabled)
                with self.assertRaisesRegex(
                    LifecycleStoreDisabled,
                    "^atomic_write_interrupted$",
                ):
                    store.reserve(
                        OwnerActionOperation.ARTIFACT_GREP,
                        request_digest=_digest(951),
                        now=12.0,
                    )

                reopened = self._store(root, now=20.0)
                existing = reopened.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(951),
                    now=21.0,
                )
                self.assertIs(
                    existing.disposition,
                    LifecycleReserveDisposition.EXISTING_TERMINAL,
                )
                self.assertIs(
                    existing.snapshot.result_status,
                    ActionReceiptStatus.STALE,
                )
                reopened.close()

    def test_init_baseexception_releases_lock_with_retained_traceback(self) -> None:
        with self._root() as root:
            original = self._store(root)
            original.close()
            retained: list[BaseException] = []
            proc_fds = Path("/proc/self/fd")
            baseline_fds = (
                len(tuple(proc_fds.iterdir())) if proc_fds.is_dir() else None
            )

            for exception_type in (RuntimeError, KeyboardInterrupt, SystemExit):
                for attempt in range(40):
                    def interrupt_load(_store: OwnerActionLifecycleStore) -> None:
                        raise exception_type(
                            "interrupted after lock acquisition"
                        )

                    try:
                        with patch.object(
                            OwnerActionLifecycleStore,
                            "_load_existing",
                            interrupt_load,
                        ):
                            OwnerActionLifecycleStore(
                                root,
                                install_secret=SECRET,
                                recovery_now=20.0 + attempt,
                            )
                    except BaseException as exc:
                        self.assertIs(type(exc), exception_type)
                        self.assertEqual(str(exc), "")
                        retained.append(exc)

                    reopened = self._store(root, now=100.0 + attempt)
                    self.assertTrue(reopened.enabled)
                    reopened.close()

                if baseline_fds is not None:
                    self.assertEqual(len(tuple(proc_fds.iterdir())), baseline_fds)

            self.assertEqual(len(retained), 120)

    @unittest.skipIf(os.name == "nt", "POSIX directory-lock semantics")
    def test_unlinked_live_lock_cannot_create_a_second_writer(self) -> None:
        for mutation in ("unlink", "replace"):
            with self.subTest(mutation=mutation), self._root() as root:
                first = self._store(root)
                first.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(952),
                    now=11.0,
                )
                lock_path = Path(root) / ".owner_action_lifecycle.lock"
                if mutation == "unlink":
                    lock_path.unlink()
                else:
                    replacement = Path(root) / "replacement-lock"
                    replacement.touch(mode=0o600)
                    os.replace(replacement, lock_path)

                second = self._store(root, now=20.0)
                self.assertFalse(second.enabled)
                self.assertEqual(second.failure_code, "lifecycle_store_locked")
                self.assertFalse(first.enabled)
                with self.assertRaisesRegex(
                    LifecycleStoreDisabled,
                    "^lifecycle_lock_identity_changed$",
                ):
                    first.snapshot_for(
                        OwnerActionOperation.ARTIFACT_GREP,
                        request_digest=_digest(952),
                    )
                self.assertFalse(first.enabled)

                reopened = self._store(root, now=21.0)
                existing = reopened.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(952),
                    now=22.0,
                )
                self.assertIs(
                    existing.disposition,
                    LifecycleReserveDisposition.EXISTING_TERMINAL,
                )
                self.assertIs(
                    existing.snapshot.result_status,
                    ActionReceiptStatus.STALE,
                )
                reopened.close()

    @unittest.skipIf(os.name == "nt", "POSIX live-path health semantics")
    def test_every_health_surface_revalidates_lock_with_closed_codes(self) -> None:
        calls = (
            ("enabled", lambda store, claim: store.enabled),
            ("failure_code", lambda store, claim: store.failure_code),
            ("trace", lambda store, claim: store.trace_metadata()),
            ("repr", lambda store, claim: repr(store)),
            (
                "reserve",
                lambda store, claim: store.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(980),
                    now=12.0,
                ),
            ),
            (
                "mark_in_progress",
                lambda store, claim: store.mark_in_progress(
                    claim.handle,
                    now=12.0,
                ),
            ),
            (
                "mark_terminal",
                lambda store, claim: store.mark_terminal(
                    claim.handle,
                    status=ActionReceiptStatus.STALE,
                    effect_state=ActionEffectState.NOT_STARTED,
                    attempted=False,
                    now=12.0,
                ),
            ),
            (
                "acknowledge_delivery",
                lambda store, claim: store.acknowledge_delivery(
                    claim.handle,
                    now=12.0,
                ),
            ),
            (
                "snapshot_for",
                lambda store, claim: store.snapshot_for(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(979),
                ),
            ),
            ("prune", lambda store, claim: store.prune(now=12.0)),
            ("enter", lambda store, claim: store.__enter__()),
        )
        for name, invoke in calls:
            with self.subTest(name=name), self._root() as root:
                store = self._store(root)
                claim = store.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(979),
                    now=11.0,
                )
                (Path(root) / ".owner_action_lifecycle.lock").unlink()
                rendered: list[str] = []
                try:
                    rendered.append(repr(invoke(store, claim)))
                except LifecycleStoreDisabled as exc:
                    rendered.append(str(exc))
                self.assertFalse(store._enabled)
                self.assertEqual(
                    store._failure_code,
                    "lifecycle_lock_identity_changed",
                )
                self.assertEqual(store._lock_fd, -1)
                self.assertEqual(store._root_fd, -1)
                rendered.extend(
                    (
                        store.failure_code,
                        repr(store),
                        repr(store.trace_metadata()),
                    )
                )
                combined = "\n".join(rendered)
                self.assertNotIn(root, combined)
                self.assertNotIn(".owner_action_lifecycle.lock", combined)

    @unittest.skipIf(os.name == "nt", "POSIX live-path health semantics")
    def test_invalid_arguments_cannot_bypass_public_api_health_check(self) -> None:
        calls = (
            (
                "reserve",
                lambda store, claim: store.reserve(
                    object(),
                    request_digest="not-a-digest",
                    now="not-a-time",
                ),
            ),
            (
                "mark_in_progress",
                lambda store, claim: store.mark_in_progress(
                    claim.handle,
                    now="not-a-time",
                ),
            ),
            (
                "mark_terminal",
                lambda store, claim: store.mark_terminal(
                    claim.handle,
                    status=object(),
                    effect_state=object(),
                    attempted=object(),
                    now="not-a-time",
                ),
            ),
            (
                "acknowledge_delivery",
                lambda store, claim: store.acknowledge_delivery(
                    claim.handle,
                    now="not-a-time",
                ),
            ),
            (
                "snapshot_for",
                lambda store, claim: store.snapshot_for(
                    object(),
                    request_digest="not-a-digest",
                ),
            ),
            ("prune", lambda store, claim: store.prune(now="not-a-time")),
        )
        for name, invoke in calls:
            with self.subTest(name=name), self._root() as root:
                store = self._store(root)
                claim = store.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(981),
                    now=11.0,
                )
                (Path(root) / ".owner_action_lifecycle.lock").unlink()
                with self.assertRaisesRegex(
                    LifecycleStoreDisabled,
                    "^lifecycle_lock_identity_changed$",
                ):
                    invoke(store, claim)
                self.assertFalse(store._enabled)
                self.assertEqual(
                    store.failure_code,
                    "lifecycle_lock_identity_changed",
                )

    def test_failure_codes_are_closed_for_all_exception_classes(self) -> None:
        marker = "private-root-path-marker"
        cases = (
            (OSError(5, marker), "lifecycle_lock_identity_changed"),
            (ValueError(marker), "lifecycle_lock_identity_changed"),
            (RuntimeError(marker), "lifecycle_lock_validation_interrupted"),
            (KeyboardInterrupt(marker), "lifecycle_lock_validation_interrupted"),
            (SystemExit(marker), "lifecycle_lock_validation_interrupted"),
        )
        for injected, expected_code in cases:
            with self.subTest(exception_type=type(injected).__name__), self._root() as root:
                store = self._store(root)
                try:
                    with patch.object(
                        lifecycle_module.os,
                        "fstat",
                        side_effect=injected,
                    ):
                        store.enabled
                except BaseException as exc:
                    self.assertIs(type(exc), type(injected))
                    self.assertNotIn(marker, str(exc))
                    self.assertNotIn(root, str(exc))
                self.assertFalse(store._enabled)
                self.assertEqual(store.failure_code, expected_code)
                rendered = "\n".join(
                    (
                        store.failure_code,
                        repr(store),
                        repr(store.trace_metadata()),
                    )
                )
                self.assertNotIn(marker, rendered)
                self.assertNotIn(root, rendered)

        with self._root() as root:
            store = self._store(root)
            store._disable(marker)
            self.assertEqual(store.failure_code, "lifecycle_fail_closed")
            self.assertNotIn(marker, repr(store))

        disabled = OwnerActionLifecycleStore(
            "\x00private-root-path-marker",
            install_secret=SECRET,
            recovery_now=10.0,
        )
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.failure_code, "journal_load_failed")
        self.assertNotIn(marker, repr(disabled))

    def test_failure_code_source_has_no_exception_string_passthrough(self) -> None:
        source = inspect.getsource(lifecycle_module)
        self.assertNotIn("str(exc)", source)
        self.assertNotIn("LifecycleStoreDisabled(str(", source)
        self.assertNotIn("self._failure_code = code", source)

    def test_live_journal_unlink_or_stale_replace_never_reopens_as_new(self) -> None:
        for mutation in ("unlink", "stale_replace"):
            with self.subTest(mutation=mutation), self._root() as root:
                store = self._store(root)
                journal = Path(root) / "owner_action_lifecycle.json"
                stale = journal.read_bytes()
                claim = store.reserve(
                    OwnerActionOperation.MEMORY_WRITE_LITERAL,
                    request_digest=_digest(982),
                    now=11.0,
                )
                store.mark_in_progress(claim.handle, now=12.0)
                if mutation == "unlink":
                    journal.unlink()
                else:
                    replacement = Path(root) / "stale-journal.tmp"
                    replacement.write_bytes(stale)
                    os.replace(replacement, journal)

                self.assertFalse(store.enabled)
                self.assertFalse(store._enabled)
                store.close()

                reopened = self._store(root, now=20.0)
                self.assertFalse(reopened.enabled)
                with self.assertRaises(LifecycleStoreDisabled):
                    reopened.reserve(
                        OwnerActionOperation.MEMORY_WRITE_LITERAL,
                        request_digest=_digest(982),
                        now=21.0,
                    )

    @unittest.skipIf(os.name == "nt", "Linux logical-path namespace lease")
    def test_root_rename_recreate_cannot_create_second_writer(self) -> None:
        with self._root() as base:
            root = Path(base) / "logical"
            moved = Path(base) / "moved"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            first = self._store(str(root))
            claim = first.reserve(
                OwnerActionOperation.MEMORY_WRITE_LITERAL,
                request_digest=_digest(983),
                now=11.0,
            )
            first.mark_in_progress(claim.handle, now=12.0)

            os.replace(root, moved)
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            second = self._store(str(root), now=20.0)
            self.assertFalse(second.enabled)
            self.assertEqual(second.failure_code, "lifecycle_store_locked")
            self.assertFalse(first.enabled)

            first.close()
            root.rmdir()
            os.replace(moved, root)
            reopened = self._store(str(root), now=30.0)
            existing = reopened.reserve(
                OwnerActionOperation.MEMORY_WRITE_LITERAL,
                request_digest=_digest(983),
                now=31.0,
            )
            self.assertIs(
                existing.disposition,
                LifecycleReserveDisposition.EXISTING_TERMINAL,
            )
            self.assertIs(
                existing.snapshot.result_status,
                ActionReceiptStatus.EFFECT_UNKNOWN,
            )

    def test_mutated_primary_handle_is_rejected_and_canonical_path_recovers(self) -> None:
        corruptions = {
            "fingerprint": ("_request_fingerprint", "f" * 64),
            "operation": ("operation", OwnerActionOperation.MEMORY_WRITE_LITERAL),
            "nonce": ("_store_nonce", object()),
        }
        for index, (name, (field, value)) in enumerate(corruptions.items(), 904):
            with self.subTest(name=name), self._root() as root:
                store = self._store(root)
                claim = store.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(index),
                    now=11.0,
                )
                object.__setattr__(claim.handle, field, value)
                with self.assertRaises(LifecycleTransitionRejected):
                    store.mark_in_progress(claim.handle, now=12.0)
                self.assertNotIn(root, repr(claim.handle))
                recovered = store.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(index),
                    now=12.0,
                )
                self.assertIs(recovered.snapshot.phase, LifecyclePhase.RESERVED)
                self.assertTrue(
                    store.mark_in_progress(recovered.handle, now=12.0).claimed
                )

        with self._root() as root:
            store = self._store(root)
            claim = store.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(954),
                now=11.0,
            )
            original_operation = claim.handle.operation
            object.__delattr__(claim.handle, "operation")
            with self.assertRaises(LifecycleTransitionRejected):
                store.mark_in_progress(claim.handle, now=12.0)
            object.__setattr__(claim.handle, "operation", original_operation)
            with self.assertRaises(LifecycleTransitionRejected):
                store.mark_in_progress(claim.handle, now=12.0)
            recovered = store.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(954),
                now=12.0,
            )
            self.assertTrue(store.mark_in_progress(recovered.handle, now=12.0).claimed)

    def test_close_is_idempotent_fail_closed_and_allows_clean_reopen(self) -> None:
        with self._root() as root:
            first = self._store(root)
            claim = first.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(908),
                now=11.0,
            )
            first.close()
            first.close()
            self.assertFalse(first.enabled)
            self.assertEqual(first.failure_code, "lifecycle_store_closed")
            with self.assertRaises(LifecycleStoreDisabled):
                first.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(911),
                    now=12.0,
                )
            with self.assertRaises(LifecycleStoreDisabled):
                first.mark_in_progress(claim.handle, now=12.0)
            with self.assertRaises(LifecycleStoreDisabled):
                first.mark_terminal(
                    claim.handle,
                    status=ActionReceiptStatus.STALE,
                    effect_state=ActionEffectState.NOT_STARTED,
                    attempted=False,
                    now=12.0,
                )
            with self.assertRaises(LifecycleStoreDisabled):
                first.acknowledge_delivery(claim.handle, now=12.0)
            with self.assertRaises(LifecycleStoreDisabled):
                first.snapshot_for(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(908),
                )
            with self.assertRaises(LifecycleStoreDisabled):
                first.prune(now=20.0)

            reopened = self._store(root, now=20.0)
            self.assertTrue(reopened.enabled)
            existing = reopened.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(908),
                now=21.0,
            )
            self.assertIs(
                existing.disposition,
                LifecycleReserveDisposition.EXISTING_TERMINAL,
            )
            self.assertIs(existing.snapshot.result_status, ActionReceiptStatus.STALE)

        with self._root() as root:
            with OwnerActionLifecycleStore(
                root,
                install_secret=SECRET,
                recovery_now=10.0,
            ) as managed:
                self.assertTrue(managed.enabled)
                managed.reserve(
                    OwnerActionOperation.ARTIFACT_GREP,
                    request_digest=_digest(956),
                    now=11.0,
                )
            self.assertFalse(managed.enabled)
            self.assertEqual(managed.failure_code, "lifecycle_store_closed")
            reopened = self._store(root, now=20.0)
            existing = reopened.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(956),
                now=21.0,
            )
            self.assertIs(
                existing.disposition,
                LifecycleReserveDisposition.EXISTING_TERMINAL,
            )

    def test_cross_store_handle_and_mutated_snapshot_cannot_change_store(self) -> None:
        with self._root() as first_root, self._root() as second_root:
            first = self._store(first_root)
            second = self._store(second_root)
            claim = first.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(909),
                now=11.0,
            )
            with self.assertRaises(LifecycleTransitionRejected):
                second.mark_in_progress(claim.handle, now=12.0)
            object.__setattr__(claim.snapshot, "phase", LifecyclePhase.DELIVERY_ACK)
            object.__setattr__(claim.snapshot, "attempted", 1)
            object.__setattr__(claim.snapshot, "created_at", True)
            trace = first.trace_metadata()
            trace["record_count"] = 999
            actual = first.snapshot_for(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(909),
            )
            self.assertIsNotNone(actual)
            self.assertIs(actual.phase, LifecyclePhase.RESERVED)
            self.assertIs(type(actual.attempted), bool)
            self.assertIs(type(actual.created_at), float)
            self.assertEqual(first.trace_metadata()["record_count"], 1)

    def test_concurrent_terminal_ack_and_prune_are_idempotent(self) -> None:
        with self._root() as root:
            store = self._store(root)
            claim = store.reserve(
                OwnerActionOperation.ARTIFACT_GREP,
                request_digest=_digest(912),
                now=2.0,
            )
            store.mark_in_progress(claim.handle, now=3.0)
            barrier = threading.Barrier(32)
            failures: list[BaseException] = []
            acknowledgements = []
            lock = threading.Lock()

            def finalize() -> None:
                try:
                    barrier.wait()
                    store.mark_terminal(
                        claim.handle,
                        status=ActionReceiptStatus.SUCCEEDED,
                        effect_state=ActionEffectState.NO_SIDE_EFFECT,
                        attempted=True,
                        now=4.0,
                    )
                    acknowledgement = store.acknowledge_delivery(
                        claim.handle,
                        now=5.0,
                    )
                    with lock:
                        acknowledgements.append(acknowledgement)
                except BaseException as exc:  # pragma: no cover - failure capture
                    with lock:
                        failures.append(exc)

            workers = [threading.Thread(target=finalize) for _ in range(32)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(failures, [])
            self.assertEqual(len(acknowledgements), 32)
            self.assertTrue(
                all(
                    item.phase is LifecyclePhase.DELIVERY_ACK
                    for item in acknowledgements
                )
            )

            prune_barrier = threading.Barrier(32)
            removed: list[int] = []

            def prune() -> None:
                prune_barrier.wait()
                count = store.prune(now=905.0)
                with lock:
                    removed.append(count)

            workers = [threading.Thread(target=prune) for _ in range(32)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(sum(removed), 1)
            self.assertEqual(store.trace_metadata()["record_count"], 0)

    def test_journal_symlink_is_rejected_when_platform_supports_it(self) -> None:
        with self._root() as base:
            external_root = Path(base) / "external"
            local_root = Path(base) / "local"
            external_root.mkdir(mode=0o700)
            local_root.mkdir(mode=0o700)
            if os.name != "nt":
                os.chmod(external_root, 0o700)
                os.chmod(local_root, 0o700)
            external = self._store(str(external_root))
            _close_if_supported(external)
            external_path = external_root / "owner_action_lifecycle.json"
            before = external_path.read_bytes()
            link = local_root / "owner_action_lifecycle.json"
            try:
                os.symlink(external_path, link)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            disabled = self._store(str(local_root), now=20.0)
            self.assertFalse(disabled.enabled)
            self.assertEqual(disabled.failure_code, "journal_symlink_rejected")
            self.assertEqual(external_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
