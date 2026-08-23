"""Fail-closed live runtime collector for closed trusted-owner operations.

The production audited allowlist is intentionally empty in P3-08E2B.  This module
can inspect an exact AstrBot tool manager, but it cannot turn whatever happens to be
installed into authority.  A future release must add reviewed, immutable source and
runtime fingerprints in code before a production candidate can be issued.

The private test seams at the bottom issue module-sealed manifests/proofs solely for
deterministic conformance tests.  They are omitted from ``__all__`` and cannot alter
the production manifest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import threading
import weakref
from collections.abc import Callable
from dataclasses import InitVar, dataclass, field
from types import MappingProxyType
from typing import Any

from . import owner_action_adapters as adapters
from .contracts import DecisionBinding, OwnerActionOperation


class RuntimeCollectorRejected(ValueError):
    """Closed rejection without source paths, raw identifiers, or tool reprs."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_file_path(path: str) -> str:
    normalized = os.path.normcase(os.path.abspath(path)).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _fingerprint_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_qualname(value: object) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _canonical_schema_digest(value: object) -> str:
    schema = getattr(value, "parameters", None)
    if type(schema) is not dict:
        raise RuntimeCollectorRejected("runtime_schema_unavailable")
    try:
        return _digest_json(schema)
    except (TypeError, ValueError):
        raise RuntimeCollectorRejected("runtime_schema_not_canonical") from None


@dataclass(frozen=True, slots=True, repr=False)
class _AuditedRuntimeSpec:
    operation: OwnerActionOperation
    implementation_version: str
    source_qualname: str
    schema_digest: str
    source_file_digest: str
    source_fingerprint: str
    local_function_tool: bool = True
    background_disabled: bool = True
    direct_send_blocked: bool = True
    narrow_context_verified: bool = True
    return_contract_verified: bool = True
    permission_wrapper_required: bool = False


# Deliberately empty: live 4.27.2 source fingerprints drift from the reviewed tag,
# FileRead/Grep lack the required source-side behavior proof, LivingMemory is in
# legacy/member mode, and shell is permanently hard-disabled.
PRODUCTION_AUDITED_OPERATIONS = MappingProxyType({})
PRODUCTION_AUDITED_OPERATION_COUNT = len(PRODUCTION_AUDITED_OPERATIONS)
_PRODUCTION_IMPLEMENTATION_VERSIONS = MappingProxyType({})


def _production_implementation_version(operation: OwnerActionOperation) -> str:
    value = _PRODUCTION_IMPLEMENTATION_VERSIONS.get(operation)
    if type(value) is not str or not value:
        raise RuntimeCollectorRejected("runtime_version_not_audited")
    return value


def _inspect_source_file(tool: object) -> str:
    try:
        path = inspect.getsourcefile(type(tool))
    except (TypeError, OSError):
        path = None
    if type(path) is not str or not path or not os.path.isfile(path):
        raise RuntimeCollectorRejected("runtime_source_file_unavailable")
    return os.path.realpath(path)


@dataclass(frozen=True, slots=True, repr=False)
class _CandidateMaterial:
    collector: "RuntimeConformanceCollector"
    tool: object = field(repr=False)
    source_tool: object = field(repr=False)
    spec: _AuditedRuntimeSpec = field(repr=False)
    tool_snapshot: tuple[object, ...] = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _RuntimeEvidenceMaterial:
    collector: "RuntimeConformanceCollector" = field(repr=False)
    candidate: "RuntimeToolCandidate" = field(repr=False)
    tool: object = field(repr=False)
    source_tool: object = field(repr=False)
    tool_snapshot: tuple[object, ...] = field(repr=False)
    binding: DecisionBinding = field(repr=False)
    operation: OwnerActionOperation
    adapter_id: str
    adapter_version: str
    exact_tool_name: str
    plugin_id: str
    implementation_version: str
    source_qualname: str
    interface_version: str
    schema_digest: str = field(repr=False)
    source_file_digest: str = field(repr=False)
    source_fingerprint: str = field(repr=False)
    collector_epoch: int
    candidate_digest: str = field(repr=False)
    local_function_tool: bool
    background_disabled: bool
    direct_send_blocked: bool
    narrow_context_verified: bool
    return_contract_verified: bool
    permission_wrapper_preserved: bool
    artifact_path: adapters.ArtifactPathConformance | None = field(
        default=None, repr=False
    )
    grep_runtime: adapters.GrepRuntimeConformance | None = field(
        default=None, repr=False
    )
    memory_scope: adapters.MemoryScopeConformance | None = field(
        default=None, repr=False
    )


_EVIDENCE_MATERIAL_LOCK = threading.RLock()
_EVIDENCE_RUNTIME_MATERIALS: weakref.WeakKeyDictionary[
    adapters.RuntimeConformanceEvidence, _RuntimeEvidenceMaterial
] = weakref.WeakKeyDictionary()
_OPENED_RUNTIME_EVIDENCE: weakref.WeakSet[adapters.RuntimeConformanceEvidence] = (
    weakref.WeakSet()
)


