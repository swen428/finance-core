"""Installed, hash-bound resources owned by :mod:`finance_core`."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

MIGRATION_LEDGER_DIGEST = "22d2e6e9a30cd732fc04bdc95945061077a6e38087b97e9c07a5bbc06ad6a668"
MIGRATION_PREFLIGHT_FILENAME = "migration_029_preflight.py"
MIGRATION_PREFLIGHT_SHA256 = "1fe3324447dbd128a80aadcafd54a70974d9c858cbc3a45a1bdaeae1cf48c295"
MIGRATION_PREFLIGHT_BYTE_COUNT = 55_851
MIGRATION_FILENAMES = (
    "001_create_core_schema.sql",
    "002_receipt_split_schema.sql",
    "003_raw_intake_persistence.sql",
    "004_parser_proposal_confirmation.sql",
    "005_raw_intake_source_evidence.sql",
    "006_reconciliation_persistence_schema_v1.sql",
    "007_reconciliation_review_resolution_persistence.sql",
    "008_reconciliation_apply_results.sql",
    "009_statement_import_fingerprint_dedup.sql",
    "010_statement_amount_direction_persistence.sql",
    "011_reconciliation_structured_evidence.sql",
    "012_reconciliation_apply_state_persistence.sql",
    "013_reconciliation_final_mutation_guard_decisions.sql",
    "014_reconciliation_guarded_apply_execution_persistence.sql",
    "015_calculation_run_persistence.sql",
    "016_calculation_snapshot_persistence.sql",
    "017_pdf_statement_import_run_persistence.sql",
    "018_reconciliation_final_mutation_authorization.sql",
    "019_reconciliation_final_mutation_audit.sql",
    "020_receipt_finalization_hardening.sql",
    "021_parser_confirmation_authorization.sql",
    "022_database_conflict_fingerprints.sql",
    "023_finance_application_identity.sql",
    "024_authoritative_calculation_snapshots.sql",
    "025_append_only_financial_audit_chain.sql",
    "026_reconciliation_direction_decision_hashes.sql",
    "027_statement_content_identity.sql",
    "028_pdf_statement_direction_evidence.sql",
    "029_authoritative_proof_evidence_integrity.sql",
    "030_parser_proposal_completion.sql",
    "031_telegram_attachment_evidence.sql",
    "032_receipt_ocr_evidence.sql",
    "033_receipt_ocr_proposal_links.sql",
    "034_receipt_proposal_revisions.sql",
    "035_receipt_proposal_conversions.sql",
    "036_receipt_item_allocation_facts.sql",
    "037_receipt_fact_set_binding_evidence.sql",
    "038_finalization_audit_immutability.sql",
    "039_receipt_scoped_membership_evidence.sql",
    "040_local_receipt_source_evidence.sql",
    "041_openclaw_human_action_references.sql",
    "042_s5e_ai_fallback_provenance_foundation.sql",
    "043_s5e_ai_fallback_lineage_sealing.sql",
    "044_nomi_ai_model_compatibility_receipts.sql",
    "045_nomi_ai_model_admission_decisions.sql",
    "046_openclaw_guided_edit_sessions.sql",
    "047_parser_human_drafts.sql",
    "048_d1_human_ai_lineage_transition.sql",
    "049_d2_one_confirmation_posting.sql",
)


class MigrationResourceError(RuntimeError):
    """Raised when installed migration resources are absent or have drifted."""


@lru_cache(maxsize=1)
def migrations_dir() -> Path:
    """Return the filesystem directory containing installed migration resources."""

    resource_root = files("finance_core.resources").joinpath("migrations")
    if not isinstance(resource_root, Path):
        raise MigrationResourceError("Migration resources require a filesystem installation")
    root = resource_root.resolve()
    if not root.is_dir():
        raise MigrationResourceError("Migration resource directory is missing")
    return root


def _ledger_digest(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise MigrationResourceError(f"Migration resource cannot be read: {path.name}") from exc
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def migration_resource_paths() -> tuple[Path, ...]:
    """Return the exact ordered, checksum-verified migration ledger resources."""

    root = migrations_dir()
    observed = tuple(path.name for path in sorted(root.glob("*.sql")))
    if observed != MIGRATION_FILENAMES:
        raise MigrationResourceError("Installed migration resource inventory has drifted")
    paths = tuple(root / name for name in MIGRATION_FILENAMES)
    if _ledger_digest(paths) != MIGRATION_LEDGER_DIGEST:
        raise MigrationResourceError("Installed migration resource bytes have drifted")
    return paths


@lru_cache(maxsize=1)
def migration_preflight_path() -> Path:
    """Return the exact checksum-bound migration 029 preflight resource."""

    path = migrations_dir().parent / "preflight" / MIGRATION_PREFLIGHT_FILENAME
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise MigrationResourceError("Migration 029 preflight resource is missing") from exc
    if (
        len(payload) != MIGRATION_PREFLIGHT_BYTE_COUNT
        or hashlib.sha256(payload).hexdigest() != MIGRATION_PREFLIGHT_SHA256
    ):
        raise MigrationResourceError("Migration 029 preflight resource bytes have drifted")
    return path


__all__ = [
    "MIGRATION_FILENAMES",
    "MIGRATION_LEDGER_DIGEST",
    "MIGRATION_PREFLIGHT_BYTE_COUNT",
    "MIGRATION_PREFLIGHT_FILENAME",
    "MIGRATION_PREFLIGHT_SHA256",
    "MigrationResourceError",
    "migration_preflight_path",
    "migration_resource_paths",
    "migrations_dir",
]
