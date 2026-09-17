"""Migration-029 fail-closed upgrade checks for migration-028 authoritative data."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.migrations import (
    MIGRATION_029_FILENAME,
    MIGRATION_029_ID,
    MIGRATION_029_PREFLIGHT_ARTIFACT,
    TEMP_DB_MIGRATION_PATHS,
    MigrationExecutionError,
    MigrationPathManifest,
    MigrationPreflightArtifact,
    apply_migration_paths,
)
from finance_core.reconciliation.models import (
    InternalCandidate,
    ReconciliationTransactionType,
    StatementAmountDirection,
)
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    normalize_pdf_statement_row_checked,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
)
from finance_core.reconciliation.repository import ReconciliationRepository
from finance_core.reconciliation.service import ReconciliationService
from finance_core.reconciliation.statement_import import StatementImporter
from finance_core.reconciliation.statement_import_contracts import StructuredStatementRow
from finance_core.staging_guard import create_staging_database

_MIGRATION_029_SQL_PATH: Path = next(
    p for p in TEMP_DB_MIGRATION_PATHS if p.name == MIGRATION_029_FILENAME
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _open_at_028(tmp_path: Path, name: str) -> sqlite3.Connection:
    return create_staging_database(
        tmp_path / f"{name}.sqlite",
        migration_paths=TEMP_DB_MIGRATION_PATHS[:28],
    )


def _copied_manifest(tmp_path: Path, name: str) -> MigrationPathManifest:
    migrations_dir = tmp_path / f"{name}-copied-migrations"
    migrations_dir.mkdir()
    copied_paths: list[Path] = []
    for source in TEMP_DB_MIGRATION_PATHS:
        copied = migrations_dir / source.name
        copied.write_bytes(source.read_bytes())
        copied_paths.append(copied)
    preflight = tmp_path / f"{name}-migration_029_preflight.py"
    preflight.write_bytes(MIGRATION_029_PREFLIGHT_ARTIFACT.read_bytes())
    return MigrationPathManifest(
        paths=tuple(copied_paths),
        preflight_artifacts=(
            MigrationPreflightArtifact(
                migration_id=MIGRATION_029_ID,
                migration_filename=MIGRATION_029_FILENAME,
                path=preflight,
            ),
        ),
    )


def _padded_029_manifest(
    tmp_path: Path,
    name: str,
    filename: str,
    *,
    copied: bool,
) -> tuple[Path, ...] | MigrationPathManifest:
    if copied:
        manifest = _copied_manifest(tmp_path, name)
        _029_copied = next(p for p in manifest.paths if p.name == MIGRATION_029_FILENAME)
        alias = _029_copied.with_name(filename)
        alias.write_bytes(_029_copied.read_bytes())
        return MigrationPathManifest(
            paths=tuple(p for p in manifest.paths if p.name != MIGRATION_029_FILENAME) + (alias,),
            preflight_artifacts=manifest.preflight_artifacts,
        )
    alias = tmp_path / filename
    alias.write_bytes(_MIGRATION_029_SQL_PATH.read_bytes())
    return tuple(p for p in TEMP_DB_MIGRATION_PATHS.paths if p.name != MIGRATION_029_FILENAME) + (
        alias,
    )


def _generic_row(*, reference: str = "line-1") -> StructuredStatementRow:
    return StructuredStatementRow(
        merchant_raw="Upgrade Merchant",
        merchant_normalized="upgrade merchant",
        amount=Decimal("12.34"),
        currency="SGD",
        transaction_date=date(2026, 7, 13),
        posted_date=date(2026, 7, 14),
        statement_row_reference=reference,
        raw_row_payload={"source_line_number": 1, "description": "Upgrade Merchant"},
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="12.34",
        raw_amount_type="debit",
    )


def _seed_generic_batch(
    conn: sqlite3.Connection,
    *,
    suffix: str = "one",
    source_hash: str | None = None,
) -> tuple[int, int]:
    batch = StatementImporter(conn).import_rows(
        [_generic_row(reference=f"line-{suffix}")],
        source_type="bank_statement",
        public_id=f"upgrade-batch-{suffix}",
        source_file_hash=source_hash or _sha(f"upgrade-source-{suffix}"),
        account_name="Upgrade Account",
        currency="SGD",
    )
    return batch.batch_id, batch.owned_row_ids[0]


def _pdf_row(source_hash: str) -> ParsedPdfStatementRow:
    return ParsedPdfStatementRow(
        source_statement_id="upgrade-pdf",
        attachment_path="/synthetic/upgrade.pdf",
        description="Upgrade PDF Merchant",
        amount=Decimal("12.34"),
        currency="SGD",
        source_page_number=1,
        source_row_number=1,
        source_row_ref="page-1:line-1",
        transaction_date=date(2026, 7, 13),
        posted_date=date(2026, 7, 14),
        raw_row_text="13/07/2026 Upgrade PDF Merchant S$12.34 D",
        amount_direction=StatementAmountDirection.DEBIT,
        source_content_hash=source_hash,
        source_filename="upgrade.pdf",
        source_text_excerpt="13/07/2026 Upgrade PDF Merchant S$12.34 D",
        table_section_id="transactions",
        template_name="upgrade-template",
        template_version="upgrade-template-v2",
        direction_source=PdfDirectionSource.EXPLICIT_COLUMN,
        direction_confidence=PdfDirectionConfidence.HIGH,
        review_status=PdfRowReviewStatus.AUTHORITATIVE,
        original_amount_token="S$12.34",
        original_amount_sign=PdfOriginalAmountSign.POSITIVE,
        currency_token="SGD",
        currency_source="template_default",
        transaction_date_token="13/07/2026",
        posted_date_token="14/07/2026",
    )


def _seed_pdf_batch(conn: sqlite3.Connection) -> tuple[int, int]:
    source_hash = _sha("upgrade-pdf-source")
    row = normalize_pdf_statement_row_checked(_pdf_row(source_hash))
    batch = StatementImporter(conn).import_rows(
        [row],
        source_type="bank_statement",
        public_id="upgrade-pdf-batch",
        source_file_hash=source_hash,
        account_name="Upgrade PDF Account",
        currency="SGD",
    )
    return batch.batch_id, batch.owned_row_ids[0]


def _seed_pdf_row(conn: sqlite3.Connection, row: ParsedPdfStatementRow, suffix: str) -> int:
    normalized = normalize_pdf_statement_row_checked(row)
    batch = StatementImporter(conn).import_rows(
        [normalized],
        source_type="bank_statement",
        public_id=f"upgrade-pdf-token-{suffix}",
        source_file_hash=row.source_content_hash,
        account_name="Upgrade PDF Account",
        currency=row.currency,
    )
    return batch.owned_row_ids[0]


def _replace_pdf_payload(
    conn: sqlite3.Connection,
    row_id: int,
    payload_text: str,
) -> None:
    trigger = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'trg_pdf_statement_evidence_update'"
    ).fetchone()[0]
    conn.execute("DROP TRIGGER trg_pdf_statement_evidence_update")
    conn.execute(
        "UPDATE statement_transactions SET raw_row_payload_json = ? WHERE id = ?",
        (payload_text, row_id),
    )
    conn.execute(trigger)
    conn.commit()


def _apply_029_fails(conn: sqlite3.Connection) -> None:
    with pytest.raises(MigrationExecutionError, match="authoritative integrity preflight"):
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = '029'"
        ).fetchone()[0]
        == 0
    )


def _make_preledger_029(conn: sqlite3.Connection) -> None:
    conn.executescript(_MIGRATION_029_SQL_PATH.read_text(encoding="utf-8"))
    conn.execute("DROP TABLE schema_migrations")
    conn.commit()


def _legacy_adoption_fails(conn: sqlite3.Connection) -> None:
    _make_preledger_029(conn)
    with pytest.raises(MigrationExecutionError, match="authoritative integrity preflight"):
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        is None
    )


def test_clean_028_authoritative_data_upgrades_to_029(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "clean")
    try:
        generic_batch, _ = _seed_generic_batch(conn)
        pdf_batch, _ = _seed_pdf_batch(conn)
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:29])
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = '029'"
            ).fetchone()[0]
            == 1
        )
        assert len(ReconciliationRepository(conn).verify_statement_import_batch(generic_batch)) == 1
        assert len(ReconciliationRepository(conn).verify_statement_import_batch(pdf_batch)) == 1
    finally:
        conn.close()


def test_copied_manifest_clean_028_authoritative_data_upgrades_to_029(
    tmp_path: Path,
) -> None:
    manifest = _copied_manifest(tmp_path, "copied-upgrade")
    conn = _open_at_028(tmp_path, "copied-upgrade")
    try:
        _seed_generic_batch(conn)
        _seed_pdf_batch(conn)
        apply_migration_paths(conn, manifest[:29])
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = '029'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_clean_preledger_029_authoritative_data_is_verified_before_adoption(
    tmp_path: Path,
) -> None:
    conn = _open_at_028(tmp_path, "legacy-clean")
    try:
        _seed_generic_batch(conn)
        _seed_pdf_batch(conn)
        _make_preledger_029(conn)
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:29])
        rows = conn.execute(
            "SELECT migration_id, adoption_mode FROM schema_migrations ORDER BY migration_sequence"
        ).fetchall()
        assert len(rows) == 29
        assert rows[-1]["migration_id"] == "029"
        assert all(row["adoption_mode"] == "verified_legacy" for row in rows)
    finally:
        conn.close()


def test_copied_manifest_clean_preledger_029_is_verified_before_adoption(
    tmp_path: Path,
) -> None:
    manifest = _copied_manifest(tmp_path, "copied-adoption")
    conn = _open_at_028(tmp_path, "copied-adoption")
    try:
        _seed_generic_batch(conn)
        _seed_pdf_batch(conn)
        _make_preledger_029(conn)
        apply_migration_paths(conn, manifest[:29])
        rows = conn.execute(
            "SELECT migration_id, adoption_mode FROM schema_migrations ORDER BY migration_sequence"
        ).fetchall()
        assert len(rows) == 29
        assert rows[-1]["migration_id"] == MIGRATION_029_ID
        assert all(row["adoption_mode"] == "verified_legacy" for row in rows)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "filename",
    [
        "0029_authoritative_proof_evidence_integrity.sql",
        "00029_authoritative_proof_evidence_integrity.sql",
    ],
)
@pytest.mark.parametrize("copied", [False, True], ids=["canonical-paths", "copied-manifest"])
def test_padded_migration_029_rejects_verified_legacy_adoption(
    tmp_path: Path,
    filename: str,
    copied: bool,
) -> None:
    manifest = _padded_029_manifest(
        tmp_path,
        f"padded-adoption-{len(filename)}-{copied}",
        filename,
        copied=copied,
    )
    conn = _open_at_028(tmp_path, f"padded-adoption-db-{len(filename)}-{copied}")
    try:
        _seed_generic_batch(conn)
        _seed_pdf_batch(conn)
        _make_preledger_029(conn)
        with pytest.raises(MigrationExecutionError, match="canonical numeric representation"):
            apply_migration_paths(conn, manifest)
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is None
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_preledger_029_generic_corruption_fails_without_ledger(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "legacy-generic")
    try:
        _, row_id = _seed_generic_batch(conn)
        conn.execute(
            "UPDATE statement_transactions SET amount = '99.99' WHERE id = ?",
            (row_id,),
        )
        conn.commit()
        _legacy_adoption_fails(conn)
    finally:
        conn.close()


def test_preledger_029_pdf_corruption_fails_without_ledger(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "legacy-pdf")
    try:
        _, row_id = _seed_pdf_batch(conn)
        conn.execute(
            "UPDATE statement_transactions SET merchant_raw = 'Changed PDF' WHERE id = ?",
            (row_id,),
        )
        conn.commit()
        _legacy_adoption_fails(conn)
    finally:
        conn.close()


def test_preledger_029_duplicate_source_owner_fails_without_ledger(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "legacy-owner")
    try:
        first_batch, _ = _seed_generic_batch(conn, suffix="legacy-first")
        second_batch, _ = _seed_generic_batch(conn, suffix="legacy-second")
        source_hash = conn.execute(
            "SELECT source_file_hash FROM statement_import_batches WHERE id = ?",
            (first_batch,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE statement_import_batches SET source_file_hash = ? WHERE id = ?",
            (source_hash, second_batch),
        )
        conn.commit()
        _legacy_adoption_fails(conn)
    finally:
        conn.close()


def test_preledger_029_audit_corruption_fails_without_ledger(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "legacy-audit")
    try:
        batch_id, _ = _seed_generic_batch(conn)
        batch_public_id = conn.execute(
            "SELECT public_id FROM statement_import_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()[0]
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_financial_audit_events_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_financial_audit_events_no_update")
        conn.execute(
            "UPDATE financial_audit_events SET event_payload_json = ? "
            "WHERE aggregate_public_id = ? AND event_type = 'statement_import_accepted'",
            ('{"contract_version":"finance-canonical-json-v1","value":{}}', batch_public_id),
        )
        conn.execute(trigger)
        conn.commit()
        _legacy_adoption_fails(conn)
    finally:
        conn.close()


def test_preledger_029_decision_corruption_fails_without_ledger(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "legacy-decision")
    try:
        batch_id, _ = _seed_generic_batch(conn)
        ReconciliationService(ReconciliationRepository(conn)).run_reconciliation_for_batch(
            batch_id,
            [
                InternalCandidate(
                    internal_id="legacy-candidate",
                    transaction_date=date(2026, 7, 13),
                    posted_date=date(2026, 7, 14),
                    merchant="Upgrade Merchant",
                    amount=Decimal("12.34"),
                    currency="SGD",
                    transaction_type=ReconciliationTransactionType.EXPENSE,
                    source_type="expense",
                    evidence_reference="transactions:legacy",
                )
            ],
            "legacy-decision-run",
        )
        conn.execute("UPDATE reconciliation_match_results SET match_status = 'no_match'")
        conn.commit()
        _legacy_adoption_fails(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("amount", "99.99"),
        ("transaction_date", "2099-01-01"),
        ("currency", "USD"),
        ("amount_direction", "credit"),
    ],
)
def test_029_rejects_changed_generic_row_with_unchanged_fingerprint(
    tmp_path: Path,
    column: str,
    replacement: str,
) -> None:
    conn = _open_at_028(tmp_path, f"generic-{column}")
    try:
        _, row_id = _seed_generic_batch(conn)
        conn.execute(
            f"UPDATE statement_transactions SET {column} = ? WHERE id = ?",
            (replacement, row_id),
        )
        conn.commit()
        _apply_029_fails(conn)
    finally:
        conn.close()


def test_029_rejects_malformed_pdf_evidence(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "malformed-pdf")
    try:
        _, row_id = _seed_pdf_batch(conn)
        _replace_pdf_payload(conn, row_id, "{malformed")
        _apply_029_fails(conn)
    finally:
        conn.close()


def test_029_rejects_pdf_evidence_fingerprint_mismatch(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "pdf-fingerprint")
    try:
        _, row_id = _seed_pdf_batch(conn)
        conn.execute(
            "UPDATE statement_transactions SET merchant_raw = 'Changed PDF Merchant' WHERE id = ?",
            (row_id,),
        )
        conn.commit()
        _apply_029_fails(conn)
    finally:
        conn.close()


def test_029_rejects_duplicate_authoritative_source_owners(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "duplicate-owner")
    try:
        first_batch, _ = _seed_generic_batch(conn, suffix="first")
        second_batch, _ = _seed_generic_batch(conn, suffix="second")
        first_hash = conn.execute(
            "SELECT source_file_hash FROM statement_import_batches WHERE id = ?",
            (first_batch,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE statement_import_batches SET source_file_hash = ? WHERE id = ?",
            (first_hash, second_batch),
        )
        conn.commit()
        _apply_029_fails(conn)
    finally:
        conn.close()


def test_029_rejects_batch_row_set_mismatch(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "row-set")
    try:
        batch_id, _ = _seed_generic_batch(conn)
        conn.execute(
            "UPDATE statement_import_batches SET row_set_fingerprint = ? WHERE id = ?",
            ("f" * 64, batch_id),
        )
        conn.commit()
        _apply_029_fails(conn)
    finally:
        conn.close()


def test_029_rejects_opaque_migration_028_fingerprint_version(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "opaque-version")
    try:
        _, row_id = _seed_generic_batch(conn)
        conn.execute(
            "UPDATE statement_transactions SET row_fingerprint_version = ? WHERE id = ?",
            ("dictionary-parser-v2", row_id),
        )
        conn.commit()
        _apply_029_fails(conn)
    finally:
        conn.close()


def test_029_rejects_accepted_audit_row_identity_mismatch(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "audit-row-id")
    try:
        batch_id, _ = _seed_generic_batch(conn)
        batch_public_id = conn.execute(
            "SELECT public_id FROM statement_import_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()[0]
        event = conn.execute(
            "SELECT event_public_id, event_payload_json FROM financial_audit_events "
            "WHERE aggregate_public_id = ? AND event_type = 'statement_import_accepted'",
            (batch_public_id,),
        ).fetchone()
        payload = json.loads(event["event_payload_json"])
        payload["value"]["row_public_ids"] = ["tampered-row-public-id"]
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_financial_audit_events_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_financial_audit_events_no_update")
        conn.execute(
            "UPDATE financial_audit_events SET event_payload_json = ? WHERE event_public_id = ?",
            (json.dumps(payload, sort_keys=True), event["event_public_id"]),
        )
        conn.execute(trigger)
        conn.commit()
        _apply_029_fails(conn)
    finally:
        conn.close()


def test_029_rejects_proof_bearing_decision_mismatch(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "decision")
    try:
        batch_id, _ = _seed_generic_batch(conn)
        ReconciliationService(ReconciliationRepository(conn)).run_reconciliation_for_batch(
            batch_id,
            [
                InternalCandidate(
                    internal_id="upgrade-candidate",
                    transaction_date=date(2026, 7, 13),
                    posted_date=date(2026, 7, 14),
                    merchant="Upgrade Merchant",
                    amount=Decimal("12.34"),
                    currency="SGD",
                    transaction_type=ReconciliationTransactionType.EXPENSE,
                    source_type="expense",
                    evidence_reference="transactions:upgrade",
                )
            ],
            "upgrade-decision-run",
        )
        conn.execute("UPDATE reconciliation_match_results SET match_status = 'no_match'")
        conn.commit()
        _apply_029_fails(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "normalized_amount",
    [
        "12.34garbage",
        "12.34 USD",
        "1e2",
        " 12.34",
        "12.34 ",
        "12.345",
        "--12.34",
        "-0.00",
        "NaN",
        "Infinity",
    ],
)
def test_029_rejects_noncanonical_persisted_pdf_amount_text(
    tmp_path: Path,
    normalized_amount: str,
) -> None:
    conn = _open_at_028(tmp_path, f"amount-{hashlib.sha1(normalized_amount.encode()).hexdigest()}")
    try:
        _, row_id = _seed_pdf_batch(conn)
        payload = json.loads(
            conn.execute(
                "SELECT raw_row_payload_json FROM statement_transactions WHERE id = ?",
                (row_id,),
            ).fetchone()[0]
        )
        payload["normalized_amount"] = normalized_amount
        _replace_pdf_payload(conn, row_id, json.dumps(payload, sort_keys=True))
        _apply_029_fails(conn)
    finally:
        conn.close()


def test_029_rejects_explicit_pdf_currency_mismatch(tmp_path: Path) -> None:
    conn = _open_at_028(tmp_path, "currency-mismatch")
    try:
        _, row_id = _seed_pdf_batch(conn)
        payload = json.loads(
            conn.execute(
                "SELECT raw_row_payload_json FROM statement_transactions WHERE id = ?",
                (row_id,),
            ).fetchone()[0]
        )
        payload["original_amount_token"] = "USD12.34"
        payload["currency_token"] = "USD"
        _replace_pdf_payload(conn, row_id, json.dumps(payload, sort_keys=True))
        _apply_029_fails(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("token", "currency", "currency_token"),
    [
        ("-S$12.34", "SGD", "SGD"),
        ("S$-12.34", "SGD", "SGD"),
        ("S$ -12.34", "SGD", "SGD"),
        ("(S$12.34)", "SGD", "SGD"),
        ("RM -12.34", "MYR", "MYR"),
    ],
)
def test_029_preflight_accepts_the_pinned_amount_token_grammar(
    tmp_path: Path,
    token: str,
    currency: str,
    currency_token: str,
) -> None:
    conn = _open_at_028(tmp_path, f"accepted-token-{hashlib.sha1(token.encode()).hexdigest()}")
    try:
        source_hash = _sha(f"accepted-token-source-{token}")
        _seed_pdf_row(
            conn,
            replace(
                _pdf_row(source_hash),
                amount_direction=StatementAmountDirection.CREDIT,
                original_amount_token=token,
                original_amount_sign=PdfOriginalAmountSign.NEGATIVE,
                currency=currency,
                currency_token=currency_token,
                raw_row_text=f"13/07/2026 Upgrade PDF Merchant {token} C",
                source_text_excerpt=f"13/07/2026 Upgrade PDF Merchant {token} C",
            ),
            hashlib.sha1(token.encode()).hexdigest(),
        )
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:29])
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = '029'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


@pytest.mark.parametrize("token", ["- S$12.34", "( S$12.34 )", "+ SGD 12.34"])
def test_029_preflight_rejects_spacing_outside_the_pinned_amount_token_grammar(
    tmp_path: Path,
    token: str,
) -> None:
    conn = _open_at_028(tmp_path, f"rejected-token-{hashlib.sha1(token.encode()).hexdigest()}")
    try:
        _, row_id = _seed_pdf_batch(conn)
        payload = json.loads(
            conn.execute(
                "SELECT raw_row_payload_json FROM statement_transactions WHERE id = ?",
                (row_id,),
            ).fetchone()[0]
        )
        payload["original_amount_token"] = token
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_pdf_statement_evidence_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_pdf_statement_evidence_update")
        conn.execute(
            "UPDATE statement_transactions SET raw_amount = ?, raw_row_payload_json = ? "
            "WHERE id = ?",
            (token, json.dumps(payload, sort_keys=True), row_id),
        )
        conn.execute(trigger)
        conn.commit()
        _apply_029_fails(conn)
    finally:
        conn.close()