_CANDIDATE_LOCK = threading.RLock()
_PENDING_CANDIDATE_SEALS: set[object] = set()
_CANONICAL_CANDIDATES: weakref.WeakValueDictionary[int, "RuntimeToolCandidate"] = (
    weakref.WeakValueDictionary()
)
_CANDIDATE_MATERIALS: weakref.WeakKeyDictionary[
    "RuntimeToolCandidate", _CandidateMaterial
] = weakref.WeakKeyDictionary()
_CANDIDATE_SNAPSHOTS: weakref.WeakKeyDictionary[
    "RuntimeToolCandidate", tuple[object, ...]
] = weakref.WeakKeyDictionary()
_CONSUMED_CANDIDATES: weakref.WeakSet["RuntimeToolCandidate"] = weakref.WeakSet()
_CANDIDATE_MARKER = object()
_COLLECTOR_EPOCH_LOCK = threading.Lock()
_COLLECTOR_EPOCH = 0


def _next_collector_epoch() -> int:
    global _COLLECTOR_EPOCH
    with _COLLECTOR_EPOCH_LOCK:
        _COLLECTOR_EPOCH += 1
        return _COLLECTOR_EPOCH


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class RuntimeToolCandidate:
    """Opaque exact-tool identity; the actual tool object stays module-private."""

    binding: DecisionBinding = field(repr=False)
    operation: OwnerActionOperation
    adapter_id: str
    exact_tool_name: str
    plugin_id: str
    implementation_version: str
    source_qualname: str
    schema_digest: str = field(repr=False)
    source_file_digest: str = field(repr=False)
    source_fingerprint: str = field(repr=False)
    collector_epoch: int
    candidate_digest: str = field(repr=False)
    permission_wrapper_preserved: bool
    _issuer_seal: InitVar[object | None] = None
    _canonical_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _issuer_seal: object | None) -> None:
        with _CANDIDATE_LOCK:
            if _issuer_seal is None or _issuer_seal not in _PENDING_CANDIDATE_SEALS:
                raise RuntimeCollectorRejected("candidate_issuer_seal_invalid")
            _PENDING_CANDIDATE_SEALS.remove(_issuer_seal)
        object.__setattr__(self, "_canonical_marker", _CANDIDATE_MARKER)
        if type(self.binding) is not DecisionBinding:
            raise RuntimeCollectorRejected("runtime_binding_invalid")
        if type(self.operation) is not OwnerActionOperation:
            raise RuntimeCollectorRejected("runtime_operation_invalid")

    @property
    def is_canonical(self) -> bool:
        with _CANDIDATE_LOCK:
            expected = _CANDIDATE_SNAPSHOTS.get(self)
            return (
                self._canonical_marker is _CANDIDATE_MARKER
                and _CANONICAL_CANDIDATES.get(id(self)) is self
                and expected is not None
                and expected == _candidate_state(self)
                and self in _CANDIDATE_MATERIALS
            )

    def trace_metadata(self) -> dict[str, str | int | bool]:
        if not self.is_canonical:
            raise RuntimeCollectorRejected("runtime_candidate_not_canonical")
        return {
            "schema_version": 1,
            "operation": self.operation.value,
            "adapter_id": self.adapter_id,
            "exact_tool_name": self.exact_tool_name,
            "collector_epoch": self.collector_epoch,
            "permission_wrapper_preserved": self.permission_wrapper_preserved,
            "has_schema_digest": True,
            "has_source_file_digest": True,
            "has_source_fingerprint": True,
            "has_candidate_digest": True,
            "canonical": self.is_canonical,
        }

    def __repr__(self) -> str:
        if not self.is_canonical:
            return "RuntimeToolCandidate(canonical=False, details_hidden=True)"
        return (
            "RuntimeToolCandidate("
            f"operation={self.operation.value!r}, adapter_id={self.adapter_id!r}, "
            f"exact_tool_name={self.exact_tool_name!r}, "
            f"collector_epoch={self.collector_epoch}, exact_object_hidden=True, "
            f"canonical={self.is_canonical})"
        )


def _candidate_state(candidate: RuntimeToolCandidate) -> tuple[object, ...]:
    return (
        candidate.binding,
        candidate.operation,
        candidate.adapter_id,
        candidate.exact_tool_name,
        candidate.plugin_id,
        candidate.implementation_version,
        candidate.source_qualname,
        candidate.schema_digest,
        candidate.source_file_digest,
        candidate.source_fingerprint,
        candidate.collector_epoch,
        candidate.candidate_digest,
        candidate.permission_wrapper_preserved,
    )


