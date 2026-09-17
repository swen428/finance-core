"""B5.1a staging runner foundation — type contracts and manifest schema.

Defines the versioned, bounded, strictly-validated input manifest and the
result types for workspace initialization, participant bootstrap, and
authorization recovery.  No field in this module accepts tokens, API keys,
credentials, arbitrary SQL, Python module paths, or shell commands.

See ``docs/design/b5_1a_receipt_staging_runner_foundation_v1.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

MANIFEST_SCHEMA_VERSION = "v1"
RECOVERY_REPORT_SCHEMA_VERSION = "v1"
RUN_MANIFEST_SCHEMA_VERSION = "v1"

MAX_MANIFEST_BYTES = 65_536  # 64 KiB
MAX_PARTICIPANTS = 20
MAX_PUBLIC_ID_LENGTH = 64
MAX_DISPLAY_NAME_LENGTH = 128
MAX_ALIASES_PER_PARTICIPANT = 5
MAX_ALIAS_LENGTH = 64
MAX_NOTES_LENGTH = 256
MAX_ACTOR_ID_LENGTH = 128

_PUBLIC_ID_RE = re.compile(r"^ptcp_[a-z0-9][a-z0-9_]{0,63}$")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RunnerManifestError(ValueError):
    """The input manifest is malformed, oversized, or violates the schema."""


class RunnerWorkspaceError(RuntimeError):
    """The workspace path or filesystem state is unsafe or invalid."""


class CallbackKeyMissingError(RunnerWorkspaceError):
    """The callback signing key file is absent from the workspace runtime.

    Distinguished from unsafe-key refusals so callers can classify
    missing-versus-unsafe structurally instead of parsing error text.
    """


class RunnerParticipantError(RuntimeError):
    """Participant bootstrap failed validation or encountered a conflict."""


class RunnerRecoveryError(RuntimeError):
    """Authorization recovery failed — durable truth is missing or drifted."""


class RunnerResumeError(RuntimeError):
    """B5.1c resume failed — durable truth could not be reconstructed safely."""


class RunnerFinalizeError(RuntimeError):
    """B5.1c finalize failed — a guard refused the operation with zero facts."""

    # ``reason`` carries a stable, bounded block reason when one is known.
    reason: str | None

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Participant definition (manifest input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParticipantDefinition:
    """One participant entry from the validated input manifest."""

    public_id: str
    display_name: str
    is_self: bool
    is_active: bool = True
    aliases: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if not _PUBLIC_ID_RE.match(self.public_id):
            raise RunnerManifestError(
                f"Participant public_id {self.public_id!r} does not match "
                f"the required pattern: ptcp_<lowercase-alphanumeric-underscore>"
            )
        if not self.display_name or not self.display_name.strip():
            raise RunnerManifestError(f"Participant {self.public_id!r} has an empty display_name")
        if len(self.display_name) > MAX_DISPLAY_NAME_LENGTH:
            raise RunnerManifestError(
                f"Participant {self.public_id!r} display_name exceeds "
                f"{MAX_DISPLAY_NAME_LENGTH} characters"
            )
        if len(self.aliases) > MAX_ALIASES_PER_PARTICIPANT:
            raise RunnerManifestError(
                f"Participant {self.public_id!r} has {len(self.aliases)} aliases "
                f"(max {MAX_ALIASES_PER_PARTICIPANT})"
            )
        for alias in self.aliases:
            if not alias or not alias.strip():
                raise RunnerManifestError(f"Participant {self.public_id!r} has an empty alias")
            if len(alias) > MAX_ALIAS_LENGTH:
                raise RunnerManifestError(
                    f"Participant {self.public_id!r} alias exceeds {MAX_ALIAS_LENGTH} characters"
                )
        if len(self.notes) > MAX_NOTES_LENGTH:
            raise RunnerManifestError(
                f"Participant {self.public_id!r} notes exceed {MAX_NOTES_LENGTH} characters"
            )


# ---------------------------------------------------------------------------
# Runner input manifest
# ---------------------------------------------------------------------------

_MANIFEST_REQUIRED_KEYS = frozenset(
    {"schema_version", "workspace_identity", "operator_actor_id", "participants"}
)
_PARTICIPANT_REQUIRED_KEYS = frozenset({"public_id", "display_name", "is_self"})
_PARTICIPANT_OPTIONAL_KEYS = frozenset({"is_active", "aliases", "notes"})
_PARTICIPANT_ALL_KEYS = _PARTICIPANT_REQUIRED_KEYS | _PARTICIPANT_OPTIONAL_KEYS


@dataclass(frozen=True)
class RunnerInputManifest:
    """Validated, immutable runner input manifest."""

    schema_version: str
    workspace_identity: str
    operator_actor_id: str
    participants: tuple[ParticipantDefinition, ...]
    manifest_sha256: str = field(compare=False)
    raw_bytes: bytes = field(compare=False, repr=False)

    @property
    def self_participant(self) -> ParticipantDefinition:
        """Return the exactly-one is_self participant."""
        selves = [p for p in self.participants if p.is_self]
        if len(selves) != 1:
            raise RunnerManifestError("Manifest must have exactly one is_self participant")
        return selves[0]


def parse_runner_manifest(raw: bytes) -> RunnerInputManifest:
    """Parse and validate raw manifest bytes into a typed manifest.

    Rejects oversized input, unknown fields, duplicate participant IDs,
    zero or multiple is_self participants, and malformed JSON.
    """
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RunnerManifestError(
            f"Manifest exceeds maximum size ({len(raw)} > {MAX_MANIFEST_BYTES} bytes)"
        )
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RunnerManifestError(f"Manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RunnerManifestError("Manifest root must be a JSON object")

    unknown_top = frozenset(data) - _MANIFEST_REQUIRED_KEYS
    if unknown_top:
        raise RunnerManifestError(f"Manifest has unknown top-level fields: {sorted(unknown_top)}")
    missing_top = _MANIFEST_REQUIRED_KEYS - frozenset(data)
    if missing_top:
        raise RunnerManifestError(f"Manifest is missing required fields: {sorted(missing_top)}")

    schema_version = data["schema_version"]
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise RunnerManifestError(
            f"Unsupported manifest schema_version {schema_version!r} "
            f"(expected {MANIFEST_SCHEMA_VERSION!r})"
        )

    workspace_identity = data["workspace_identity"]
    if not isinstance(workspace_identity, str) or not workspace_identity.strip():
        raise RunnerManifestError("workspace_identity must be a non-empty string")
    if len(workspace_identity) > MAX_PUBLIC_ID_LENGTH:
        raise RunnerManifestError("workspace_identity exceeds maximum length")

    operator_actor_id = data["operator_actor_id"]
    if not isinstance(operator_actor_id, str) or not operator_actor_id.strip():
        raise RunnerManifestError("operator_actor_id must be a non-empty string")
    if len(operator_actor_id) > MAX_ACTOR_ID_LENGTH:
        raise RunnerManifestError("operator_actor_id exceeds maximum length")

    raw_participants = data["participants"]
    if not isinstance(raw_participants, list):
        raise RunnerManifestError("participants must be a JSON array")
    if len(raw_participants) == 0:
        raise RunnerManifestError("participants must not be empty")
    if len(raw_participants) > MAX_PARTICIPANTS:
        raise RunnerManifestError(
            f"participants count {len(raw_participants)} exceeds maximum {MAX_PARTICIPANTS}"
        )

    participants: list[ParticipantDefinition] = []
    seen_ids: set[str] = set()
    self_count = 0

    for i, entry in enumerate(raw_participants):
        if not isinstance(entry, dict):
            raise RunnerManifestError(f"participants[{i}] must be a JSON object")
        unknown_p = frozenset(entry) - _PARTICIPANT_ALL_KEYS
        if unknown_p:
            raise RunnerManifestError(f"participants[{i}] has unknown fields: {sorted(unknown_p)}")
        missing_p = _PARTICIPANT_REQUIRED_KEYS - frozenset(entry)
        if missing_p:
            raise RunnerManifestError(
                f"participants[{i}] is missing required fields: {sorted(missing_p)}"
            )

        public_id = entry["public_id"]
        display_name = entry["display_name"]
        is_self = entry["is_self"]

        if not isinstance(public_id, str):
            raise RunnerManifestError(f"participants[{i}].public_id must be a string")
        if not isinstance(display_name, str):
            raise RunnerManifestError(f"participants[{i}].display_name must be a string")
        if not isinstance(is_self, bool):
            raise RunnerManifestError(f"participants[{i}].is_self must be a boolean")

        if public_id in seen_ids:
            raise RunnerManifestError(f"Duplicate participant public_id {public_id!r} in manifest")
        seen_ids.add(public_id)

        is_active = entry.get("is_active", True)
        if not isinstance(is_active, bool):
            raise RunnerManifestError(f"participants[{i}].is_active must be a boolean")

        raw_aliases = entry.get("aliases", [])
        if not isinstance(raw_aliases, list):
            raise RunnerManifestError(f"participants[{i}].aliases must be a JSON array")
        for j, alias in enumerate(raw_aliases):
            if not isinstance(alias, str):
                raise RunnerManifestError(f"participants[{i}].aliases[{j}] must be a string")

        notes = entry.get("notes", "")
        if not isinstance(notes, str):
            raise RunnerManifestError(f"participants[{i}].notes must be a string")

        p = ParticipantDefinition(
            public_id=public_id,
            display_name=display_name,
            is_self=is_self,
            is_active=is_active,
            aliases=tuple(raw_aliases),
            notes=notes,
        )
        participants.append(p)
        if is_self:
            self_count += 1

    if self_count != 1:
        raise RunnerManifestError(
            f"Manifest must have exactly one is_self=true participant (got {self_count})"
        )

    manifest_hash = hashlib.sha256(raw).hexdigest()
    return RunnerInputManifest(
        schema_version=schema_version,
        workspace_identity=workspace_identity,
        operator_actor_id=operator_actor_id,
        participants=tuple(participants),
        manifest_sha256=manifest_hash,
        raw_bytes=raw,
    )


def canonical_manifest_hash(raw: bytes) -> str:
    """Compute the SHA-256 hash of exact manifest bytes."""
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Workspace result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerWorkspace:
    """Result of a successful workspace initialization.

    ``callback_key_path`` identifies the OpenClaw staging-bridge callback
    signing key location.  It is a path only: key material never appears in
    this model, envelopes, logs, reports, or audit evidence.
    """

    workspace_path: str
    database_path: str
    attachments_path: str
    runtime_path: str
    evidence_path: str
    manifest_hash: str
    workspace_identity: str
    callback_key_path: str


# ---------------------------------------------------------------------------
# Participant bootstrap result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParticipantBootstrapResult:
    """Result of a deterministic participant bootstrap."""

    participant_public_ids: tuple[str, ...]
    manifest_hash: str
    bootstrap_hash: str
    replayed: bool


# ---------------------------------------------------------------------------
# Recovery evidence report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryEvidence:
    """Versioned, bounded, read-only recovery report."""

    report_schema_version: str
    workspace_identity: str
    workspace_path: str
    manifest_hash: str
    database_identity: str
    migration_ledger_count: int
    latest_migration: str
    migration_verification: str
    participant_bootstrap_hash: str
    authorization_id: str | None
    authorization_state: str | None
    snapshot_id: str | None
    snapshot_hash: str | None
    fact_set_public_id: str | None
    fact_set_version: int | None
    fact_set_input_hash: str | None
    fact_set_result_hash: str | None
    evidence_verification: str
    canonical_financial_facts_created: int
    finalization_executed: bool

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize to a stable JSON-compatible dict for CLI output."""
        return {
            "report_schema_version": self.report_schema_version,
            "workspace_identity": self.workspace_identity,
            "workspace_path": self.workspace_path,
            "manifest_hash": self.manifest_hash,
            "database_identity": self.database_identity,
            "migration_ledger_count": self.migration_ledger_count,
            "latest_migration": self.latest_migration,
            "migration_verification": self.migration_verification,
            "participant_bootstrap_hash": self.participant_bootstrap_hash,
            "authorization_id": self.authorization_id,
            "authorization_state": self.authorization_state,
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "fact_set_public_id": self.fact_set_public_id,
            "fact_set_version": self.fact_set_version,
            "fact_set_input_hash": self.fact_set_input_hash,
            "fact_set_result_hash": self.fact_set_result_hash,
            "evidence_verification": self.evidence_verification,
            "canonical_financial_facts_created": self.canonical_financial_facts_created,
            "finalization_executed": self.finalization_executed,
        }


