"""Exact protected single-host registration; a DTO alone never authorizes use."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from finance_core.instance_admission.authority import (
    _RUNTIME_ACTION,
    InstanceAdmissionError,
    TrustedInstanceConfig,
    _canonical,
    _control,
    _decode,
    _profile,
    _record,
    _reference,
    _state,
    _transition,
    _validate_runtime_context,
    _verify_record_database,
)

from .contracts import LifecycleError, RuntimeBinding


@dataclass(frozen=True)
class SingleHostRuntimeAdmissionV1:
    """Serializable reference to one exact durable authority history entry."""

    binding: RuntimeBinding
    config_binding_sha256: str
    registration_operation_id: str
    registration_version: int
    registration_entry_sha256: str


def _checked_runtime_binding(binding: object) -> dict[str, str]:
    if type(binding) is not RuntimeBinding:
        raise InstanceAdmissionError("Exact RuntimeBinding required")
    material = asdict(binding)
    if any(type(value) is not str or not 0 < len(value) <= 256 for value in material.values()):
        raise InstanceAdmissionError("Invalid runtime binding fields")
    try:
        RuntimeBinding(**material)
    except LifecycleError as exc:
        raise InstanceAdmissionError("Invalid runtime binding") from exc
    return material


def _runtime_entries(state: dict[str, Any], config: TrustedInstanceConfig) -> list[dict[str, Any]]:
    return [
        entry
        for entry in state["history"]
        if entry["action"] == _RUNTIME_ACTION and entry["binding"] == config.binding_sha256
    ]


def _admission_from_entry(entry: dict[str, Any]) -> SingleHostRuntimeAdmissionV1:
    return SingleHostRuntimeAdmissionV1(
        RuntimeBinding(**entry["runtime_context"]["runtime_binding"]),
        entry["binding"],
        entry["operation_id"],
        entry["version"],
        entry["entry_sha256"],
    )


def _require_runtime_admission(
    state: dict[str, Any], config: TrustedInstanceConfig, runtime_admission: object
) -> dict[str, Any] | None:
    """Pure match against already validated state under the caller's control lease.

    No lock, connection, verifier call or process-local authorization cache is
    created here. Current ACTIVE/write eligibility remains the session's job.
    """
    _record(config, state)
    entries = _runtime_entries(state, config)
    if not entries:
        if runtime_admission is not None:
            raise InstanceAdmissionError("Runtime reference has no protected registration")
        return None
    if len(entries) != 1 or type(runtime_admission) is not SingleHostRuntimeAdmissionV1:
        raise InstanceAdmissionError("Registered config requires its exact runtime admission")
    entry = entries[0]
    material = _checked_runtime_binding(runtime_admission.binding)
    if (
        material != entry["runtime_context"]["runtime_binding"]
        or material["instance_id"] != config.instance_id
        or type(runtime_admission.config_binding_sha256) is not str
        or runtime_admission.config_binding_sha256 != config.binding_sha256
        or type(runtime_admission.registration_operation_id) is not str
        or runtime_admission.registration_operation_id != entry["operation_id"]
        or type(runtime_admission.registration_version) is not int
        or runtime_admission.registration_version != entry["version"]
        or type(runtime_admission.registration_entry_sha256) is not str
        or runtime_admission.registration_entry_sha256 != entry["entry_sha256"]
    ):
        raise InstanceAdmissionError("Runtime admission differs from protected registration")
    return entry


def admit_single_host_runtime_v1(
    config: TrustedInstanceConfig,
    binding: RuntimeBinding,
    *,
    operation_id: str,
    approval_ref: str,
) -> SingleHostRuntimeAdmissionV1:
    """Register one approved external binding without changing instance state.

    Interrupted publication uses the original exact
    ``resume_authority_transition`` API before retrying this call. A completed
    retry verifies its original approval digest, even after later transitions.
    """
    _reference(operation_id)
    _reference(approval_ref)
    material = _checked_runtime_binding(binding)
    context = {"format": "single-host-runtime-admission-v1", "runtime_binding": material}
    _validate_runtime_context(context)
    if material["instance_id"] != config.instance_id:
        raise InstanceAdmissionError("Runtime binding belongs to another instance")
    with _control(config) as control:
        state = _state(config)
        record = _record(config, state)
        entries = _runtime_entries(state, config)
        if entries:
            entry = entries[0]
            if (
                len(entries) != 1
                or entry["operation_id"] != operation_id
                or entry["approval_ref"] != approval_ref
                or entry["runtime_context"] != context
                or config.approval_verifier(approval_ref, _RUNTIME_ACTION, entry["approval_digest"])
                is not True
            ):
                raise InstanceAdmissionError("Runtime registration requires exact original retry")
            _verify_record_database(config, record, control)
            return _admission_from_entry(entry)
        if record["state"] not in {"PREPARED", "RESTORE_HELD", "ACTIVE"}:
            raise InstanceAdmissionError("Fenced instance cannot register runtime")
        _verify_record_database(config, record, control, require_no_pending_finalization=True)
        with _profile(config, expected=record["db_identity"]):
            result = _transition(
                config,
                state,
                _decode(_canonical(state)),
                action=_RUNTIME_ACTION,
                operation_id=operation_id,
                approval_ref=approval_ref,
                context=context,
            )
            return _admission_from_entry(result["history"][-1])
