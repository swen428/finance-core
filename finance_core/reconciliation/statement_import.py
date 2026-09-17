"""Statement Import Service v2 -- imports structured external statement rows
into the reconciliation persistence schema with an authoritative,
service-owned Unit of Work transaction.

The service receives a caller-owned ``sqlite3.Connection``, validates the
import command, opens one ``BEGIN IMMEDIATE`` transaction, coordinates
SQL-only repositories, persists the complete batch and all rows, and
commits exactly once on success or rolls back on every failure.

Repository methods execute SQL only.  They never begin, commit, or roll
back a transaction; the service is the sole transaction owner for the
complete statement-import command.

Design properties:
- Deterministic ``public_id`` strategy (no UUIDs, no random).
- Decimal-safe money handling.
- ``transaction_date`` and ``posted_date`` preserved separately.
- ``merchant_raw``, ``amount``, ``currency`` preserved from input.
- Source row reference / raw payload preserved as evidence.
- Duplicate imports (same source row re-imported) are safely rejected.
- Distinct same-day same-merchant same-amount rows are NOT collapsed;
  ``statement_row_reference`` / ``source_row_index`` disambiguates them.
- No live database access; never opens ``database/finance.db``.
- No mutation of unrelated tables.
- Complete batch-and-row atomicity: any failure rolls back the entire
  import command; no partial batch or partial rows survive.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Sequence

from typing_extensions import TypeAlias

from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.repository import (
    DuplicatePublicIdError,
    ReconciliationRepository,
)
from finance_core.reconciliation.statement_identity import (
    AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS,
    CALLER_SUPPLIED_ROW_FINGERPRINT_VERSION,
    ROW_FINGERPRINT_VERSION,
    STATEMENT_IMPORT_CONTRACT_VERSION,
    canonical_statement_row_fingerprint,
    derive_batch_public_id,
    derive_statement_row_public_id,
    import_command_hash,
    read_source_file_evidence,
    require_sha256,
    row_set_fingerprint,
    source_evidence_observation_hash,
    stable_row_fingerprint_evidence,
)
from finance_core.reconciliation.statement_import_contracts import (
    StatementImportBatch,
    StatementImportRow,
    StatementSourceIdentity,
    StructuredStatementRow,
)
from finance_core.staging_guard import require_staging_database

# ---------------------------------------------------------------------------
# Import error
# ---------------------------------------------------------------------------


class StatementImportError(ValueError):
    """Base error for authoritative statement import failures."""


class StatementImportTransactionError(StatementImportError):
    """Raised when the supplied connection is already inside a transaction."""


# ---------------------------------------------------------------------------
# Test-only hook signatures (not part of the public financial API)
# ---------------------------------------------------------------------------

_PostBatchHook: TypeAlias = Callable[[], None] | None
_PreCommitHook: TypeAlias = Callable[[], None] | None

# ---------------------------------------------------------------------------
# Import service
# ---------------------------------------------------------------------------


class StatementImporter:
    """Imports structured statement rows into the reconciliation schema.

    The importer owns the write transaction: it validates the import
    command, opens one ``BEGIN IMMEDIATE``, coordinates SQL-only
    repositories, and commits once on success or rolls back on every
    failure.  No partial batch or partial rows survive.

    Usage::

        conn = connect_sqlite(":memory:")
        # migrate schema first
        importer = StatementImporter(conn)
        batch = importer.import_rows(
            rows,
            source_type="bank_statement",
            public_id="my-batch-2024-12",
        )
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        _test_post_batch_hook: _PostBatchHook | None = None,
        _test_pre_commit_hook: _PreCommitHook | None = None,
    ) -> None:
        self._repo = ReconciliationRepository(conn)
        self._conn = conn
        # Test-only hooks: default no-op, never production paths.
        # _test_post_batch_hook: called once after batch INSERT, before any row.
        # _test_pre_commit_hook: called after all rows are persisted, before COMMIT.
        self._post_batch_hook = _test_post_batch_hook
        self._pre_commit_hook = _test_pre_commit_hook

    def import_rows(
        self,
        rows: Sequence[StructuredStatementRow] | list[StatementImportRow],
        *,
        source_type: str,
        public_id: str | None = None,
        account_id: int | None = None,
        account_name: str | None = None,
        statement_period_start: str | None = None,
        statement_period_end: str | None = None,
        currency: str | None = None,
        source_file_path: str | None = None,
        source_file_hash: str | None = None,
        source_filename: str | None = None,
        import_contract_version: str = STATEMENT_IMPORT_CONTRACT_VERSION,
        actor_type: str = "system",
        actor_public_id: str = "statement-importer",
        authorization_public_id: str | None = None,
    ) -> StatementImportBatch:
        """Import a sequence of structured statement rows as a single batch.

        Accepts either ``StructuredStatementRow`` instances or raw dicts
        with the standard column contract.

        Each row receives a stable ``source_row_index`` (1-based) derived
        from its position in the input sequence.  This index is used in
        public_id derivation to distinguish rows that are otherwise
        identical on merchant/amount/currency/date.

        The entire import -- batch header and all rows -- runs inside one
        service-owned ``BEGIN IMMEDIATE`` transaction.  Any failure rolls
        back the complete command; no partial batch or rows survive.

        Parameters
        ----------
        rows:
            Structured rows to import.
        source_type:
            One of ``"bank_statement"``, ``"credit_card_statement"``,
            ``"structured_csv"``, etc.
        public_id:
            Deterministic public_id for the batch.  If ``None``, one is
            derived from the versioned canonical import command.

        Returns
        -------
        StatementImportBatch with batch metadata and inserted row ids.
        """
        if not rows:
            raise ValueError("rows must not be empty")

        # --- Pre-transaction validation ---------------------------------
        require_staging_database(self._conn)

        if not import_contract_version.strip():
            raise StatementImportError("import_contract_version must not be empty")

        fingerprint_source_hashes = _row_source_content_hashes(rows)
        if len(fingerprint_source_hashes) > 1:
            raise StatementImportError(
                "Statement rows declare conflicting fingerprint source content hashes"
            )
        fingerprint_source_hash = next(iter(fingerprint_source_hashes), None)

        file_evidence = None
        authoritative_source_hash: str | None
        preserved_filename: str | None
        if source_file_path is not None:
            file_evidence = read_source_file_evidence(
                source_file_path,
                expected_hash=source_file_hash,
            )
            authoritative_source_hash = file_evidence.content_hash
            preserved_filename = file_evidence.original_filename
            hash_status = "verified_from_bytes"
        elif source_file_hash is not None:
            authoritative_source_hash = require_sha256(
                source_file_hash,
                field="source_file_hash",
            )
            preserved_filename = source_filename
            hash_status = "provided_unverified"
        elif fingerprint_source_hash is not None:
            authoritative_source_hash = fingerprint_source_hash
            preserved_filename = source_filename
            hash_status = "provided_unverified"
        else:
            authoritative_source_hash = None
            preserved_filename = source_filename
            hash_status = "unverified_no_source_bytes"

        if (
            fingerprint_source_hash is not None
            and fingerprint_source_hash != authoritative_source_hash
        ):
            raise StatementImportError(
                "Row fingerprint source content hash does not match statement source bytes"
            )

        # Build source identity for row public_id derivation (validated
        # before we open the transaction).
        source_identity: StatementSourceIdentity = {
            "source_type": source_type,
            "source_file_hash": authoritative_source_hash,
            "source_file_path": source_file_path,
            "source_filename": preserved_filename,
            "batch_public_id": public_id,
            "import_contract_version": import_contract_version,
            "account_id": str(account_id) if account_id is not None else None,
            "account_name": account_name,
            "statement_period_start": statement_period_start,
            "statement_period_end": statement_period_end,
        }

        # Normalize rows and assign stable source_row_index before the
        # transaction so that validation errors are raised cleanly.
        normalized = _rows_to_import_dicts(rows, source_identity)
        normalized_fingerprints = [str(row["row_fingerprint"]) for row in normalized]
        if len(set(normalized_fingerprints)) != len(normalized_fingerprints):
            raise StatementImportError("Import command contains a duplicate statement row identity")
        row_set_hash = row_set_fingerprint(
            [
                (
                    str(row["row_fingerprint"]),
                    str(row["row_fingerprint_version"]),
                )
                for row in normalized
            ]
        )
        command_hash = import_command_hash(
            {
                "import_contract_version": import_contract_version,
                "source_type": source_type,
                "source_content_hash": authoritative_source_hash,
                "account_id": account_id,
                "account_name": account_name,
                "statement_period_start": statement_period_start,
                "statement_period_end": statement_period_end,
                "currency": currency,
                "row_set_fingerprint": row_set_hash,
            }
        )
        requested_batch_public_id = public_id or derive_batch_public_id(command_hash)
        source_identity["batch_public_id"] = requested_batch_public_id

        # --- Service-owned transaction ----------------------------------
        _begin_immediate(self._conn)
        try:
            # 1. Create or reuse the batch.
            batch_preexisted = False
            existing_by_public_id = self._repo.get_statement_import_batch_by_public_id(
                requested_batch_public_id
            )
            existing_by_command = self._repo.get_statement_import_batch_by_command_hash(
                command_hash
            )
            existing_source_owner = (
                self._repo.get_authoritative_statement_batch_by_source_hash(
                    authoritative_source_hash
                )
                if authoritative_source_hash is not None
                else None
            )
            if (
                existing_source_owner is not None
                and existing_source_owner["import_command_hash"] != command_hash
            ):
                raise DuplicatePublicIdError(
                    "Statement source content already belongs to a different import command: "
                    f"{authoritative_source_hash}"
                )
            if (
                existing_by_public_id is not None
                and existing_by_public_id["import_command_hash"] != command_hash
            ):
                raise DuplicatePublicIdError(
                    f"Conflicting statement import batch public_id: {requested_batch_public_id}"
                )
            existing_batch = existing_by_command or existing_by_public_id
            if existing_batch is not None:
                batch_preexisted = True
                if not _batch_metadata_matches(
                    existing_batch,
                    source_type=source_type,
                    account_id=account_id,
                    account_name=account_name,
                    statement_period_start=statement_period_start,
                    statement_period_end=statement_period_end,
                    currency=currency,
                    source_file_hash=authoritative_source_hash,
                    import_contract_version=import_contract_version,
                    import_command_hash=command_hash,
                    row_set_fingerprint=row_set_hash,
                ):
                    raise DuplicatePublicIdError(
                        f"Conflicting statement import command: {command_hash}"
                    )
                batch_id = int(existing_batch["id"])
                canonical_batch_public_id = str(existing_batch["public_id"])
                source_identity["source_file_path"] = existing_batch["source_file_path"]
                source_identity["source_filename"] = existing_batch["source_filename"]
            else:
                batch_id = self._repo.create_statement_import_batch(
                    public_id=requested_batch_public_id,
                    source_type=source_type,
                    account_id=account_id,
                    account_name=account_name,
                    statement_period_start=statement_period_start,
                    statement_period_end=statement_period_end,
                    currency=currency,
                    source_file_path=source_file_path,
                    source_file_hash=authoritative_source_hash,
                    source_filename=preserved_filename,
                    source_hash_verification_status=hash_status,
                    import_contract_version=import_contract_version,
                    import_command_hash=command_hash,
                    row_set_fingerprint=row_set_hash,
                )
                canonical_batch_public_id = requested_batch_public_id

            if file_evidence is not None:
                self._repo.record_statement_import_source_evidence(
                    batch_id=batch_id,
                    source_content_hash=file_evidence.content_hash,
                    evidence_path=file_evidence.evidence_path,
                    original_filename=file_evidence.original_filename,
                )

            # 2. Test-only injection: after batch INSERT, before any row.
            if self._post_batch_hook is not None:
                self._post_batch_hook()

            # 3. Persist every accepted row through SQL-only repositories.
            idempotent: int = 0
            inserted_ids: list[int] = []
            skipped: int = 0
            for nrow in normalized:
                fp = nrow.get("row_fingerprint")
                if fp:
                    existing_by_fp = self._repo.get_statement_transaction_by_batch_and_fingerprint(
                        batch_id,
                        fp,
                    )
                    if existing_by_fp is not None:
                        if _statement_transaction_matches(
                            existing_by_fp,
                            nrow,
                            batch_id=batch_id,
                            include_public_id=False,
                            include_statement_reference=True,
                        ):
                            idempotent += 1
                            continue
                        raise DuplicatePublicIdError(
                            f"Conflicting statement transaction row_fingerprint: {fp}"
                        )
                try:
                    ids = self._repo.create_statement_transactions(batch_id, [nrow])
                    inserted_ids.extend(ids)
                except DuplicatePublicIdError:
                    existing_by_public_id = self._repo.get_statement_transaction_by_public_id(
                        nrow["public_id"]
                    )
                    if existing_by_public_id is not None:
                        if int(existing_by_public_id["batch_id"]) != batch_id:
                            raise DuplicatePublicIdError(
                                "Statement row identity is already owned by another batch: "
                                f"{nrow['public_id']}"
                            )
                        if _statement_transaction_matches(
                            existing_by_public_id,
                            nrow,
                            batch_id=batch_id,
                        ):
                            idempotent += 1
                            continue
                    raise DuplicatePublicIdError(
                        f"Conflicting statement transaction public_id: {nrow['public_id']}"
                    )

            # 4. Append the accepted batch audit in this same transaction.
            _append_statement_import_audit(
                self._conn,
                batch_public_id=canonical_batch_public_id,
                source_identity=source_identity,
                normalized_rows=normalized,
                import_command_hash_value=command_hash,
                row_set_fingerprint_value=row_set_hash,
                actor_type=actor_type,
                actor_public_id=actor_public_id,
                authorization_public_id=authorization_public_id,
                legacy_unchanged_replay=batch_preexisted and not inserted_ids,
            )
            if file_evidence is not None:
                _append_statement_source_evidence_audit(
                    self._conn,
                    batch_public_id=canonical_batch_public_id,
                    import_command_hash_value=command_hash,
                    source_content_hash=file_evidence.content_hash,
                    evidence_path=file_evidence.evidence_path,
                    original_filename=file_evidence.original_filename,
                    actor_type=actor_type,
                    actor_public_id=actor_public_id,
                    authorization_public_id=authorization_public_id,
                )

            owned_rows = self._repo.verify_statement_import_batch(batch_id)
            owned_row_ids = [int(row["id"]) for row in owned_rows]
            if len(owned_row_ids) != len(normalized):
                raise StatementImportError(
                    "Accepted statement batch ownership does not match the requested row set"
                )

            # 5. Test-only injection: after all rows and audit, before COMMIT.
            if self._pre_commit_hook is not None:
                self._pre_commit_hook()

            # 6. Commit the complete import command.
            self._conn.commit()

        except Exception:
            _rollback_if_needed(self._conn)
            raise

        return StatementImportBatch(
            batch_id=batch_id,
            public_id=canonical_batch_public_id,
            source_type=source_type,
            row_count=len(rows),
            inserted_ids=inserted_ids,
            owned_row_ids=owned_row_ids,
            skipped_duplicates=skipped,
            idempotent_count=idempotent,
            source_content_hash=authoritative_source_hash,
            row_set_fingerprint=row_set_hash,
            import_command_hash=command_hash,
        )

    # ------------------------------------------------------------------
    # Batch public_id helpers
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public row-id helper
# ---------------------------------------------------------------------------