def _runtime_tool_snapshot(
    tool: object,
    source_tool: object,
    *,
    permission_wrapper_preserved: bool,
) -> tuple[object, ...]:
    return (
        id(tool),
        id(source_tool),
        getattr(tool, "name", None),
        getattr(tool, "active", None),
        getattr(tool, "is_background_task", None),
        getattr(tool, "handler_module_path", None),
        getattr(tool, "handler", None) is None,
        _source_qualname(tool),
        _source_qualname(source_tool),
        _canonical_schema_digest(source_tool),
        permission_wrapper_preserved,
        (
            getattr(tool, "_wrapped", None) is source_tool
            if permission_wrapper_preserved
            else tool is source_tool
        ),
    )


_PROOF_LOCK = threading.RLock()
_PENDING_PROOF_SEALS: set[object] = set()
_CANONICAL_PROOFS: weakref.WeakValueDictionary[int, "RuntimeOperationProof"] = (
    weakref.WeakValueDictionary()
)
_PROOF_SNAPSHOTS: weakref.WeakKeyDictionary[
    "RuntimeOperationProof", tuple[object, ...]
] = weakref.WeakKeyDictionary()
_CONSUMED_PROOFS: weakref.WeakSet["RuntimeOperationProof"] = weakref.WeakSet()
_PROOF_MARKER = object()


@dataclass(frozen=True, slots=True, repr=False, eq=False, weakref_slot=True)
class RuntimeOperationProof:
    """Module-sealed operation probe output; public booleans cannot create it."""

    artifact_path: adapters.ArtifactPathConformance | None = field(
        default=None, repr=False
    )
    grep_runtime: adapters.GrepRuntimeConformance | None = field(
        default=None, repr=False
    )
    memory_scope: adapters.MemoryScopeConformance | None = field(
        default=None, repr=False
    )
    _issuer_seal: InitVar[object | None] = None
    _canonical_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _issuer_seal: object | None) -> None:
        with _PROOF_LOCK:
            if _issuer_seal is None or _issuer_seal not in _PENDING_PROOF_SEALS:
                raise RuntimeCollectorRejected("operation_proof_issuer_seal_invalid")
            _PENDING_PROOF_SEALS.remove(_issuer_seal)
        object.__setattr__(self, "_canonical_marker", _PROOF_MARKER)
        shapes = (
            (self.artifact_path, adapters.ArtifactPathConformance),
            (self.grep_runtime, adapters.GrepRuntimeConformance),
            (self.memory_scope, adapters.MemoryScopeConformance),
        )
        if any(
            value is not None and type(value) is not expected
            for value, expected in shapes
        ):
            raise RuntimeCollectorRejected("operation_proof_shape_invalid")

    @property
    def is_canonical(self) -> bool:
        with _PROOF_LOCK:
            expected = _PROOF_SNAPSHOTS.get(self)
            return (
                self._canonical_marker is _PROOF_MARKER
                and _CANONICAL_PROOFS.get(id(self)) is self
                and expected is not None
                and expected == _proof_state(self)
            )

    def trace_metadata(self) -> dict[str, int | bool]:
        if not self.is_canonical:
            raise RuntimeCollectorRejected("operation_proof_not_canonical")
        return {
            "schema_version": 1,
            "has_artifact_path": self.artifact_path is not None,
            "has_grep_runtime": self.grep_runtime is not None,
            "has_memory_scope": self.memory_scope is not None,
            "canonical": True,
        }

    def __repr__(self) -> str:
        if not self.is_canonical:
            return "RuntimeOperationProof(canonical=False, details_hidden=True)"
        return (
            "RuntimeOperationProof("
            f"has_path={self.artifact_path is not None}, "
            f"has_grep={self.grep_runtime is not None}, "
            f"has_scope={self.memory_scope is not None}, "
            f"canonical={self.is_canonical}, details_hidden=True)"
        )