# ---------------------------------------------------------------------------
# B5.1c versioned run manifest (built solely from durable truth)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunManifest:
    """Versioned, bounded reconstruction of a staging run from durable truth.

    The manifest is evidence, never a source of authority.  It carries no
    mutable financial input: amounts and obligations appear only as stable
    canonical reference hashes and identities.
    """

    report_schema_version: str
    workspace_identity: str
    workspace_path: str
    manifest_hash: str
    operator_actor_id: str
    database_identity: str
    migration_ledger_count: int
    latest_migration: str
    migration_verification: str
    participant_bootstrap_hash: str
    authorization_id: str
    authorization_state: str
    confirmation_id: str
    content_hash: str
    receipt_public_id: str
    receipt_group_public_id: str
    calculation_run_public_id: str
    calculation_snapshot_id: str
    calculation_snapshot_hash: str
    currency_contract_version: str
    fact_set_public_id: str
    fact_set_version: int
    fact_set_input_hash: str
    fact_set_result_hash: str
    source_evidence_refs: tuple[str, ...]
    canonical_financial_facts_created: int
    finalization_executed: bool
    finalization_public_id: str | None
    transaction_public_id: str | None
    audit_id: str | None

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize to a stable JSON-compatible dict for CLI output."""
        return {
            "report_schema_version": self.report_schema_version,
            "workspace_identity": self.workspace_identity,
            "workspace_path": self.workspace_path,
            "manifest_hash": self.manifest_hash,
            "operator_actor_id": self.operator_actor_id,
            "database_identity": self.database_identity,
            "migration_ledger_count": self.migration_ledger_count,
            "latest_migration": self.latest_migration,
            "migration_verification": self.migration_verification,
            "participant_bootstrap_hash": self.participant_bootstrap_hash,
            "authorization_id": self.authorization_id,
            "authorization_state": self.authorization_state,
            "confirmation_id": self.confirmation_id,
            "content_hash": self.content_hash,
            "receipt_public_id": self.receipt_public_id,
            "receipt_group_public_id": self.receipt_group_public_id,
            "calculation_run_public_id": self.calculation_run_public_id,
            "calculation_snapshot_id": self.calculation_snapshot_id,
            "calculation_snapshot_hash": self.calculation_snapshot_hash,
            "currency_contract_version": self.currency_contract_version,
            "fact_set_public_id": self.fact_set_public_id,
            "fact_set_version": self.fact_set_version,
            "fact_set_input_hash": self.fact_set_input_hash,
            "fact_set_result_hash": self.fact_set_result_hash,
            "source_evidence_refs": sorted(self.source_evidence_refs),
            "canonical_financial_facts_created": self.canonical_financial_facts_created,
            "finalization_executed": self.finalization_executed,
            "finalization_public_id": self.finalization_public_id,
            "transaction_public_id": self.transaction_public_id,
            "audit_id": self.audit_id,
        }


# ---------------------------------------------------------------------------
# CLI envelope
# ---------------------------------------------------------------------------

CLI_ENVELOPE_VERSION = "v1"


def cli_success_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a success payload in the stable CLI JSON envelope."""
    return {
        "envelope_version": CLI_ENVELOPE_VERSION,
        "status": "ok",
        "payload": payload,
    }