def derive_row_public_id(
    merchant_raw: str,
    amount: Decimal,
    currency: str,
    transaction_date: date | None,
    *,
    posted_date: date | None = None,
    statement_row_reference: str | None = None,
    source_row_index: int | None = None,
    source_type: str | None = None,
    source_file_hash: str | None = None,
    source_file_path: str | None = None,
    batch_public_id: str | None = None,
    account_id: str | None = None,
    account_name: str | None = None,
    statement_period_start: str | None = None,
    statement_period_end: str | None = None,
) -> str:
    """Derive a path-independent row public ID from canonical material.

    ``source_file_path`` is accepted for API compatibility but is evidence
    only and is deliberately excluded from identity.
    """
    del source_file_path
    material = {
        "identity_version": "statement-row-public-id-v2",
        "merchant_raw": merchant_raw,
        "amount": str(amount),
        "currency": currency,
        "transaction_date": transaction_date.isoformat() if transaction_date else None,
        "posted_date": posted_date.isoformat() if posted_date else None,
        "statement_row_reference": statement_row_reference,
        "source_row_index": source_row_index,
        "source_type": source_type,
        "source_content_hash": source_file_hash,
        "legacy_batch_public_id": batch_public_id if source_file_hash is None else None,
        "account_id": account_id,
        "account_name": account_name,
        "statement_period_start": statement_period_start,
        "statement_period_end": statement_period_end,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"stmt-{digest}"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _begin_immediate(conn: sqlite3.Connection) -> None:
    """Open an exclusive write transaction.

    Fails closed when the caller-supplied connection is already inside a
    transaction.  The import service is the sole transaction owner.
    """
    if conn.in_transaction:
        raise StatementImportTransactionError(
            "Statement import requires a connection without pending work. "
            "The import service owns the transaction."
        )
    conn.execute("BEGIN IMMEDIATE")


def _rollback_if_needed(conn: sqlite3.Connection) -> None:
    """Roll back a pending service-owned transaction (no-op otherwise)."""
    if conn.in_transaction:
        conn.rollback()


def _append_statement_import_audit(
    conn: sqlite3.Connection,
    *,
    batch_public_id: str,
    source_identity: StatementSourceIdentity,
    normalized_rows: list[StatementImportRow],
    import_command_hash_value: str,
    row_set_fingerprint_value: str,
    actor_type: str,
    actor_public_id: str,
    authorization_public_id: str | None,
    legacy_unchanged_replay: bool,
) -> None:
    event_type = "statement_import_accepted"
    event_id = derive_audit_event_public_id(
        aggregate_type="statement_import_batch",
        aggregate_public_id=batch_public_id,
        event_type=event_type,
        causation_public_id=import_command_hash_value,
    )
    if legacy_unchanged_replay:
        return
    row_public_ids = tuple(sorted(str(row["public_id"]) for row in normalized_rows))
    row_fingerprints = tuple(
        sorted(str(row["row_fingerprint"]) for row in normalized_rows if row.get("row_fingerprint"))
    )
    pdf_row_evidence = _pdf_row_audit_evidence(normalized_rows)
    external_row_evidence = _external_row_audit_evidence(normalized_rows)
    evidence = {f"statement-batch:{batch_public_id}"}
    if source_identity.get("source_file_hash"):
        evidence.add(f"source-file-sha256:{source_identity['source_file_hash']}")
    if source_identity.get("source_file_path"):
        evidence.add(f"source-file-path:{source_identity['source_file_path']}")
    if source_identity.get("source_filename"):
        evidence.add(f"source-filename:{source_identity['source_filename']}")
    for row in normalized_rows:
        if row.get("statement_row_reference"):
            evidence.add(f"statement-row:{row['statement_row_reference']}")
        payload = _decode_row_payload(row.get("raw_row_payload_json"))
        if (
            isinstance(payload, dict)
            and payload.get("evidence_contract_version") == "pdf-row-evidence-v2"
        ):
            if payload.get("source_page_number") is not None:
                evidence.add(f"pdf-page:{payload['source_page_number']}")
            if payload.get("stable_row_locator"):
                evidence.add(f"pdf-row:{payload['stable_row_locator']}")
    accepted_state = {
        "status": "accepted",
        "row_public_ids": row_public_ids,
        "row_fingerprints": row_fingerprints,
        "row_set_fingerprint": row_set_fingerprint_value,
        "import_command_hash": import_command_hash_value,
        "pdf_row_evidence": pdf_row_evidence,
        "external_row_evidence": external_row_evidence,
    }
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="statement_import_batch",
            aggregate_public_id=batch_public_id,
            event_type=event_type,
            event_payload={
                "source_type": source_identity["source_type"],
                "source_file_hash": source_identity.get("source_file_hash"),
                "source_filename": source_identity.get("source_filename"),
                "import_contract_version": source_identity["import_contract_version"],
                "import_command_hash": import_command_hash_value,
                "row_set_fingerprint": row_set_fingerprint_value,
                "account_id": source_identity.get("account_id"),
                "account_name": source_identity.get("account_name"),
                "statement_period_start": source_identity.get("statement_period_start"),
                "statement_period_end": source_identity.get("statement_period_end"),
                "row_count": len(normalized_rows),
                "row_public_ids": row_public_ids,
                "pdf_row_evidence": pdf_row_evidence,
                "external_row_evidence": external_row_evidence,
            },
            previous_state=None,
            new_state=accepted_state,
            actor_type=actor_type,
            actor_public_id=actor_public_id,
            authorization_public_id=authorization_public_id,
            source_evidence_references=tuple(evidence),
            correlation_public_id=batch_public_id,
            causation_public_id=import_command_hash_value,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


def _pdf_row_audit_evidence(
    normalized_rows: list[StatementImportRow],
) -> tuple[dict[str, object], ...]:
    """Return complete versioned PDF evidence for the accepted audit event."""
    evidence_rows: list[dict[str, object]] = []
    for row in normalized_rows:
        payload = _decode_row_payload(row.get("raw_row_payload_json"))
        if not isinstance(payload, dict):
            continue
        if payload.get("evidence_contract_version") != "pdf-row-evidence-v2":
            continue
        evidence_rows.append(
            {
                "row_public_id": str(row["public_id"]),
                "row_fingerprint": str(row["row_fingerprint"]),
                "evidence": payload,
            }
        )
    return tuple(sorted(evidence_rows, key=lambda item: str(item["row_fingerprint"])))


def _external_row_audit_evidence(
    normalized_rows: list[StatementImportRow],
) -> tuple[dict[str, str], ...]:
    """Retain caller/adapter digests as evidence, never canonical authority."""
    evidence_rows = [
        {
            "row_public_id": str(row["public_id"]),
            "external_row_fingerprint": str(row["external_row_fingerprint"]),
            "external_row_fingerprint_version": str(row["external_row_fingerprint_version"]),
        }
        for row in normalized_rows
        if row.get("external_row_fingerprint") is not None
    ]
    return tuple(
        sorted(
            evidence_rows,
            key=lambda item: (
                item["external_row_fingerprint"],
                item["external_row_fingerprint_version"],
                item["row_public_id"],
            ),
        )
    )


def _append_statement_source_evidence_audit(
    conn: sqlite3.Connection,
    *,
    batch_public_id: str,
    import_command_hash_value: str,
    source_content_hash: str,
    evidence_path: str,
    original_filename: str,
    actor_type: str,
    actor_public_id: str,
    authorization_public_id: str | None,
) -> None:
    observation_hash = source_evidence_observation_hash(
        import_command_hash_value=import_command_hash_value,
        source_content_hash=source_content_hash,
        evidence_path=evidence_path,
        original_filename=original_filename,
    )
    event_type = "statement_import_source_evidence_observed"
    event_id = derive_audit_event_public_id(
        aggregate_type="statement_import_batch",
        aggregate_public_id=batch_public_id,
        event_type=event_type,
        causation_public_id=observation_hash,
    )
    state = {
        "status": "observed",
        "import_command_hash": import_command_hash_value,
        "source_content_hash": source_content_hash,
        "evidence_path": evidence_path,
        "original_filename": original_filename,
        "observation_hash": observation_hash,
    }
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="statement_import_batch",
            aggregate_public_id=batch_public_id,
            event_type=event_type,
            event_payload=state,
            previous_state=None,
            new_state=state,
            actor_type=actor_type,
            actor_public_id=actor_public_id,
            authorization_public_id=authorization_public_id,
            source_evidence_references=(
                f"statement-batch:{batch_public_id}",
                f"source-file-sha256:{source_content_hash}",
                f"source-file-path:{evidence_path}",
                f"source-filename:{original_filename}",
            ),
            correlation_public_id=batch_public_id,
            causation_public_id=observation_hash,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


def _to_import_dict(
    row: StructuredStatementRow,
    source_row_index: int,
    source_identity: StatementSourceIdentity,
) -> StatementImportRow:
    """Convert a structured row to a dict suitable for the repository."""
    source_row_locator = row.statement_row_reference or f"source-row-{source_row_index}"
    canonical_fingerprint = canonical_statement_row_fingerprint(
        source_content_hash=source_identity.get("source_file_hash"),
        import_contract_version=source_identity["import_contract_version"],
        source_row_locator=source_row_locator,
        transaction_date=row.transaction_date.isoformat() if row.transaction_date else None,
        posted_date=row.posted_date.isoformat() if row.posted_date else None,
        original_amount=row.raw_amount,
        normalized_amount=row.amount,
        currency=row.currency,
        direction=row.amount_direction.value if row.amount_direction else None,
        raw_amount_type=row.raw_amount_type,
        merchant_raw=row.merchant_raw,
        merchant_normalized=row.merchant_normalized,
        account_id=row.account_id,
        account_name=row.account_name,
        statement_row_reference=row.statement_row_reference,
        raw_row_payload=row.raw_row_payload,
    )
    fingerprint = canonical_fingerprint
    fingerprint_version = ROW_FINGERPRINT_VERSION
    external_fingerprint = row.external_row_fingerprint
    external_fingerprint_version = row.external_row_fingerprint_version
    if row.row_fingerprint is not None:
        supplied_version = row.row_fingerprint_version
        if supplied_version is None or supplied_version == CALLER_SUPPLIED_ROW_FINGERPRINT_VERSION:
            if external_fingerprint is not None:
                raise StatementImportError(
                    "Caller fingerprint was supplied in both authoritative and external fields"
                )
            external_fingerprint = row.row_fingerprint
            external_fingerprint_version = (
                supplied_version or CALLER_SUPPLIED_ROW_FINGERPRINT_VERSION
            )
        else:
            if supplied_version not in AUTHORITATIVE_ROW_FINGERPRINT_VERSIONS:
                raise StatementImportError(
                    f"Unsupported authoritative row_fingerprint_version: {supplied_version!r}"
                )
            fingerprint = row.row_fingerprint
            fingerprint_version = supplied_version
            if (
                fingerprint_version == ROW_FINGERPRINT_VERSION
                and fingerprint != canonical_fingerprint
            ):
                raise StatementImportError(
                    "Supplied canonical row fingerprint does not match row material"
                )
    require_sha256(fingerprint, field="row_fingerprint")
    return {
        "public_id": derive_statement_row_public_id(
            fingerprint,
            source_identity.get("source_file_hash"),
        ),
        "merchant_raw": row.merchant_raw,
        "amount": str(row.amount),
        "currency": row.currency,
        "transaction_date": row.transaction_date.isoformat() if row.transaction_date else None,
        "posted_date": row.posted_date.isoformat() if row.posted_date else None,
        "merchant_normalized": row.merchant_normalized,
        "account_id": row.account_id,
        "account_name": row.account_name,
        "statement_row_reference": row.statement_row_reference,
        "row_fingerprint": fingerprint,
        "row_fingerprint_version": fingerprint_version,
        "external_row_fingerprint": external_fingerprint,
        "external_row_fingerprint_version": external_fingerprint_version,
        "raw_row_payload_json": json.dumps(row.raw_row_payload, sort_keys=True)
        if row.raw_row_payload is not None
        else None,
        "amount_direction": (
            row.amount_direction.value if row.amount_direction is not None else None
        ),
        "raw_amount": row.raw_amount,
        "raw_amount_type": row.raw_amount_type,
    }


def _batch_metadata_matches(
    existing_batch: sqlite3.Row,
    *,
    source_type: str,
    account_id: int | None,
    account_name: str | None,
    statement_period_start: str | None,
    statement_period_end: str | None,
    currency: str | None,
    source_file_hash: str | None,
    import_contract_version: str,
    import_command_hash: str,
    row_set_fingerprint: str,
) -> bool:
    """Return True only when a duplicate batch public_id is the same source."""
    expected: dict[str, object] = {
        "source_type": source_type,
        "account_id": account_id,
        "account_name": account_name,
        "statement_period_start": statement_period_start,
        "statement_period_end": statement_period_end,
        "currency": currency,
        "source_file_hash": source_file_hash,
        "import_contract_version": import_contract_version,
        "import_command_hash": import_command_hash,
        "row_set_fingerprint": row_set_fingerprint,
    }
    return all(existing_batch[column] == value for column, value in expected.items())


def _statement_transaction_matches(
    existing: sqlite3.Row,
    row: dict[str, Any],
    *,
    batch_id: int | None,
    include_public_id: bool = True,
    include_statement_reference: bool = True,
) -> bool:
    """Return True only when a duplicate statement row is an idempotent replay."""
    amount_raw = row["amount"]
    expected_amount = amount_raw if isinstance(amount_raw, Decimal) else Decimal(str(amount_raw))
    existing_amount = Decimal(str(existing["amount"]))
    if existing_amount != expected_amount:
        return False

    existing_payload = _decode_row_payload(existing["raw_row_payload_json"])
    requested_payload = _decode_row_payload(row.get("raw_row_payload_json"))
    if stable_row_fingerprint_evidence(existing_payload) != stable_row_fingerprint_evidence(
        requested_payload
    ):
        return False

    expected: dict[str, object] = {
        "transaction_date": row.get("transaction_date"),
        "posted_date": row.get("posted_date"),
        "merchant_raw": row["merchant_raw"],
        "merchant_normalized": row.get("merchant_normalized"),
        "currency": row["currency"],
        "account_id": row.get("account_id"),
        "account_name": row.get("account_name"),
        "amount_direction": row.get("amount_direction"),
        "raw_amount": row.get("raw_amount"),
        "raw_amount_type": row.get("raw_amount_type"),
        "row_fingerprint": row.get("row_fingerprint"),
        "row_fingerprint_version": row.get("row_fingerprint_version"),
    }
    if "external_row_fingerprint" in existing.keys():
        expected["external_row_fingerprint"] = row.get("external_row_fingerprint")
        expected["external_row_fingerprint_version"] = row.get("external_row_fingerprint_version")
    if batch_id is not None:
        expected["batch_id"] = batch_id
    if include_public_id:
        expected["public_id"] = row["public_id"]
    if include_statement_reference:
        expected["statement_row_reference"] = row.get("statement_row_reference")
    return all(_sqlite_value_matches(existing[column], value) for column, value in expected.items())


def _decode_row_payload(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _sqlite_value_matches(existing: object, expected: object) -> bool:
    """Compare SQLite values with mild type normalization for nullable fields."""
    if existing is None or expected is None:
        return existing is None and expected is None
    return str(existing) == str(expected)


def _parse_direction(
    value: str | StatementAmountDirection | None,
) -> StatementAmountDirection | None:
    """Parse direction without converting invalid evidence to ``None``."""
    if value is None:
        return None
    if isinstance(value, StatementAmountDirection):
        return value
    try:
        return StatementAmountDirection(value.strip().lower())
    except (AttributeError, ValueError) as exc:
        raise StatementImportError(f"Invalid statement amount direction: {value!r}") from exc


def _dict_to_structured(d: StatementImportRow) -> StructuredStatementRow:
    """Parse a raw dict into a ``StructuredStatementRow``."""
    amount = d["amount"]
    if isinstance(amount, str):
        amount = Decimal(amount)
    txn_date = d.get("transaction_date")
    if isinstance(txn_date, str):
        txn_date = date.fromisoformat(txn_date)
    posted_date = d.get("posted_date")
    if isinstance(posted_date, str):
        posted_date = date.fromisoformat(posted_date)
    return StructuredStatementRow(
        merchant_raw=d["merchant_raw"],
        amount=amount,
        currency=d["currency"],
        transaction_date=txn_date,
        posted_date=posted_date,
        merchant_normalized=d.get("merchant_normalized"),
        account_name=d.get("account_name"),
        account_id=d.get("account_id"),
        statement_row_reference=d.get("statement_row_reference"),
        raw_row_payload=d.get("raw_row_payload"),
        row_fingerprint=d.get("row_fingerprint"),
        row_fingerprint_version=d.get("row_fingerprint_version"),
        external_row_fingerprint=d.get("external_row_fingerprint"),
        external_row_fingerprint_version=d.get("external_row_fingerprint_version"),
        fingerprint_source_content_hash=d.get("fingerprint_source_content_hash"),
        amount_direction=_parse_direction(d.get("amount_direction")),
        raw_amount=d.get("raw_amount"),
        raw_amount_type=d.get("raw_amount_type"),
    )


def _rows_to_import_dicts(
    rows: Sequence[StructuredStatementRow] | list[StatementImportRow],
    source_identity: StatementSourceIdentity,
) -> list[StatementImportRow]:
    """Normalize a mixed sequence of rows into import-ready dicts.

    Each row receives a stable ``source_row_index`` (1-based) from its
    position in the sequence so the public_id derivation can distinguish
    rows without explicit ``statement_row_reference``.
    """
    result: list[StatementImportRow] = []
    for idx, row in enumerate(rows, start=1):
        if isinstance(row, dict):
            srow = _dict_to_structured(row)
        elif isinstance(row, StructuredStatementRow):
            srow = row
        else:
            raise TypeError(f"Unexpected row type: {type(row)}")
        result.append(_to_import_dict(srow, source_row_index=idx, source_identity=source_identity))
    return result


def _row_source_content_hashes(
    rows: Sequence[StructuredStatementRow] | list[StatementImportRow],
) -> set[str]:
    hashes: set[str] = set()
    for row in rows:
        value = (
            row.get("fingerprint_source_content_hash")
            if isinstance(row, dict)
            else row.fingerprint_source_content_hash
        )
        if value is not None:
            hashes.add(require_sha256(value, field="fingerprint_source_content_hash"))
    return hashes


__all__ = [
    "StatementImportTransactionError",
    "StatementImporter",
    "StatementImportBatch",
    "StructuredStatementRow",
    "StatementAmountDirection",
    "derive_row_public_id",
]
