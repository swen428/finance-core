"""Reconciliation Repository v1 -- SQLite-backed persistence for the
Statement Reconciliation Pipeline.

Wraps an existing sqlite3.Connection; does not open database/finance.db.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from finance_core.financial_audit import verify_financial_audit_chain
from finance_core.reconciliation.decision import (
    verify_authoritative_decision_record,
    verify_persisted_decision_hash,
)
from finance_core.reconciliation.models import MatchResult, MatchStatus, StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    build_pdf_statement_evidence_payload,
    build_pdf_statement_row_fingerprint,
    validate_pdf_statement_row,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PDF_ROW_FINGERPRINT_VERSION,
    PdfAmountSignConvention,
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
    validate_canonical_normalized_amount_text,
)
from finance_core.reconciliation.statement_identity import (
    AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS,
    ROW_FINGERPRINT_VERSION,
    canonical_statement_row_fingerprint,
    derive_statement_row_public_id,
    import_command_hash,
    row_set_fingerprint,
    source_evidence_observation_hash,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReconciliationRepositoryError(Exception):
    """Base exception for repository-level errors."""


class DuplicatePublicIdError(ReconciliationRepositoryError):
    """Raised when a public_id conflicts with an existing record."""


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResultRecord:
    """Persisted match result row, with optional parsed JSON fields."""

    id: int
    public_id: str
    run_id: int
    statement_transaction_id: int
    internal_candidate_id: str | None
    match_status: str
    reason_codes_json: str
    evidence_json: str
    amount_delta: Decimal | None = None
    date_delta_days: int | None = None
    merchant_similarity: float | None = None
    needs_review: bool = False
    created_at: str | None = None
    updated_at: str | None = None
    decision_contract_version: str | None = None
    matcher_version: str | None = None
    compatibility_version: str | None = None
    merchant_normalization_version: str | None = None
    candidate_set_fingerprint: str | None = None
    decision_hash: str | None = None
    decision_material_json: str | None = None
    authorization_public_id: str | None = None

    @property
    def reason_codes(self) -> list[str]:
        return json.loads(self.reason_codes_json) if self.reason_codes_json else []

    @property
    def evidence(self) -> dict[str, Any]:
        return json.loads(self.evidence_json) if self.evidence_json else {}


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ReconciliationRepository:
    """SQLite-backed repository for the reconciliation pipeline.

    Wraps an externally-provided **sqlite3.Connection**.  The caller is
    responsible for connection lifecycle, migrations, and foreign-key
    pragma.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Caller-owned connection used by the service Unit of Work."""
        return self._conn

    # ------------------------------------------------------------------
    # 1. Statement import batches
    # ------------------------------------------------------------------

    def create_statement_import_batch(
        self,
        public_id: str,
        source_type: str,
        *,
        account_id: int | None = None,
        account_name: str | None = None,
        statement_period_start: str | None = None,
        statement_period_end: str | None = None,
        currency: str | None = None,
        source_file_path: str | None = None,
        source_file_hash: str | None = None,
        source_filename: str | None = None,
        source_hash_verification_status: str = "legacy_unverified",
        import_contract_version: str | None = None,
        import_command_hash: str | None = None,
        row_set_fingerprint: str | None = None,
    ) -> int:
        """Insert a statement import batch and return its integer id.

        SQL-only: executes an INSERT on the caller-owned connection.  The
        caller owns the transaction and must commit or roll back.
        """
        try:
            cursor = self._conn.execute(
                """
                INSERT INTO statement_import_batches (
                  public_id, source_type, account_id, account_name,
                  statement_period_start, statement_period_end, currency,
                  source_file_path, source_file_hash, source_filename,
                  source_hash_verification_status, import_contract_version,
                  import_command_hash, row_set_fingerprint
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id,
                    source_type,
                    account_id,
                    account_name,
                    statement_period_start,
                    statement_period_end,
                    currency,
                    source_file_path,
                    source_file_hash,
                    source_filename,
                    source_hash_verification_status,
                    import_contract_version,
                    import_command_hash,
                    row_set_fingerprint,
                ),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper() or "public_id" in str(exc).lower():
                raise DuplicatePublicIdError(f"Duplicate public_id: {public_id}") from exc
            raise ReconciliationRepositoryError(str(exc)) from exc

    def get_statement_import_batch_by_public_id(
        self,
        public_id: str,
    ) -> sqlite3.Row | None:
        """Return an import batch row by public_id, or None when absent."""
        row = self._conn.execute(
            """
            SELECT * FROM statement_import_batches
            WHERE public_id = ?
            """,
            (public_id,),
        ).fetchone()
        return row

    def get_statement_import_batch_by_command_hash(
        self,
        import_command_hash: str,
    ) -> sqlite3.Row | None:
        """Return the canonical batch for a versioned import command."""
        return self._conn.execute(
            "SELECT * FROM statement_import_batches WHERE import_command_hash = ?",
            (import_command_hash,),
        ).fetchone()

    def get_authoritative_statement_batch_by_source_hash(
        self,
        source_content_hash: str,
    ) -> sqlite3.Row | None:
        """Return the existing authoritative owner of exact source bytes."""
        return self._conn.execute(
            """SELECT * FROM statement_import_batches
            WHERE source_file_hash = ? AND import_contract_version IS NOT NULL
            ORDER BY id ASC LIMIT 1""",
            (source_content_hash,),
        ).fetchone()

    def record_statement_import_source_evidence(
        self,
        *,
        batch_id: int,
        source_content_hash: str,
        evidence_path: str,
        original_filename: str,
    ) -> bool:
        """Persist one observed file path without making it batch identity.

        SQL-only: the caller owns the transaction. Exact replays are
        idempotent; constraint violations other than the declared unique key
        are not suppressed.
        """
        cursor = self._conn.execute(
            """
            INSERT INTO statement_import_source_evidence (
              batch_id, source_content_hash, evidence_path, original_filename,
              verification_status
            ) VALUES (?, ?, ?, ?, 'verified_from_bytes')
            ON CONFLICT(batch_id, evidence_path, source_content_hash) DO NOTHING
            """,
            (batch_id, source_content_hash, evidence_path, original_filename),
        )
        return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Row fingerprint lookup for idempotent dedup checks
    # ------------------------------------------------------------------

    def has_row_fingerprint_in_batch(
        self,
        batch_id: int,
        row_fingerprint: str,
    ) -> bool:
        """Return True when a row with the given fingerprint exists in the batch."""
        row = self._conn.execute(
            """
            SELECT 1 FROM statement_transactions
            WHERE batch_id = ? AND row_fingerprint = ?
            """,
            (batch_id, row_fingerprint),
        ).fetchone()
        return row is not None

    def get_statement_transaction_by_public_id(
        self,
        public_id: str,
    ) -> sqlite3.Row | None:
        """Return a statement transaction by public_id, or None when absent."""
        row = self._conn.execute(
            """
            SELECT * FROM statement_transactions
            WHERE public_id = ?
            """,
            (public_id,),
        ).fetchone()
        return row

    def get_statement_transaction_by_batch_and_fingerprint(
        self,
        batch_id: int,
        row_fingerprint: str,
    ) -> sqlite3.Row | None:
        """Return a statement transaction by batch and row_fingerprint."""
        row = self._conn.execute(
            """
            SELECT * FROM statement_transactions
            WHERE batch_id = ? AND row_fingerprint = ?
            """,
            (batch_id, row_fingerprint),
        ).fetchone()
        return row

    # ------------------------------------------------------------------
    # 2. Statement transactions (single)
    # ------------------------------------------------------------------

    def create_statement_transaction(
        self,
        public_id: str,
        batch_id: int,
        merchant_raw: str,
        amount: Decimal,
        currency: str,
        *,
        transaction_date: str | None = None,
        posted_date: str | None = None,
        merchant_normalized: str | None = None,
        account_id: int | None = None,
        account_name: str | None = None,
        statement_row_reference: str | None = None,
        row_fingerprint: str | None = None,
        raw_row_payload_json: str | None = None,
        amount_direction: str | None = None,
        raw_amount: str | None = None,
        raw_amount_type: str | None = None,
        row_fingerprint_version: str | None = None,
        external_row_fingerprint: str | None = None,
        external_row_fingerprint_version: str | None = None,
    ) -> int:
        """Insert a single statement transaction and return its integer id.

        SQL-only: executes an INSERT on the caller-owned connection.  The
        caller owns the transaction and must commit or roll back.
        """
        try:
            columns = [
                "public_id",
                "batch_id",
                "transaction_date",
                "posted_date",
                "merchant_raw",
                "merchant_normalized",
                "amount",
                "currency",
                "account_id",
                "account_name",
                "statement_row_reference",
                "raw_row_payload_json",
                "amount_direction",
                "raw_amount",
                "raw_amount_type",
                "row_fingerprint",
                "row_fingerprint_version",
            ]
            values: list[object] = [
                public_id,
                batch_id,
                transaction_date,
                posted_date,
                merchant_raw,
                merchant_normalized,
                str(amount),
                currency,
                account_id,
                account_name,
                statement_row_reference,
                raw_row_payload_json,
                amount_direction,
                raw_amount,
                raw_amount_type,
                row_fingerprint,
                row_fingerprint_version,
            ]
            if _has_column(
                self._conn,
                "statement_transactions",
                "external_row_fingerprint",
            ):
                columns.extend(["external_row_fingerprint", "external_row_fingerprint_version"])
                values.extend([external_row_fingerprint, external_row_fingerprint_version])
            elif external_row_fingerprint is not None:
                raise ReconciliationRepositoryError(
                    "External statement fingerprint evidence requires migration 029"
                )
            placeholders = ", ".join("?" for _column in columns)
            cursor = self._conn.execute(
                f"INSERT INTO statement_transactions ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(values),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper() or "public_id" in str(exc).lower():
                raise DuplicatePublicIdError(f"Duplicate public_id: {public_id}") from exc
            raise ReconciliationRepositoryError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 3. Statement transactions (bulk)
    # ------------------------------------------------------------------

    def create_statement_transactions(
        self,
        batch_id: int,
        rows: Sequence[dict[str, Any]],
    ) -> list[int]:
        """Insert statement transactions and return created ids.

        Each row dict must contain at minimum: ``public_id``,
        ``merchant_raw``, ``amount`` (Decimal or str), ``currency``.

        SQL-only: executes INSERTs on the caller-owned connection.  The
        caller owns the transaction and must commit or roll back.
        """
        ids: list[int] = []
        try:
            has_external_columns = _has_column(
                self._conn,
                "statement_transactions",
                "external_row_fingerprint",
            )
            for row in rows:
                amount = row["amount"]
                if isinstance(amount, Decimal):
                    amount = str(amount)
                columns = [
                    "public_id",
                    "batch_id",
                    "transaction_date",
                    "posted_date",
                    "merchant_raw",
                    "merchant_normalized",
                    "amount",
                    "currency",
                    "account_id",
                    "account_name",
                    "statement_row_reference",
                    "raw_row_payload_json",
                    "amount_direction",
                    "raw_amount",
                    "raw_amount_type",
                    "row_fingerprint",
                    "row_fingerprint_version",
                ]
                values: list[object] = [
                    row["public_id"],
                    batch_id,
                    row.get("transaction_date"),
                    row.get("posted_date"),
                    row["merchant_raw"],
                    row.get("merchant_normalized"),
                    amount,
                    row["currency"],
                    row.get("account_id"),
                    row.get("account_name"),
                    row.get("statement_row_reference"),
                    row.get("raw_row_payload_json"),
                    row.get("amount_direction"),
                    row.get("raw_amount"),
                    row.get("raw_amount_type"),
                    row.get("row_fingerprint"),
                    row.get("row_fingerprint_version"),
                ]
                if has_external_columns:
                    columns.extend(["external_row_fingerprint", "external_row_fingerprint_version"])
                    values.extend(
                        [
                            row.get("external_row_fingerprint"),
                            row.get("external_row_fingerprint_version"),
                        ]
                    )
                elif row.get("external_row_fingerprint") is not None:
                    raise ReconciliationRepositoryError(
                        "External statement fingerprint evidence requires migration 029"
                    )
                placeholders = ", ".join("?" for _column in columns)
                cursor = self._conn.execute(
                    f"INSERT INTO statement_transactions ({', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    tuple(values),
                )
                assert cursor.lastrowid is not None
                ids.append(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper() or "public_id" in str(exc).lower():
                raise DuplicatePublicIdError("Duplicate public_id in bulk insert") from exc
            raise ReconciliationRepositoryError(str(exc)) from exc
        return ids

    # ------------------------------------------------------------------
    # 4. Reconciliation runs
    # ------------------------------------------------------------------

    def create_reconciliation_run(
        self,
        public_id: str,
        *,
        batch_id: int | None = None,
        matcher_version: str | None = None,
    ) -> int:
        """Create a reconciliation run with status ``started``."""
        try:
            cursor = self._conn.execute(
                """
                INSERT INTO reconciliation_runs (
                  public_id, batch_id, run_status, matcher_version
                )
                VALUES (?, ?, 'started', ?)
                """,
                (public_id, batch_id, matcher_version),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper() or "public_id" in str(exc).lower():
                raise DuplicatePublicIdError(f"Duplicate public_id: {public_id}") from exc
            raise ReconciliationRepositoryError(str(exc)) from exc

    def complete_reconciliation_run(self, run_id: int) -> None:
        """Set the run status to ``completed``."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self._conn.execute(
            """
            UPDATE reconciliation_runs
            SET run_status = 'completed',
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, run_id),
        )

    def fail_reconciliation_run(self, run_id: int) -> None:
        """Set the run status to ``failed``."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self._conn.execute(
            """
            UPDATE reconciliation_runs
            SET run_status = 'failed',
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, run_id),
        )

    # ------------------------------------------------------------------
    # 5. Match results
    # ------------------------------------------------------------------

    def save_match_result(
        self,
        run_id: int,
        statement_transaction_id: int,
        match_result: MatchResult,
        *,
        public_id: str | None = None,
        internal_candidate_id: str | None = None,
    ) -> int:
        """Persist a MatchResult as a reconciliation_match_results row."""
        if (match_result.decision_hash is None) != (match_result.decision_material_json is None):
            raise ReconciliationRepositoryError(
                "Decision hash and canonical decision material must be supplied together"
            )
        if match_result.decision_hash is not None and not verify_persisted_decision_hash(
            match_result.decision_hash,
            match_result.decision_material_json or "",
        ):
            raise ReconciliationRepositoryError("Reconciliation decision hash verification failed")
        reason_codes_json = json.dumps(
            [r.value for r in match_result.reasons],
            sort_keys=True,
        )
        evidence_json = _serialize_evidence(match_result)

        amount_delta = _compute_amount_delta(match_result)
        date_delta_days = match_result.evidence.date_delta_days
        merchant_similarity = match_result.evidence.merchant_similarity

        needs_review = 1 if match_result.status != MatchStatus.MATCHED else 0

        if internal_candidate_id is None and match_result.best_candidate is not None:
            internal_candidate_id = match_result.best_candidate.internal_id

        if public_id is None:
            # Low-level fallback: deterministic, derived from run + statement.
            # Callers should prefer explicit domain-meaningful public_ids.
            public_id = f"match-{run_id}-{statement_transaction_id}"

        existing = self._conn.execute(
            """SELECT * FROM reconciliation_match_results
               WHERE public_id = ?""",
            (public_id,),
        ).fetchone()
        if existing is not None:
            if _is_identical_decision_replay(
                existing,
                run_id=run_id,
                statement_transaction_id=statement_transaction_id,
                match_result=match_result,
            ):
                self.verify_match_result_integrity(public_id, require_audit=False)
                return int(existing["id"])
            raise DuplicatePublicIdError(f"Conflicting reconciliation decision: {public_id}")

        try:
            cursor = self._conn.execute(
                """
                INSERT INTO reconciliation_match_results (
                  public_id, run_id, statement_transaction_id,
                  internal_candidate_id,
                  match_status, reason_codes_json, evidence_json,
                  amount_delta, date_delta_days, merchant_similarity,
                  needs_review, decision_contract_version, matcher_version,
                  compatibility_version, merchant_normalization_version,
                  candidate_set_fingerprint, decision_hash,
                  decision_material_json, authorization_public_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id,
                    run_id,
                    statement_transaction_id,
                    internal_candidate_id,
                    match_result.status.value,
                    reason_codes_json,
                    evidence_json,
                    str(amount_delta) if amount_delta is not None else None,
                    date_delta_days,
                    merchant_similarity,
                    needs_review,
                    match_result.decision_contract_version,
                    match_result.matcher_version,
                    match_result.compatibility_version,
                    match_result.merchant_normalization_version,
                    match_result.candidate_set_fingerprint,
                    match_result.decision_hash,
                    match_result.decision_material_json,
                    match_result.authorization_public_id,
                ),
            )
            assert cursor.lastrowid is not None
            self.verify_match_result_integrity(public_id, require_audit=False)
            return cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper() or "public_id" in str(exc).lower():
                existing = self._conn.execute(
                    """SELECT * FROM reconciliation_match_results
                       WHERE public_id = ?""",
                    (public_id,),
                ).fetchone()
                if existing is not None and _is_identical_decision_replay(
                    existing,
                    run_id=run_id,
                    statement_transaction_id=statement_transaction_id,
                    match_result=match_result,
                ):
                    self.verify_match_result_integrity(public_id, require_audit=False)
                    return int(existing["id"])
                raise DuplicatePublicIdError(f"Duplicate public_id: {public_id}") from exc
            raise ReconciliationRepositoryError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 6. Queries
    # ------------------------------------------------------------------

    def get_statement_transactions_by_batch(self, batch_id: int) -> list[sqlite3.Row]:
        """Return all statement_transactions rows for a given batch."""
        rows = self._conn.execute(
            """
            SELECT st.*,
                   b.public_id AS batch_public_id,
                   b.source_file_hash AS source_content_hash
            FROM statement_transactions AS st
            JOIN statement_import_batches AS b ON b.id = st.batch_id
            WHERE st.batch_id = ?
            ORDER BY st.id ASC
            """,
            (batch_id,),
        ).fetchall()
        self._verify_statement_import_batch(batch_id, rows)
        return rows

    def verify_statement_import_batch(self, batch_id: int) -> list[sqlite3.Row]:
        """Verify authoritative batch columns, rows, fingerprints, and audit."""
        rows = self._conn.execute(
            """SELECT st.*,
                   b.public_id AS batch_public_id,
                   b.source_file_hash AS source_content_hash
            FROM statement_transactions AS st
            JOIN statement_import_batches AS b ON b.id = st.batch_id
            WHERE st.batch_id = ?
            ORDER BY st.id ASC""",
            (batch_id,),
        ).fetchall()
        self._verify_statement_import_batch(batch_id, rows)
        return rows

    def verify_authoritative_state_before_migration_029(self) -> None:
        """Fail closed unless every migration-028 authoritative fact verifies."""
        duplicate_owner = self._conn.execute(
            """SELECT source_file_hash
            FROM statement_import_batches
            WHERE import_contract_version IS NOT NULL AND source_file_hash IS NOT NULL
            GROUP BY source_file_hash HAVING COUNT(*) > 1
            ORDER BY source_file_hash LIMIT 1"""
        ).fetchone()
        if duplicate_owner is not None:
            raise ReconciliationRepositoryError(
                "Authoritative integrity preflight found duplicate statement source owners"
            )

        batches = self._conn.execute(
            """SELECT DISTINCT b.id
            FROM statement_import_batches AS b
            LEFT JOIN statement_transactions AS st ON st.batch_id = b.id
            WHERE b.import_contract_version IS NOT NULL
               OR b.import_command_hash IS NOT NULL
               OR b.row_set_fingerprint IS NOT NULL
               OR st.row_fingerprint_version IS NOT NULL
            ORDER BY b.id"""
        ).fetchall()
        for batch in batches:
            self.verify_statement_import_batch(int(batch["id"]))

        decisions = self._conn.execute(
            """SELECT public_id
            FROM reconciliation_match_results
            WHERE decision_contract_version IS NOT NULL
               OR matcher_version IS NOT NULL
               OR compatibility_version IS NOT NULL
               OR merchant_normalization_version IS NOT NULL
               OR candidate_set_fingerprint IS NOT NULL
               OR decision_hash IS NOT NULL
               OR decision_material_json IS NOT NULL
            ORDER BY id"""
        ).fetchall()
        for decision in decisions:
            self.verify_match_result_integrity(
                str(decision["public_id"]),
                require_audit=True,
            )

    def verify_match_result_integrity(
        self,
        public_id: str,
        *,
        require_audit: bool,
    ) -> sqlite3.Row:
        """Reload one decision and verify proof against relational authority."""
        row = self._conn.execute(
            "SELECT * FROM reconciliation_match_results WHERE public_id = ?",
            (public_id,),
        ).fetchone()
        if row is None:
            raise ReconciliationRepositoryError(
                f"Reconciliation decision integrity row is missing: {public_id}"
            )
        proof_columns = (
            "decision_contract_version",
            "matcher_version",
            "compatibility_version",
            "merchant_normalization_version",
            "candidate_set_fingerprint",
            "decision_hash",
            "decision_material_json",
        )
        proof_values = [row[column] for column in proof_columns]
        if all(value is None for value in proof_values):
            return row
        if any(value is None for value in proof_values):
            raise ReconciliationRepositoryError(
                "Reconciliation decision integrity proof is incomplete"
            )
        statement_row = self._conn.execute(
            """SELECT st.*, b.source_file_hash AS source_content_hash
            FROM statement_transactions AS st
            JOIN statement_import_batches AS b ON b.id = st.batch_id
            WHERE st.id = ?""",
            (row["statement_transaction_id"],),
        ).fetchone()
        if statement_row is None or not verify_authoritative_decision_record(
            row,
            statement_row,
        ):
            raise ReconciliationRepositoryError(
                "Reconciliation decision integrity verification failed"
            )
        if require_audit:
            self._verify_reconciliation_decision_audit(row, statement_row)
        return row

    # ------------------------------------------------------------------

    def get_match_results_for_run(self, run_id: int) -> list[MatchResultRecord]:
        """Return all match results for a reconciliation run."""
        rows = self._conn.execute(
            """
            SELECT * FROM reconciliation_match_results
            WHERE run_id = ?
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()
        return [
            _row_to_match_result_record(
                self.verify_match_result_integrity(r["public_id"], require_audit=True)
            )
            for r in rows
        ]

    def get_review_required_results(self, run_id: int | None = None) -> list[MatchResultRecord]:
        """Return match results flagged for human review."""
        if run_id is not None:
            rows = self._conn.execute(
                """
                SELECT * FROM reconciliation_match_results
                WHERE needs_review = 1 AND run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM reconciliation_match_results
                WHERE needs_review = 1
                ORDER BY id ASC
                """,
            ).fetchall()
        return [
            _row_to_match_result_record(
                self.verify_match_result_integrity(r["public_id"], require_audit=True)
            )
            for r in rows
        ]

    def get_unmatched_statement_transactions(self, run_id: int) -> list[MatchResultRecord]:
        """Return match results where status is not ``matched``."""
        rows = self._conn.execute(
            """
            SELECT * FROM reconciliation_match_results
            WHERE run_id = ? AND match_status != 'matched'
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()
        return [
            _row_to_match_result_record(
                self.verify_match_result_integrity(r["public_id"], require_audit=True)
            )
            for r in rows
        ]

    def _verify_statement_import_batch(
        self,
        batch_id: int,
        rows: Sequence[sqlite3.Row],
    ) -> None:
        batch = self._conn.execute(
            "SELECT * FROM statement_import_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise ReconciliationRepositoryError("Statement batch integrity row is missing")
        identity_columns = (
            "import_contract_version",
            "import_command_hash",
            "row_set_fingerprint",
        )
        identity_values = [batch[column] for column in identity_columns]
        if all(value is None for value in identity_values):
            if any(row["row_fingerprint_version"] is not None for row in rows):
                raise ReconciliationRepositoryError(
                    "Statement batch integrity has versioned rows without batch authority"
                )
            return
        if any(value is None for value in identity_values):
            raise ReconciliationRepositoryError(
                "Statement batch integrity identity proof is incomplete"
            )
        if not rows:
            raise ReconciliationRepositoryError(
                "Statement batch integrity verification found no owned rows"
            )
        fingerprints: list[tuple[str, str]] = []
        row_public_ids: list[str] = []
        row_ids: set[int] = set()
        for row in rows:
            if row["batch_id"] != batch_id:
                raise ReconciliationRepositoryError("Statement batch integrity ownership mismatch")
            fingerprint = row["row_fingerprint"]
            version = row["row_fingerprint_version"]
            if not isinstance(fingerprint, str) or not isinstance(version, str):
                raise ReconciliationRepositoryError(
                    "Statement batch integrity fingerprint is incomplete"
                )
            self._verify_statement_row_fingerprint(
                batch,
                row,
                source_row_index=len(fingerprints) + 1,
            )
            expected_public_id = derive_statement_row_public_id(
                fingerprint,
                batch["source_file_hash"],
            )
            if row["public_id"] != expected_public_id:
                raise ReconciliationRepositoryError(
                    "Statement batch integrity row identity mismatch"
                )
            fingerprints.append((fingerprint, version))
            row_public_ids.append(str(row["public_id"]))
            row_ids.add(int(row["id"]))
        if len(row_ids) != len(rows):
            raise ReconciliationRepositoryError(
                "Statement batch integrity contains duplicate owned rows"
            )
        persisted_row_set = row_set_fingerprint(fingerprints)
        if persisted_row_set != batch["row_set_fingerprint"]:
            raise ReconciliationRepositoryError(
                "Statement batch integrity row-set fingerprint mismatch"
            )
        expected_command_hash = import_command_hash(
            {
                "import_contract_version": batch["import_contract_version"],
                "source_type": batch["source_type"],
                "source_content_hash": batch["source_file_hash"],
                "account_id": batch["account_id"],
                "account_name": batch["account_name"],
                "statement_period_start": batch["statement_period_start"],
                "statement_period_end": batch["statement_period_end"],
                "currency": batch["currency"],
                "row_set_fingerprint": persisted_row_set,
            }
        )
        if expected_command_hash != batch["import_command_hash"]:
            raise ReconciliationRepositoryError("Statement batch integrity command hash mismatch")
        self._verify_statement_source_evidence(batch)
        self._verify_statement_import_audit(
            batch,
            rows=rows,
            row_public_ids=row_public_ids,
            row_fingerprints=[fingerprint for fingerprint, _version in fingerprints],
        )

    def _verify_statement_row_fingerprint(
        self,
        batch: sqlite3.Row,
        row: sqlite3.Row,
        *,
        source_row_index: int,
    ) -> None:
        version = str(row["row_fingerprint_version"])
        fingerprint = str(row["row_fingerprint"])
        try:
            if version not in AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS:
                raise ReconciliationRepositoryError(
                    "Statement batch integrity fingerprint version is not authoritative"
                )
            payload = (
                json.loads(row["raw_row_payload_json"])
                if row["raw_row_payload_json"] is not None
                else None
            )
            if version == ROW_FINGERPRINT_VERSION:
                expected = canonical_statement_row_fingerprint(
                    source_content_hash=batch["source_file_hash"],
                    import_contract_version=str(batch["import_contract_version"]),
                    source_row_locator=row["statement_row_reference"]
                    or f"source-row-{source_row_index}",
                    transaction_date=row["transaction_date"],
                    posted_date=row["posted_date"],
                    original_amount=row["raw_amount"],
                    normalized_amount=Decimal(str(row["amount"])),
                    currency=str(row["currency"]),
                    direction=row["amount_direction"],
                    raw_amount_type=row["raw_amount_type"],
                    merchant_raw=str(row["merchant_raw"]),
                    merchant_normalized=row["merchant_normalized"],
                    account_id=row["account_id"],
                    account_name=row["account_name"],
                    statement_row_reference=row["statement_row_reference"],
                    raw_row_payload=payload,
                )
                if expected != fingerprint:
                    raise ReconciliationRepositoryError(
                        "Statement batch integrity generic row fingerprint mismatch"
                    )
            elif version == PDF_ROW_FINGERPRINT_VERSION:
                self._verify_pdf_statement_row_fingerprint(
                    batch,
                    row,
                    payload,
                )
        except ReconciliationRepositoryError:
            raise
        except (ArithmeticError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReconciliationRepositoryError(
                "Statement batch integrity row fingerprint evidence is malformed"
            ) from exc

    def _verify_pdf_statement_row_fingerprint(
        self,
        batch: sqlite3.Row,
        row: sqlite3.Row,
        payload: object,
    ) -> None:
        if not isinstance(payload, dict):
            raise ReconciliationRepositoryError(
                "Statement batch integrity PDF evidence is malformed"
            )
        amount = Decimal(str(row["amount"]))
        validate_canonical_normalized_amount_text(
            payload.get("normalized_amount"),
            relational_amount=amount,
        )
        parsed = ParsedPdfStatementRow(
            source_statement_id="persisted-authoritative-row",
            attachment_path=payload["attachment_path"],
            description=str(row["merchant_raw"]),
            amount=amount,
            currency=str(row["currency"]),
            source_page_number=payload["source_page_number"],
            source_row_number=payload.get("source_row_number"),
            source_row_ref=payload["stable_row_locator"],
            transaction_date=(
                date.fromisoformat(row["transaction_date"])
                if row["transaction_date"] is not None
                else None
            ),
            posted_date=(
                date.fromisoformat(row["posted_date"]) if row["posted_date"] is not None else None
            ),
            raw_row_text=payload["original_line_text"],
            amount_direction=StatementAmountDirection(row["amount_direction"]),
            source_content_hash=batch["source_file_hash"],
            source_filename=payload.get("source_filename"),
            source_text_excerpt=payload["source_text_excerpt"],
            table_section_id=payload.get("table_section_id"),
            parser_name=payload["parser_name"],
            parser_version=payload["parser_version"],
            template_name=payload["template_name"],
            template_version=payload["template_version"],
            extraction_version=payload["extraction_version"],
            evidence_contract_version=payload["evidence_contract_version"],
            direction_source=PdfDirectionSource(payload["direction_source"]),
            direction_confidence=PdfDirectionConfidence(payload["direction_confidence"]),
            review_status=PdfRowReviewStatus(payload["review_status"]),
            review_reason=payload.get("review_reason"),
            original_amount_token=payload["original_amount_token"],
            original_amount_sign=PdfOriginalAmountSign(payload["original_amount_sign"]),
            amount_sign_convention=PdfAmountSignConvention(payload["amount_sign_convention"]),
            currency_token=payload["currency_token"],
            currency_source=payload["currency_source"],
            transaction_date_token=payload.get("transaction_date_token"),
            posted_date_token=payload.get("posted_date_token"),
        )
        validation = validate_pdf_statement_row(parsed)
        if validation.is_blocked:
            raise ReconciliationRepositoryError(
                "Statement batch integrity PDF evidence is not authoritative"
            )
        fingerprint = build_pdf_statement_row_fingerprint(parsed)
        if fingerprint != row["row_fingerprint"]:
            raise ReconciliationRepositoryError(
                "Statement batch integrity PDF row fingerprint mismatch"
            )
        expected_payload = build_pdf_statement_evidence_payload(
            parsed,
            row_fingerprint=fingerprint,
        )
        if payload != expected_payload:
            raise ReconciliationRepositoryError(
                "Statement batch integrity PDF evidence does not match relational authority"
            )

    def _verify_statement_source_evidence(self, batch: sqlite3.Row) -> None:
        evidence_rows = self._conn.execute(
            """SELECT * FROM statement_import_source_evidence
            WHERE batch_id = ? ORDER BY id""",
            (batch["id"],),
        ).fetchall()
        if batch["source_hash_verification_status"] == "verified_from_bytes" and not evidence_rows:
            raise ReconciliationRepositoryError(
                "Statement batch integrity verified source evidence is missing"
            )
        for evidence in evidence_rows:
            if (
                evidence["source_content_hash"] != batch["source_file_hash"]
                or evidence["verification_status"] != "verified_from_bytes"
                or not str(evidence["evidence_path"]).strip()
                or not str(evidence["original_filename"]).strip()
            ):
                raise ReconciliationRepositoryError(
                    "Statement batch integrity source evidence ownership mismatch"
                )
            observation_hash = source_evidence_observation_hash(
                import_command_hash_value=str(batch["import_command_hash"]),
                source_content_hash=str(evidence["source_content_hash"]),
                evidence_path=str(evidence["evidence_path"]),
                original_filename=str(evidence["original_filename"]),
            )
            event = self._conn.execute(
                """SELECT 1 FROM financial_audit_events
                WHERE aggregate_type = 'statement_import_batch'
                  AND aggregate_public_id = ?
                  AND event_type = 'statement_import_source_evidence_observed'
                  AND causation_public_id = ?""",
                (batch["public_id"], observation_hash),
            ).fetchone()
            if event is None:
                raise ReconciliationRepositoryError(
                    "Statement batch integrity source evidence audit is missing"
                )

    def _verify_statement_import_audit(
        self,
        batch: sqlite3.Row,
        *,
        rows: Sequence[sqlite3.Row],
        row_public_ids: list[str],
        row_fingerprints: list[str],
    ) -> None:
        chain = verify_financial_audit_chain(
            self._conn,
            aggregate_type="statement_import_batch",
            aggregate_public_id=batch["public_id"],
        )
        if not chain.valid or chain.event_count == 0:
            raise ReconciliationRepositoryError(
                "Statement batch integrity audit chain is missing or invalid"
            )
        event = self._conn.execute(
            """SELECT * FROM financial_audit_events
            WHERE aggregate_type = 'statement_import_batch'
              AND aggregate_public_id = ?
              AND event_type = 'statement_import_accepted'
              AND causation_public_id = ?""",
            (batch["public_id"], batch["import_command_hash"]),
        ).fetchone()
        if event is None:
            raise ReconciliationRepositoryError(
                "Statement batch integrity accepted audit is missing"
            )
        payload = _canonical_audit_value(event["event_payload_json"])
        state = _canonical_audit_value(event["new_state_json"])
        expected_ids = sorted(row_public_ids)
        expected_fingerprints = sorted(row_fingerprints)
        expected_pdf_evidence = sorted(
            (
                {
                    "row_public_id": str(row["public_id"]),
                    "row_fingerprint": str(row["row_fingerprint"]),
                    "evidence": json.loads(row["raw_row_payload_json"]),
                }
                for row in rows
                if row["row_fingerprint_version"] == PDF_ROW_FINGERPRINT_VERSION
            ),
            key=lambda item: item["row_fingerprint"],
        )
        expected_external_evidence = sorted(
            (
                {
                    "row_public_id": str(row["public_id"]),
                    "external_row_fingerprint": str(row["external_row_fingerprint"]),
                    "external_row_fingerprint_version": str(
                        row["external_row_fingerprint_version"]
                    ),
                }
                for row in rows
                if "external_row_fingerprint" in row.keys()
                and row["external_row_fingerprint"] is not None
            ),
            key=lambda item: (
                item["external_row_fingerprint"],
                item["external_row_fingerprint_version"],
                item["row_public_id"],
            ),
        )
        if (
            payload.get("source_type") != batch["source_type"]
            or payload.get("source_file_hash") != batch["source_file_hash"]
            or payload.get("import_contract_version") != batch["import_contract_version"]
            or payload.get("import_command_hash") != batch["import_command_hash"]
            or payload.get("row_set_fingerprint") != batch["row_set_fingerprint"]
            or payload.get("row_count") != len(row_public_ids)
            or sorted(payload.get("row_public_ids", [])) != expected_ids
            or payload.get("pdf_row_evidence", []) != expected_pdf_evidence
            or payload.get("external_row_evidence", []) != expected_external_evidence
            or state.get("import_command_hash") != batch["import_command_hash"]
            or state.get("row_set_fingerprint") != batch["row_set_fingerprint"]
            or sorted(state.get("row_public_ids", [])) != expected_ids
            or sorted(state.get("row_fingerprints", [])) != expected_fingerprints
            or state.get("pdf_row_evidence", []) != expected_pdf_evidence
            or state.get("external_row_evidence", []) != expected_external_evidence
        ):
            raise ReconciliationRepositoryError(
                "Statement batch integrity audit does not match owned rows"
            )

    def _verify_reconciliation_decision_audit(
        self,
        row: sqlite3.Row,
        statement_row: sqlite3.Row,
    ) -> None:
        chain = verify_financial_audit_chain(
            self._conn,
            aggregate_type="reconciliation_match_result",
            aggregate_public_id=row["public_id"],
        )
        if not chain.valid or chain.event_count == 0:
            raise ReconciliationRepositoryError(
                "Reconciliation decision integrity audit chain is missing or invalid"
            )
        event = self._conn.execute(
            """SELECT * FROM financial_audit_events
            WHERE aggregate_type = 'reconciliation_match_result'
              AND aggregate_public_id = ?
              AND event_type = 'reconciliation_decision_recorded'
              AND causation_public_id = ?""",
            (row["public_id"], row["decision_hash"]),
        ).fetchone()
        if event is None:
            raise ReconciliationRepositoryError(
                "Reconciliation decision integrity audit is missing"
            )
        state = _canonical_audit_value(event["new_state_json"])
        reasons = json.loads(row["reason_codes_json"])
        required_refs = {f"statement-row:{statement_row['public_id']}"}
        if statement_row["row_fingerprint"]:
            required_refs.add(f"row-fingerprint:{statement_row['row_fingerprint']}")
        if statement_row["source_content_hash"]:
            required_refs.add(f"source-content-sha256:{statement_row['source_content_hash']}")
        refs = set(json.loads(event["source_evidence_refs_json"]))
        if (
            state.get("decision_hash") != row["decision_hash"]
            or state.get("candidate_set_fingerprint") != row["candidate_set_fingerprint"]
            or state.get("match_status") != row["match_status"]
            or state.get("reason_codes") != reasons
            or state.get("authorization_public_id") != row["authorization_public_id"]
            or not required_refs.issubset(refs)
        ):
            raise ReconciliationRepositoryError(
                "Reconciliation decision integrity audit does not match relational state"
            )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _has_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    return any(
        str(row[1]) == column_name
        for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    )


def _serialize_evidence(match_result: MatchResult) -> str:
    """Serialize MatchEvidence into deterministic JSON."""
    ev = match_result.evidence
    payload: dict[str, Any] = {
        "statement_amount": str(ev.statement_amount) if ev.statement_amount is not None else None,
        "candidate_amount": str(ev.candidate_amount) if ev.candidate_amount is not None else None,
        "statement_currency": ev.statement_currency,
        "candidate_currency": ev.candidate_currency,
        "statement_txn_date": ev.statement_txn_date.isoformat() if ev.statement_txn_date else None,
        "statement_posted_date": ev.statement_posted_date.isoformat()
        if ev.statement_posted_date
        else None,
        "candidate_txn_date": ev.candidate_txn_date.isoformat() if ev.candidate_txn_date else None,
        "date_delta_days": ev.date_delta_days,
        "date_tolerance_days": ev.date_tolerance_days,
        "statement_merchant": ev.statement_merchant,
        "candidate_merchant": ev.candidate_merchant,
        "statement_merchant_normalized": ev.statement_merchant_normalized,
        "candidate_merchant_normalized": ev.candidate_merchant_normalized,
        "merchant_similarity": ev.merchant_similarity,
        "candidate_count": ev.candidate_count,
        "candidate_ids": [c.internal_id for c in match_result.candidates],
        "statement_direction": ev.statement_direction,
        "candidate_transaction_type": ev.candidate_transaction_type,
        "original_amount_text": ev.original_amount_text,
        "original_amount_sign": ev.original_amount_sign,
        "compatibility_result": ev.compatibility_result,
        "hard_gate_results": list(ev.hard_gate_results),
    }
    return json.dumps(payload, sort_keys=True)


def _canonical_audit_value(payload_json: str) -> dict[str, Any]:
    try:
        decoded = json.loads(payload_json)
        value = decoded["value"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReconciliationRepositoryError(
            "Authoritative integrity audit payload is malformed"
        ) from exc
    if not isinstance(value, dict):
        raise ReconciliationRepositoryError(
            "Authoritative integrity audit payload must contain an object"
        )
    return value


def _compute_amount_delta(match_result: MatchResult) -> Decimal | None:
    """Absolute amount difference when both statement and candidate amounts exist."""
    ev = match_result.evidence
    if ev.statement_amount is not None and ev.candidate_amount is not None:
        return abs(ev.statement_amount - ev.candidate_amount)
    return None


def _is_identical_decision_replay(
    existing: sqlite3.Row,
    *,
    run_id: int,
    statement_transaction_id: int,
    match_result: MatchResult,
) -> bool:
    """Require identity, foreign-key context, hash, and material to replay exactly."""
    return (
        match_result.decision_hash is not None
        and existing["run_id"] == run_id
        and existing["statement_transaction_id"] == statement_transaction_id
        and existing["decision_hash"] == match_result.decision_hash
        and existing["decision_material_json"] == match_result.decision_material_json
    )


def _row_to_match_result_record(row: sqlite3.Row) -> MatchResultRecord:
    """Convert a sqlite3.Row to a MatchResultRecord."""
    amount_delta_raw = row["amount_delta"]
    return MatchResultRecord(
        id=row["id"],
        public_id=row["public_id"],
        run_id=row["run_id"],
        statement_transaction_id=row["statement_transaction_id"],
        internal_candidate_id=row["internal_candidate_id"],
        match_status=row["match_status"],
        reason_codes_json=row["reason_codes_json"],
        evidence_json=row["evidence_json"],
        amount_delta=Decimal(str(amount_delta_raw)) if amount_delta_raw is not None else None,
        date_delta_days=row["date_delta_days"],
        merchant_similarity=row["merchant_similarity"],
        needs_review=bool(row["needs_review"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        decision_contract_version=row["decision_contract_version"],
        matcher_version=row["matcher_version"],
        compatibility_version=row["compatibility_version"],
        merchant_normalization_version=row["merchant_normalization_version"],
        candidate_set_fingerprint=row["candidate_set_fingerprint"],
        decision_hash=row["decision_hash"],
        decision_material_json=row["decision_material_json"],
        authorization_public_id=row["authorization_public_id"],
    )


__all__ = [
    "ReconciliationRepository",
    "ReconciliationRepositoryError",
    "DuplicatePublicIdError",
    "MatchResultRecord",
]