def cli_error_envelope(
    error_type: str,
    message: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Wrap an error in the stable CLI JSON envelope.

    ``reason`` is an optional bounded block/refusal code carried by the typed
    runner errors (for example ``runner_finalization_locked`` for a retryable
    write-lock refusal).  It is included only when the caller supplies it, so
    the envelope stays backward-compatible.
    """
    envelope: dict[str, Any] = {
        "envelope_version": CLI_ENVELOPE_VERSION,
        "status": "error",
        "error_type": error_type,
        "message": message,
    }
    if reason is not None:
        envelope["reason"] = reason
    return envelope


__all__ = [
    "CLI_ENVELOPE_VERSION",
    "CallbackKeyMissingError",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_MANIFEST_BYTES",
    "MAX_PARTICIPANTS",
    "ParticipantBootstrapResult",
    "ParticipantDefinition",
    "RecoveryEvidence",
    "RUN_MANIFEST_SCHEMA_VERSION",
    "RunManifest",
    "RunnerFinalizeError",
    "RunnerInputManifest",
    "RunnerManifestError",
    "RunnerParticipantError",
    "RunnerRecoveryError",
    "RunnerResumeError",
    "RunnerWorkspace",
    "RunnerWorkspaceError",
    "RECOVERY_REPORT_SCHEMA_VERSION",
    "canonical_manifest_hash",
    "cli_error_envelope",
    "cli_success_envelope",
    "parse_runner_manifest",
]
