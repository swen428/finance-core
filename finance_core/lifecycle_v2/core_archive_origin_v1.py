"""Signed cut provenance for Core-only archives."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from finance_core.instance_admission import TrustedInstanceConfig
from finance_core.instance_admission.authority import (
    _MANUAL_RESTORE_FORMAT,
    _record,
    _recovery_cut_ref,
    _recovery_hash,
    _state,
)
from finance_core.instance_admission.sessions import (
    _require_runtime_scope,
    _witness_for,
    lifecycle_read_operation,
)

from .contracts import LifecycleError, RuntimeBinding, SnapshotCut, canonical, cut_ref, digest
from .historical_evidence_v1 import (
    _protected_history,
    historical_generations_sha256_v1,
    open_archive_read_connection_v1,
)
from .service import LedgerService

if TYPE_CHECKING:
    from .recovery import RecoveryAuthority

CORE_ARCHIVE_ORIGIN_ACTION_V1 = "core_archive_origin_v1"
_FORMAT = "core-archive-origin-v1"
_SIGNATURE_PREFIX = _FORMAT + ":"
_EMPTY_HISTORY = historical_generations_sha256_v1(())


def _is_signature_reference(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith(_SIGNATURE_PREFIX)
        and _recovery_hash(value[len(_SIGNATURE_PREFIX) :])
    )


@dataclass(frozen=True)
class CoreArchiveOriginV1:
    """Portable credential. Its signature is checked by the installed target verifier."""

    cut_ref: Mapping[str, str]
    source_binding: RuntimeBinding
    source_admission_entry_sha256: str
    source_admission_kind: str
    parent_history_sha256: str
    source_signature_ref: str
    format: str = _FORMAT

    def __post_init__(self) -> None:
        if type(self.cut_ref) not in {dict, MappingProxyType}:
            raise LifecycleError("Exact Core archive cut reference required")
        reference = dict(self.cut_ref)
        if (
            type(self.format) is not str
            or self.format != _FORMAT
            or not _recovery_cut_ref(reference)
            or type(self.source_binding) is not RuntimeBinding
            or type(self.source_admission_kind) is not str
            or self.source_admission_kind not in {"admit", "restore_admit"}
            or not _recovery_hash(self.source_admission_entry_sha256)
            or not _recovery_hash(self.parent_history_sha256)
            or (self.source_admission_kind == "admit")
            != (self.parent_history_sha256 == _EMPTY_HISTORY)
            or not _is_signature_reference(self.source_signature_ref)
        ):
            raise LifecycleError("Exact Core archive origin credential required")
        object.__setattr__(self, "cut_ref", MappingProxyType(reference))

    def _source_material(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "cut_ref": dict(self.cut_ref),
            "source_binding": asdict(self.source_binding),
            "source_admission_entry_sha256": self.source_admission_entry_sha256,
            "source_admission_kind": self.source_admission_kind,
            "parent_history_sha256": self.parent_history_sha256,
        }

    @property
    def seal_sha256(self) -> str:
        return digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self._source_material(), "source_signature_ref": self.source_signature_ref}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CoreArchiveOriginV1:
        if (
            type(value) is not dict
            or set(value) != set(cls.__dataclass_fields__)
            or not _recovery_cut_ref(value["cut_ref"])
            or type(value["source_binding"]) is not dict
            or set(value["source_binding"]) != set(RuntimeBinding.__dataclass_fields__)
        ):
            raise LifecycleError("Core archive origin JSON shape differs from v1")
        return cls(**{**value, "source_binding": RuntimeBinding(**value["source_binding"])})


def _source_signature_ref(authority: RecoveryAuthority, source_material_sha256: str) -> str:
    material = canonical(
        [_FORMAT, authority.namespace, CORE_ARCHIVE_ORIGIN_ACTION_V1, source_material_sha256]
    )
    return (
        _SIGNATURE_PREFIX
        + hmac.new(authority.key, material.encode("utf-8"), hashlib.sha256).hexdigest()
    )


def core_archive_origin_approval_verifier_v1(
    authority: RecoveryAuthority,
) -> Callable[[str, str, str], bool]:
    """Adapt an externally trusted source authority for the target's approval verifier."""
    from .recovery import RecoveryAuthority

    if type(authority) is not RecoveryAuthority:
        raise LifecycleError("Independent Core archive recovery authority required")

    def verify(reference: str, action: str, source_material_sha256: str) -> bool:
        return (
            action == CORE_ARCHIVE_ORIGIN_ACTION_V1
            and _is_signature_reference(reference)
            and _recovery_hash(source_material_sha256)
            and hmac.compare_digest(
                reference, _source_signature_ref(authority, source_material_sha256)
            )
        )

    return verify