def _shape_state(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return tuple(
            (item.name, _shape_state(getattr(value, item.name)))
            for item in dataclasses.fields(value)
        )
    if hasattr(value, "value") and type(getattr(value, "value", None)) is str:
        return (type(value), value.value)
    if isinstance(value, (list, tuple)):
        return tuple(_shape_state(item) for item in value)
    return value


def _proof_state(proof: RuntimeOperationProof) -> tuple[object, ...]:
    return (
        _shape_state(proof.artifact_path),
        _shape_state(proof.grep_runtime),
        _shape_state(proof.memory_scope),
    )


def _issue_operation_proof(
    *,
    artifact_path: adapters.ArtifactPathConformance | None = None,
    grep_runtime: adapters.GrepRuntimeConformance | None = None,
    memory_scope: adapters.MemoryScopeConformance | None = None,
) -> RuntimeOperationProof:
    seal = object()
    with _PROOF_LOCK:
        _PENDING_PROOF_SEALS.add(seal)
    try:
        proof = RuntimeOperationProof(
            artifact_path=artifact_path,
            grep_runtime=grep_runtime,
            memory_scope=memory_scope,
            _issuer_seal=seal,
        )
    finally:
        with _PROOF_LOCK:
            _PENDING_PROOF_SEALS.discard(seal)
    with _PROOF_LOCK:
        _CANONICAL_PROOFS[id(proof)] = proof
        _PROOF_SNAPSHOTS[proof] = _proof_state(proof)
    return proof


class _PermissionEvent:
    __slots__ = ("_admin",)

    def __init__(self, admin: bool) -> None:
        self._admin = admin

    def is_admin(self) -> bool:
        return self._admin

    def get_sender_id(self) -> str:
        return "redacted"


class _PermissionContext:
    __slots__ = ("context",)

    def __init__(self, admin: bool) -> None:
        event = _PermissionEvent(admin)
        self.context = type("_PermissionInner", (), {"event": event})()


@dataclass(frozen=True, slots=True, repr=False)
class _CollectorProfile:
    manager: object = field(repr=False)
    collector_epoch: int
    specs: object = field(repr=False)
    implementation_version: Callable[[OwnerActionOperation], str] | None = field(
        default=None, repr=False
    )
    source_file: Callable[[object], str] | None = field(default=None, repr=False)
    memory_scope_probe: Callable[[], str] | None = field(default=None, repr=False)


_COLLECTOR_PROFILE_LOCK = threading.RLock()
_COLLECTOR_PROFILES: weakref.WeakKeyDictionary[
    "RuntimeConformanceCollector", _CollectorProfile
] = weakref.WeakKeyDictionary()


class RuntimeConformanceCollector:
    """Collect exact tool candidates from one exact manager and code audit manifest."""

    def __init__(self, manager: object) -> None:
        if isinstance(manager, dict) or manager is None:
            raise RuntimeCollectorRejected("runtime_manager_invalid")
        profile = _CollectorProfile(
            manager=manager,
            collector_epoch=_next_collector_epoch(),
            specs=PRODUCTION_AUDITED_OPERATIONS,
            implementation_version=_production_implementation_version,
            source_file=_inspect_source_file,
        )
        with _COLLECTOR_PROFILE_LOCK:
            _COLLECTOR_PROFILES[self] = profile

    def _profile(self) -> _CollectorProfile:
        with _COLLECTOR_PROFILE_LOCK:
            profile = _COLLECTOR_PROFILES.get(self)
        if profile is None:
            raise RuntimeCollectorRejected("runtime_collector_not_canonical")
        return profile

    def collect(
        self,
        binding: DecisionBinding,
        operation: OwnerActionOperation,
    ) -> RuntimeToolCandidate:
        if type(binding) is not DecisionBinding:
            raise RuntimeCollectorRejected("runtime_binding_invalid")
        if type(operation) is not OwnerActionOperation:
            raise RuntimeCollectorRejected("runtime_operation_invalid")
        if operation is OwnerActionOperation.SANDBOX_SHELL_ONCE:
            raise RuntimeCollectorRejected("shell_runtime_hard_disabled")
        profile = self._profile()
        spec = profile.specs.get(operation)
        if spec is None:
            raise RuntimeCollectorRejected("runtime_operation_not_audited")
        descriptor = adapters.OWNER_ACTION_ADAPTER_REGISTRY[operation]
        if descriptor.hard_disabled:
            raise RuntimeCollectorRejected("runtime_adapter_hard_disabled")

        if operation in {
            OwnerActionOperation.ARTIFACT_READ_EXACT,
            OwnerActionOperation.ARTIFACT_GREP,
        }:
            tool, source_tool, permission_wrapper = self._collect_builtin(
                profile,
                descriptor,
            )
        elif operation is OwnerActionOperation.MEMORY_WRITE_LITERAL:
            tool, source_tool, permission_wrapper = self._collect_memory(
                profile,
                descriptor,
            )
        else:  # pragma: no cover - shell and enum closure handled above.
            raise RuntimeCollectorRejected("runtime_operation_not_audited")

        self._assert_source_contract(
            operation=operation,
            source_tool=source_tool,
            spec=spec,
            profile=profile,
        )
        return self._issue_candidate(
            binding=binding,
            descriptor=descriptor,
            tool=tool,
            source_tool=source_tool,
            spec=spec,
            permission_wrapper_preserved=permission_wrapper,
        )

    def _collect_builtin(
        self,
        profile: _CollectorProfile,
        descriptor: adapters.AdapterDescriptor,
    ) -> tuple[object, object, bool]:
        getter = getattr(profile.manager, "get_builtin_tool", None)
        if not callable(getter):
            raise RuntimeCollectorRejected("builtin_manager_api_missing")
        try:
            first = getter(descriptor.exact_tool_name)
            second = getter(descriptor.exact_tool_name)
        except Exception:
            raise RuntimeCollectorRejected("builtin_tool_unavailable") from None
        if first is not second:
            raise RuntimeCollectorRejected("builtin_tool_identity_unstable")
        conflicts = getattr(profile.manager, "func_list", None)
        if type(conflicts) not in {list, tuple}:
            raise RuntimeCollectorRejected("runtime_conflict_inventory_missing")
        if any(
            candidate is not first
            and type(getattr(candidate, "name", None)) is str
            and candidate.name == descriptor.exact_tool_name
            for candidate in conflicts
        ):
            raise RuntimeCollectorRejected("runtime_tool_name_conflict")
        self._assert_exact_tool_common(first, descriptor.exact_tool_name)
        return first, first, False

    def _collect_memory(
        self,
        profile: _CollectorProfile,
        descriptor: adapters.AdapterDescriptor,
    ) -> tuple[object, object, bool]:
        raw_tools = getattr(profile.manager, "func_list", None)
        if type(raw_tools) not in {list, tuple}:
            raise RuntimeCollectorRejected("runtime_plugin_inventory_missing")
        matches = tuple(
            tool
            for tool in raw_tools
            if type(getattr(tool, "name", None)) is str
            and tool.name == descriptor.exact_tool_name
        )
        if len(matches) != 1:
            raise RuntimeCollectorRejected("memory_raw_tool_not_unique")
        raw = matches[0]
        self._assert_exact_tool_common(raw, descriptor.exact_tool_name)

        full_getter = getattr(profile.manager, "get_full_tool_set", None)
        if not callable(full_getter):
            raise RuntimeCollectorRejected("memory_full_tool_set_missing")
        try:
            tool_set = full_getter()
            wrapped = tool_set.get_tool(descriptor.exact_tool_name)
        except Exception:
            raise RuntimeCollectorRejected("memory_full_tool_set_unavailable") from None
        if wrapped is None or _source_qualname(wrapped) != (
            "astrbot.core.provider.func_tool_manager._PermissionGuardedTool"
        ):
            raise RuntimeCollectorRejected("memory_permission_wrapper_missing")
        if getattr(wrapped, "_wrapped", None) is not raw:
            raise RuntimeCollectorRejected("memory_wrapped_identity_mismatch")
        self._assert_exact_tool_common(
            wrapped,
            descriptor.exact_tool_name,
            expected_handler_module=type(raw).__module__,
        )

        permission = getattr(profile.manager, "_check_tool_permission", None)
        if not callable(permission):
            raise RuntimeCollectorRejected("memory_permission_probe_missing")
        try:
            guest_result = permission(
                descriptor.exact_tool_name,
                _PermissionContext(False),
            )
            admin_result = permission(
                descriptor.exact_tool_name,
                _PermissionContext(True),
            )
        except Exception:
            raise RuntimeCollectorRejected("memory_permission_probe_failed") from None
        if type(guest_result) is not str or not guest_result or admin_result is not None:
            raise RuntimeCollectorRejected("memory_dashboard_permission_not_admin")
        if profile.memory_scope_probe is None:
            raise RuntimeCollectorRejected("memory_private_scope_probe_missing")
        try:
            scope_mode = profile.memory_scope_probe()
        except Exception:
            raise RuntimeCollectorRejected("memory_private_scope_probe_failed") from None
        if type(scope_mode) is not str or scope_mode != "private_user":
            raise RuntimeCollectorRejected("memory_private_scope_required")
        return wrapped, raw, True

    @staticmethod
    def _assert_exact_tool_common(
        tool: object,
        exact_name: str,
        *,
        expected_handler_module: str | None = None,
    ) -> None:
        if type(getattr(tool, "name", None)) is not str or tool.name != exact_name:
            raise RuntimeCollectorRejected("runtime_tool_name_drift")
        if getattr(tool, "active", None) is not True:
            raise RuntimeCollectorRejected("runtime_tool_inactive")
        if getattr(tool, "is_background_task", None) is not False:
            raise RuntimeCollectorRejected("runtime_background_not_disabled")
        expected_module = expected_handler_module or type(tool).__module__
        if getattr(tool, "handler_module_path", None) != expected_module:
            raise RuntimeCollectorRejected("runtime_handler_source_drift")
        if getattr(tool, "handler", None) is not None:
            raise RuntimeCollectorRejected("runtime_handler_bypass_present")

    def _assert_source_contract(
        self,
        *,
        operation: OwnerActionOperation,
        source_tool: object,
        spec: _AuditedRuntimeSpec,
        profile: _CollectorProfile,
    ) -> None:
        qualname = _source_qualname(source_tool)
        schema_digest = _canonical_schema_digest(source_tool)
        if profile.implementation_version is None or profile.source_file is None:
            raise RuntimeCollectorRejected("runtime_audit_resolver_missing")
        try:
            implementation_version = profile.implementation_version(operation)
            source_file = profile.source_file(source_tool)
            source_file_digest = _digest_file_path(source_file)
            source_fingerprint = _fingerprint_file(source_file)
        except Exception:
            raise RuntimeCollectorRejected("runtime_source_unavailable") from None
        actual = (
            implementation_version,
            qualname,
            schema_digest,
            source_file_digest,
            source_fingerprint,
        )
        expected = (
            spec.implementation_version,
            spec.source_qualname,
            spec.schema_digest,
            spec.source_file_digest,
            spec.source_fingerprint,
        )
        if actual != expected:
            raise RuntimeCollectorRejected("runtime_source_contract_drift")

    def _issue_candidate(
        self,
        *,
        binding: DecisionBinding,
        descriptor: adapters.AdapterDescriptor,
        tool: object,
        source_tool: object,
        spec: _AuditedRuntimeSpec,
        permission_wrapper_preserved: bool,
    ) -> RuntimeToolCandidate:
        payload = {
            "binding": dataclasses.asdict(binding),
            "operation": descriptor.operation.value,
            "adapter": descriptor.adapter_id,
            "tool": descriptor.exact_tool_name,
            "plugin": descriptor.plugin_id,
            "implementation": spec.implementation_version,
            "source": spec.source_qualname,
            "schema": spec.schema_digest,
            "source_file": spec.source_file_digest,
            "source_fingerprint": spec.source_fingerprint,
            "collector_epoch": self._profile().collector_epoch,
            "permission_wrapper": permission_wrapper_preserved,
        }
        candidate_digest = _digest_json(payload)
        seal = object()
        with _CANDIDATE_LOCK:
            _PENDING_CANDIDATE_SEALS.add(seal)
        try:
            candidate = RuntimeToolCandidate(
                binding=binding,
                operation=descriptor.operation,
                adapter_id=descriptor.adapter_id,
                exact_tool_name=descriptor.exact_tool_name,
                plugin_id=descriptor.plugin_id,
                implementation_version=spec.implementation_version,
                source_qualname=spec.source_qualname,
                schema_digest=spec.schema_digest,
                source_file_digest=spec.source_file_digest,
                source_fingerprint=spec.source_fingerprint,
                collector_epoch=self._profile().collector_epoch,
                candidate_digest=candidate_digest,
                permission_wrapper_preserved=permission_wrapper_preserved,
                _issuer_seal=seal,
            )
        finally:
            with _CANDIDATE_LOCK:
                _PENDING_CANDIDATE_SEALS.discard(seal)
        material = _CandidateMaterial(
            collector=self,
            tool=tool,
            source_tool=source_tool,
            spec=spec,
            tool_snapshot=_runtime_tool_snapshot(
                tool,
                source_tool,
                permission_wrapper_preserved=permission_wrapper_preserved,
            ),
        )
        with _CANDIDATE_LOCK:
            _CANONICAL_CANDIDATES[id(candidate)] = candidate
            _CANDIDATE_MATERIALS[candidate] = material
            _CANDIDATE_SNAPSHOTS[candidate] = _candidate_state(candidate)
        return candidate

    def issue_evidence(
        self,
        candidate: RuntimeToolCandidate,
        proof: RuntimeOperationProof,
    ) -> adapters.RuntimeConformanceEvidence:
        return adapters._issue_runtime_conformance_evidence(self, candidate, proof)

    @staticmethod
    def _assert_proof_shape(
        operation: OwnerActionOperation,
        proof: RuntimeOperationProof,
    ) -> None:
        shapes = (
            proof.artifact_path is not None,
            proof.grep_runtime is not None,
            proof.memory_scope is not None,
        )
        if operation is OwnerActionOperation.ARTIFACT_READ_EXACT:
            if shapes not in {(False, False, False), (True, False, False)}:
                raise RuntimeCollectorRejected("operation_proof_shape_mismatch")
        elif operation is OwnerActionOperation.ARTIFACT_GREP:
            if shapes != (True, True, False):
                raise RuntimeCollectorRejected("operation_proof_shape_mismatch")
        elif operation is OwnerActionOperation.MEMORY_WRITE_LITERAL:
            if shapes != (False, False, True):
                raise RuntimeCollectorRejected("operation_proof_shape_mismatch")
        else:
            raise RuntimeCollectorRejected("shell_runtime_hard_disabled")


def _claim_runtime_evidence_material(
    collector: object,
    candidate: object,
    proof: object,
) -> _RuntimeEvidenceMaterial:
    """Atomically claim an exact candidate+proof pair using a fixed lock order."""

    if type(collector) is not RuntimeConformanceCollector:
        raise RuntimeCollectorRejected("runtime_collector_not_canonical")
    if type(candidate) is not RuntimeToolCandidate:
        raise RuntimeCollectorRejected("runtime_candidate_not_canonical")
    if type(proof) is not RuntimeOperationProof:
        raise RuntimeCollectorRejected("operation_proof_not_canonical")
    # Fixed lock order: candidate then proof.  All integrity checks and both replay
    # marks happen in the same critical section, so no half-consumed pair exists.
    with _CANDIDATE_LOCK:
        with _PROOF_LOCK:
            if (
                candidate._canonical_marker is not _CANDIDATE_MARKER
                or _CANONICAL_CANDIDATES.get(id(candidate)) is not candidate
                or _CANDIDATE_SNAPSHOTS.get(candidate) != _candidate_state(candidate)
            ):
                raise RuntimeCollectorRejected("runtime_candidate_not_canonical")
            candidate_material = _CANDIDATE_MATERIALS.get(candidate)
            if candidate_material is None or candidate_material.collector is not collector:
                raise RuntimeCollectorRejected("candidate_collector_mismatch")
            if candidate in _CONSUMED_CANDIDATES:
                raise RuntimeCollectorRejected("runtime_candidate_replayed")
            if (
                proof._canonical_marker is not _PROOF_MARKER
                or _CANONICAL_PROOFS.get(id(proof)) is not proof
                or _PROOF_SNAPSHOTS.get(proof) != _proof_state(proof)
            ):
                raise RuntimeCollectorRejected("operation_proof_not_canonical")
            if proof in _CONSUMED_PROOFS:
                raise RuntimeCollectorRejected("operation_proof_replayed")
            RuntimeConformanceCollector._assert_proof_shape(candidate.operation, proof)
            descriptor = adapters.OWNER_ACTION_ADAPTER_REGISTRY[candidate.operation]
            material = _RuntimeEvidenceMaterial(
                collector=collector,
                candidate=candidate,
                tool=candidate_material.tool,
                source_tool=candidate_material.source_tool,
                tool_snapshot=candidate_material.tool_snapshot,
                binding=candidate.binding,
                operation=candidate.operation,
                adapter_id=descriptor.adapter_id,
                adapter_version=descriptor.adapter_version,
                exact_tool_name=descriptor.exact_tool_name,
                plugin_id=descriptor.plugin_id,
                implementation_version=candidate.implementation_version,
                source_qualname=candidate.source_qualname,
                interface_version=descriptor.interface_version,
                schema_digest=candidate.schema_digest,
                source_file_digest=candidate.source_file_digest,
                source_fingerprint=candidate.source_fingerprint,
                collector_epoch=candidate.collector_epoch,
                candidate_digest=candidate.candidate_digest,
                local_function_tool=candidate_material.spec.local_function_tool,
                background_disabled=candidate_material.spec.background_disabled,
                direct_send_blocked=candidate_material.spec.direct_send_blocked,
                narrow_context_verified=candidate_material.spec.narrow_context_verified,
                return_contract_verified=candidate_material.spec.return_contract_verified,
                permission_wrapper_preserved=candidate.permission_wrapper_preserved,
                artifact_path=proof.artifact_path,
                grep_runtime=proof.grep_runtime,
                memory_scope=proof.memory_scope,
            )
            _CONSUMED_CANDIDATES.add(candidate)
            _CONSUMED_PROOFS.add(proof)
            return material


def _bind_runtime_evidence_material(
    evidence: adapters.RuntimeConformanceEvidence,
    material: _RuntimeEvidenceMaterial,
) -> None:
    """Bind the freshly issued evidence to its retained exact tool identity."""

    if type(evidence) is not adapters.RuntimeConformanceEvidence or not evidence.is_canonical:
        raise RuntimeCollectorRejected("runtime_evidence_not_canonical")
    if type(material) is not _RuntimeEvidenceMaterial:
        raise RuntimeCollectorRejected("runtime_evidence_material_invalid")
    if evidence.binding != material.binding or evidence.operation is not material.operation:
        raise RuntimeCollectorRejected("runtime_evidence_material_mismatch")
    with _EVIDENCE_MATERIAL_LOCK:
        if evidence in _EVIDENCE_RUNTIME_MATERIALS:
            raise RuntimeCollectorRejected("runtime_evidence_material_duplicate")
        _EVIDENCE_RUNTIME_MATERIALS[evidence] = material


def _open_runtime_evidence(
    evidence: adapters.RuntimeConformanceEvidence,
    *,
    collector: RuntimeConformanceCollector,
    binding: DecisionBinding,
    operation: OwnerActionOperation,
) -> _RuntimeEvidenceMaterial:
    """One-shot executor friend opener for the retained exact runtime object."""

    if type(evidence) is not adapters.RuntimeConformanceEvidence or not evidence.is_canonical:
        raise RuntimeCollectorRejected("runtime_evidence_not_canonical")
    if type(collector) is not RuntimeConformanceCollector:
        raise RuntimeCollectorRejected("runtime_collector_not_canonical")
    if type(binding) is not DecisionBinding or type(operation) is not OwnerActionOperation:
        raise RuntimeCollectorRejected("runtime_open_binding_invalid")
    with _EVIDENCE_MATERIAL_LOCK:
        material = _EVIDENCE_RUNTIME_MATERIALS.get(evidence)
        if material is None:
            raise RuntimeCollectorRejected("runtime_evidence_material_missing")
        if material.collector is not collector:
            raise RuntimeCollectorRejected("runtime_evidence_collector_mismatch")
        if material.binding != binding or material.operation is not operation:
            raise RuntimeCollectorRejected("runtime_evidence_open_mismatch")
        if evidence in _OPENED_RUNTIME_EVIDENCE:
            raise RuntimeCollectorRejected("runtime_evidence_open_replayed")
        candidate = material.candidate
        if not candidate.is_canonical:
            raise RuntimeCollectorRejected("runtime_candidate_not_canonical")
        with _CANDIDATE_LOCK:
            candidate_material = _CANDIDATE_MATERIALS.get(candidate)
        if (
            candidate_material is None
            or candidate_material.collector is not collector
            or candidate_material.tool is not material.tool
            or candidate_material.source_tool is not material.source_tool
            or candidate_material.tool_snapshot != material.tool_snapshot
        ):
            raise RuntimeCollectorRejected("runtime_exact_object_lineage_mismatch")
        current_snapshot = _runtime_tool_snapshot(
            material.tool,
            material.source_tool,
            permission_wrapper_preserved=evidence.permission_wrapper_preserved,
        )
        if current_snapshot != material.tool_snapshot:
            raise RuntimeCollectorRejected("runtime_exact_object_drift")
        profile = collector._profile()
        if profile.source_file is None:
            raise RuntimeCollectorRejected("runtime_source_file_unavailable")
        try:
            source_file = profile.source_file(material.source_tool)
            source_file_digest = _digest_file_path(source_file)
            source_fingerprint = _fingerprint_file(source_file)
        except Exception:
            raise RuntimeCollectorRejected("runtime_source_unavailable") from None
        if (
            source_file_digest != evidence.source_file_digest
            or source_fingerprint != evidence.source_fingerprint
        ):
            raise RuntimeCollectorRejected("runtime_source_contract_drift")
        _OPENED_RUNTIME_EVIDENCE.add(evidence)
        return material


def inspect_public_collector_signature() -> tuple[str, ...]:
    """Small stable introspection helper used by the contract tests."""

    return tuple(inspect.signature(RuntimeConformanceCollector.collect).parameters)[1:]


def _build_test_runtime_collector(
    manager: object,
    *,
    operation: OwnerActionOperation,
    implementation_version: str,
    memory_scope_probe: Callable[[], str] | None = None,
) -> RuntimeConformanceCollector:
    """Module-private deterministic manifest issuer; never used by production."""

    if operation is OwnerActionOperation.SANDBOX_SHELL_ONCE:
        raise RuntimeCollectorRejected("shell_runtime_hard_disabled")
    descriptor = adapters.OWNER_ACTION_ADAPTER_REGISTRY[operation]
    if operation in {
        OwnerActionOperation.ARTIFACT_READ_EXACT,
        OwnerActionOperation.ARTIFACT_GREP,
    }:
        source_tool = getattr(manager, "tool", None)
    else:
        source_tool = getattr(manager, "raw", None)
    if source_tool is None:
        raise RuntimeCollectorRejected("test_source_tool_missing")
    source_file = __file__.replace("core\\owner_action_runtime_collector.py", "tests\\test_owner_action_runtime_collector.py")
    if source_file == __file__:
        source_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "tests",
            "test_owner_action_runtime_collector.py",
        )
    spec = _AuditedRuntimeSpec(
        operation=operation,
        implementation_version=implementation_version,
        source_qualname=_source_qualname(source_tool),
        schema_digest=_canonical_schema_digest(source_tool),
        source_file_digest=_digest_file_path(source_file),
        source_fingerprint=_fingerprint_file(source_file),
        permission_wrapper_required=(
            operation is OwnerActionOperation.MEMORY_WRITE_LITERAL
        ),
    )
    # The fixture still has to represent the exact adapter source and schema.  It
    # cannot use this seam to sign an arbitrary operation or schema.
    if (
        spec.source_qualname != descriptor.source_qualname
        or spec.schema_digest != descriptor.schema_digest
        or spec.implementation_version != descriptor.implementation_version
    ):
        raise RuntimeCollectorRejected("test_manifest_descriptor_mismatch")
    runtime = RuntimeConformanceCollector(manager)
    production_profile = runtime._profile()
    test_profile = _CollectorProfile(
        manager=manager,
        collector_epoch=production_profile.collector_epoch,
        specs=MappingProxyType({operation: spec}),
        implementation_version=lambda candidate_operation: (
            implementation_version if candidate_operation is operation else ""
        ),
        source_file=lambda _tool: source_file,
        memory_scope_probe=memory_scope_probe,
    )
    with _COLLECTOR_PROFILE_LOCK:
        _COLLECTOR_PROFILES[runtime] = test_profile
    return runtime


def _issue_test_operation_proof(
    *,
    artifact_path: adapters.ArtifactPathConformance | None = None,
    grep_runtime: adapters.GrepRuntimeConformance | None = None,
    memory_scope: adapters.MemoryScopeConformance | None = None,
) -> RuntimeOperationProof:
    return _issue_operation_proof(
        artifact_path=artifact_path,
        grep_runtime=grep_runtime,
        memory_scope=memory_scope,
    )


__all__ = [
    "PRODUCTION_AUDITED_OPERATION_COUNT",
    "PRODUCTION_AUDITED_OPERATIONS",
    "RuntimeCollectorRejected",
    "RuntimeConformanceCollector",
    "RuntimeOperationProof",
    "RuntimeToolCandidate",
    "inspect_public_collector_signature",
]
