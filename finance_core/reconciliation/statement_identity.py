"""Canonical source-content and row identity for statement imports."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from finance_core.calculation.authoritative_snapshot import canonical_json_text
from finance_core.money import canonical_decimal_str

STATEMENT_IMPORT_CONTRACT_VERSION = "statement-import-v3"
ROW_FINGERPRINT_VERSION = "statement-row-fingerprint-v1"
CALLER_SUPPLIED_ROW_FINGERPRINT_VERSION = "caller-supplied-sha256-v1"
PDF_ROW_FINGERPRINT_VERSION = "pdf-row-fingerprint-v2"
AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS = frozenset(
    {ROW_FINGERPRINT_VERSION, PDF_ROW_FINGERPRINT_VERSION}
)
ROW_SET_FINGERPRINT_VERSION = "statement-row-set-v1"
IMPORT_COMMAND_VERSION = "statement-import-command-v1"
SOURCE_EVIDENCE_OBSERVATION_VERSION = "statement-source-evidence-observation-v1"

_ROW_DOMAIN = "finance-statement-row-fingerprint-v1"
_ROW_PUBLIC_ID_DOMAIN = "finance-statement-row-public-id-v3"
_ROW_SET_DOMAIN = "finance-statement-row-set-v1"
_COMMAND_DOMAIN = "finance-statement-import-command-v1"
_SOURCE_EVIDENCE_DOMAIN = "finance-statement-source-evidence-observation-v1"

_OPERATIONAL_EVIDENCE_KEYS = {
    "absolute_path",
    "attachment_path",
    "audit_serialization_metadata",
    "batch_id",
    "database_row_id",
    "host",
    "import_timestamp",
    "processing_host",
    "source_file_path",
    "source_filename",
    "original_filename",
    "temporary_directory",
    "temporary_path",
}


@dataclass(frozen=True)
class SourceFileEvidence:
    evidence_path: str
    original_filename: str
    content_hash: str


@dataclass(frozen=True)
class SourceFileContents:
    evidence: SourceFileEvidence
    content: bytes


class SourceFileIdentityError(ValueError):
    """Source bytes cannot be read or verified safely."""


def read_source_file_evidence(
    source_file_path: str | Path,
    *,
    expected_hash: str | None = None,
) -> SourceFileEvidence:
    """Hash exact file bytes without following a final-component symlink."""
    evidence, _ = _read_source_file(source_file_path, expected_hash=expected_hash)
    return evidence


def read_source_file_contents(
    source_file_path: str | Path,
    *,
    expected_hash: str | None = None,
) -> SourceFileContents:
    """Read and hash exact bytes without following a final-component symlink."""
    evidence, content = _read_source_file(
        source_file_path,
        expected_hash=expected_hash,
        collect_content=True,
    )
    assert content is not None
    return SourceFileContents(evidence=evidence, content=content)


def _read_source_file(
    source_file_path: str | Path,
    *,
    expected_hash: str | None,
    collect_content: bool = False,
) -> tuple[SourceFileEvidence, bytes | None]:
    path = Path(source_file_path).expanduser()
    if path.is_symlink():
        raise SourceFileIdentityError(f"Refusing symlink statement source: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SourceFileIdentityError(f"Unable to read statement source file: {path}") from exc
    digest = hashlib.sha256()
    content_chunks: list[bytes] | None = [] if collect_content else None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceFileIdentityError(f"Statement source must be a regular file: {path}")
        with os.fdopen(fd, "rb", closefd=True) as source:
            fd = -1
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                if content_chunks is not None:
                    content_chunks.append(chunk)
    finally:
        if fd >= 0:
            os.close(fd)
    content_hash = digest.hexdigest()
    if expected_hash is not None:
        require_sha256(expected_hash, field="source_file_hash")
        if content_hash != expected_hash:
            raise SourceFileIdentityError(
                "Supplied source_file_hash does not match the exact source file bytes"
            )
    return (
        SourceFileEvidence(
            evidence_path=str(source_file_path),
            original_filename=path.name,
            content_hash=content_hash,
        ),
        b"".join(content_chunks) if content_chunks is not None else None,
    )


def require_sha256(value: str, *, field: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a full lowercase SHA-256 digest")
    return value


def require_authoritative_row_fingerprint_version(value: str) -> str:
    """Require a fingerprint algorithm the importer can deterministically verify."""
    if value not in AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS:
        raise ValueError(f"unsupported authoritative row_fingerprint_version: {value!r}")
    return value


def canonical_row_fingerprint(material: dict[str, Any]) -> str:
    return _domain_hash(_ROW_DOMAIN, canonical_json_text(material))


def canonical_statement_row_fingerprint(
    *,
    source_content_hash: str | None,
    import_contract_version: str,
    source_row_locator: str,
    transaction_date: str | None,
    posted_date: str | None,
    original_amount: str | None,
    normalized_amount: Any,
    currency: str,
    direction: str | None,
    raw_amount_type: str | None,
    merchant_raw: str,
    merchant_normalized: str | None,
    account_id: str | int | None,
    account_name: str | None,
    statement_row_reference: str | None,
    raw_row_payload: object,
) -> str:
    """Reconstruct the derived generic-v1 row fingerprint from stable facts."""
    amount = (
        normalized_amount
        if isinstance(normalized_amount, Decimal)
        else Decimal(str(normalized_amount))
    )
    if not amount.is_finite():
        raise ValueError("statement row normalized amount must be finite")
    material = {
        "row_contract_version": ROW_FINGERPRINT_VERSION,
        "source_content_hash": source_content_hash,
        "import_contract_version": import_contract_version,
        "source_row_locator": source_row_locator,
        "transaction_date": transaction_date,
        "posted_date": posted_date,
        "original_amount": original_amount,
        "normalized_amount": canonical_decimal_str(amount),
        "currency": currency,
        "direction": direction,
        "raw_amount_type": raw_amount_type,
        "merchant_raw": merchant_raw,
        "merchant_normalized": merchant_normalized,
        "account_id": str(account_id) if account_id is not None else None,
        "account_name": account_name,
        "statement_row_reference": statement_row_reference,
        "stable_row_evidence": stable_row_fingerprint_evidence(raw_row_payload),
    }
    return canonical_row_fingerprint(material)


def stable_row_fingerprint_evidence(value: object) -> object:
    """Remove mutable path/host metadata from derived row identity evidence."""
    if isinstance(value, dict):
        return {
            str(key): stable_row_fingerprint_evidence(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).lower() not in _OPERATIONAL_EVIDENCE_KEYS
        }
    if isinstance(value, list):
        return [stable_row_fingerprint_evidence(item) for item in value]
    if isinstance(value, tuple):
        return tuple(stable_row_fingerprint_evidence(item) for item in value)
    return value


def row_set_fingerprint(
    row_fingerprints: Sequence[str] | Sequence[tuple[str, str]],
) -> str:
    entries: list[dict[str, str | None]] = []
    for item in row_fingerprints:
        if isinstance(item, tuple):
            fingerprint, version = item
        else:
            fingerprint, version = item, None
        require_sha256(fingerprint, field="row_fingerprint")
        entries.append({"row_fingerprint": fingerprint, "version": version})
    material = {
        "row_set_version": ROW_SET_FINGERPRINT_VERSION,
        "rows": sorted(
            entries,
            key=lambda entry: (entry["row_fingerprint"] or "", entry["version"] or ""),
        ),
    }
    return _domain_hash(_ROW_SET_DOMAIN, canonical_json_text(material))


def import_command_hash(material: dict[str, Any]) -> str:
    versioned = {"command_version": IMPORT_COMMAND_VERSION, **material}
    return _domain_hash(_COMMAND_DOMAIN, canonical_json_text(versioned))


def derive_batch_public_id(command_hash: str) -> str:
    require_sha256(command_hash, field="import_command_hash")
    return f"statement-batch-{command_hash}"


def derive_statement_row_public_id(
    row_fingerprint: str,
    source_content_hash: str | None,
) -> str:
    """Derive a stable row ID without replacing the persisted fingerprint."""
    require_sha256(row_fingerprint, field="row_fingerprint")
    if source_content_hash is not None:
        require_sha256(source_content_hash, field="source_content_hash")
    material = canonical_json_text(
        {
            "identity_version": "statement-row-public-id-v3",
            "row_fingerprint": row_fingerprint,
            "source_content_hash": source_content_hash,
        }
    )
    return f"stmt-{_domain_hash(_ROW_PUBLIC_ID_DOMAIN, material)}"


def source_evidence_observation_hash(
    *,
    import_command_hash_value: str,
    source_content_hash: str,
    evidence_path: str,
    original_filename: str,
) -> str:
    """Bind one observed path to content without making it import identity."""
    require_sha256(import_command_hash_value, field="import_command_hash")
    require_sha256(source_content_hash, field="source_content_hash")
    material = canonical_json_text(
        {
            "observation_version": SOURCE_EVIDENCE_OBSERVATION_VERSION,
            "import_command_hash": import_command_hash_value,
            "source_content_hash": source_content_hash,
            "evidence_path": evidence_path,
            "original_filename": original_filename,
        }
    )
    return _domain_hash(_SOURCE_EVIDENCE_DOMAIN, material)


def _domain_hash(domain: str, payload: str) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + payload.encode("utf-8")).hexdigest()


__all__ = [
    "AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS",
    "IMPORT_COMMAND_VERSION",
    "CALLER_SUPPLIED_ROW_FINGERPRINT_VERSION",
    "PDF_ROW_FINGERPRINT_VERSION",
    "ROW_FINGERPRINT_VERSION",
    "ROW_SET_FINGERPRINT_VERSION",
    "STATEMENT_IMPORT_CONTRACT_VERSION",
    "SOURCE_EVIDENCE_OBSERVATION_VERSION",
    "SourceFileEvidence",
    "SourceFileContents",
    "SourceFileIdentityError",
    "canonical_row_fingerprint",
    "canonical_statement_row_fingerprint",
    "derive_batch_public_id",
    "derive_statement_row_public_id",
    "import_command_hash",
    "read_source_file_evidence",
    "read_source_file_contents",
    "require_authoritative_row_fingerprint_version",
    "require_sha256",
    "row_set_fingerprint",
    "source_evidence_observation_hash",
    "stable_row_fingerprint_evidence",
]