def issue_core_archive_origin_v1(
    service: LedgerService,
    cut: SnapshotCut,
    authority: RecoveryAuthority,
    *,
    snapshot_path: Path,
) -> CoreArchiveOriginV1:
    """Authenticate a closed archive against one current, admitted source transaction."""
    from .recovery import RecoveryAuthority, _verify_cut_connection

    if (
        type(service) is not LedgerService
        or type(cut) is not SnapshotCut
        or type(authority) is not RecoveryAuthority
    ):
        raise LifecycleError("Exact source service, snapshot cut and recovery authority required")
    with service._runtime_scope(), lifecycle_read_operation(service.owner) as conn:
        _require_runtime_scope(conn, service.binding, service.runtime_admission)
        witness = _witness_for(conn)
        if witness is None or witness.owner is not service.owner:
            raise LifecycleError("Core archive origin requires the actual source owner")
        config = service.owner.config
        binding = service.binding
        conn.execute("BEGIN")
        try:
            state = _state(config, _proof=witness)
            record = _record(config, state)
            if record["state"] != "ACTIVE" or state["current"] != config.binding_sha256:
                raise LifecycleError("Core archive origin requires the current ACTIVE source")
            if (
                config.application_schema is not None
                or (binding.instance_id, binding.generation) != (cut.instance_id, cut.generation)
                or config.binding_sha256 != cut.source_binding_sha256
                or config.core_identity != cut.core_identity
                or config._binding["migration_identity"] != cut.migration_identity
                or digest(config._binding["application_schema"]) != cut.application_schema_digest
            ):
                raise LifecycleError("Core archive cut differs from its admitted source")
            entry = next(
                (
                    item
                    for item in state["history"]
                    if item["binding"] == config.binding_sha256
                    and item["operation_id"] == record["admission"]
                ),
                None,
            )
            if entry is None or entry["action"] not in {"admit", "restore_admit"}:
                raise LifecycleError("Core archive source lacks an exact durable admission")
            parent_history_sha256 = _EMPTY_HISTORY
            if entry["action"] == "restore_admit":
                context = entry.get("recovery_context") or {}
                commitment = context.get("historical_generations_sha256")
                if (
                    context.get("format") != _MANUAL_RESTORE_FORMAT
                    or type(commitment) is not str
                    or not _recovery_hash(commitment)
                    or commitment == _EMPTY_HISTORY
                    or not _recovery_hash(context.get("core_archive_origin_sha256"))
                ):
                    raise LifecycleError("Restored Core archive source lacks committed ancestry")
                _protected_history(conn, service, active_only=True)
                parent_history_sha256 = commitment
            elif service.historical_evidence:
                raise LifecycleError("Original Core archive source cannot claim parent history")
            _verify_cut_connection(conn, cut)
            with open_archive_read_connection_v1(
                snapshot_path, database_sha256=cut.database_sha256
            ) as archive:
                _verify_cut_connection(archive, cut)
            source_material = {
                "format": _FORMAT,
                "cut_ref": cut_ref(cut),
                "source_binding": asdict(binding),
                "source_admission_entry_sha256": entry["entry_sha256"],
                "source_admission_kind": entry["action"],
                "parent_history_sha256": parent_history_sha256,
            }
        finally:
            conn.rollback()
    signature_ref = _source_signature_ref(authority, digest(source_material))
    return CoreArchiveOriginV1.from_dict({**source_material, "source_signature_ref": signature_ref})


def verify_core_archive_origin_v1(
    origin: CoreArchiveOriginV1,
    cut: SnapshotCut,
    target_config: TrustedInstanceConfig,
) -> None:
    """Require externally configured provenance for the exact cut."""
    if (
        type(origin) is not CoreArchiveOriginV1
        or type(target_config) is not TrustedInstanceConfig
        or target_config.application_schema is not None
        or origin.cut_ref != cut_ref(cut)
        or (origin.source_binding.instance_id, origin.source_binding.generation)
        != (cut.instance_id, cut.generation)
    ):
        raise LifecycleError("Core archive origin differs from the exact restore cut")
    target_config.assert_valid()
    if (
        target_config.approval_verifier(
            origin.source_signature_ref,
            CORE_ARCHIVE_ORIGIN_ACTION_V1,
            digest(origin._source_material()),
        )
        is not True
    ):
        raise LifecycleError("Trusted source Core archive signature was not verified")
