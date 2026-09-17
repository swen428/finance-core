"""B4.1 conversion idempotency, concurrency, failure injection, and DB safety.

Covers design Sections 8.4 (idempotency and concurrency), 8.5 (failure
injection and rollback), and 8.7 (database safety) of
``docs/design/receipt_proposal_to_facts_conversion_v1.md``.  Guard, lineage,
and mutual-exclusion coverage lives in ``test_receipt_facts_conversion_v1.py``,
whose fixture helpers are reused here.

All fixtures are synthetic and privacy-safe.  The live database is only ever
opened read-only to prove the staging guard rejects it; no live or seed data
is touched.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

import finance_core.parser_proposals.receipt_facts_conversion as conversion_module
from finance_core.financial_audit import AuditChainTransactionError
from finance_core.parser_proposals import confirm_proposal, supersede_receipt_total_proposal
from finance_core.parser_proposals.receipt_facts_conversion import (
    ConversionCallerOwnedTransactionError,
    ConversionEvidenceLineageError,
    ConversionForeignKeysDisabledError,
    ConversionIdempotencyConflictError,
    ConversionPersistenceError,
    ConversionStagingDatabaseRejectedError,
    ReceiptFactsAlreadyConvertedError,
    ReceiptFactsConversionCommand,
    StaleConfirmationHashError,
    derive_receipt_public_id,
)
from finance_core.sqlite_connection import ForeignKeysDisabledError
from tests.conftest import LIVE_DB_PATH, connect_temp_db
from tests.test_receipt_facts_conversion_v1 import (
    _sha,
    command,
    convert,
    count_diff,
    entries,
    evidence_rows,
    expected_conversion_diff,
    hash_of,
    participant_id,
    seed_confirmed_receipt_proposal,
    seed_people,
    seed_receipt_proposal,
    table_counts,
)

# ---------------------------------------------------------------------------
# Local fixture helpers
# ---------------------------------------------------------------------------


def insert_manual_receipt(
    conn: sqlite3.Connection,
    public_id: str,
    *,
    parser_output_id: int | None = None,
) -> int:
    """Insert a minimal schema-legal receipts row outside the conversion path."""
    cursor = conn.execute(
        "INSERT INTO receipts ("
        "  public_id, merchant, net_paid_amount, currency,"
        "  payer_participant_id, parser_output_id"
        ") VALUES (?, 'MANUAL FIXTURE', '9.99', 'SGD', ?, ?)",
        (public_id, participant_id(conn, "person_owner"), parser_output_id),
    )
    lastrowid = cursor.lastrowid
    assert lastrowid is not None
    return int(lastrowid)


def forge_registry_row(
    conn: sqlite3.Connection,
    *,
    command_public_id: str,
    parser_output_id: int,
    root_parser_output_id: int,
    receipt_id: int,
    confirmation_public_id: str,
) -> None:
    """Directly insert a registry row (schema-backstop fixture SQL only)."""
    conn.execute(
        "INSERT INTO receipt_proposal_conversions ("
        "  command_public_id, parser_output_id,"
        "  supersession_root_parser_output_id, receipt_id,"
        "  confirmation_public_id, proposal_content_hash,"
        "  command_material_hash, conversion_result_hash, actor_type,"
        "  authenticated_actor_id, conversion_channel"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'human', 'owner', 'cli')",
        (
            command_public_id,
            parser_output_id,
            root_parser_output_id,
            receipt_id,
            confirmation_public_id,
            _sha(f"forged-proposal:{command_public_id}"),
            _sha(f"forged-material:{command_public_id}"),
            _sha(f"forged-result:{command_public_id}"),
        ),
    )


def result_fields(result: object) -> dict[str, object]:
    return {
        "command_public_id": result.command_public_id,  # type: ignore[attr-defined]
        "proposal_public_id": result.proposal_public_id,  # type: ignore[attr-defined]
        "parser_output_id": result.parser_output_id,  # type: ignore[attr-defined]
        "receipt_public_id": result.receipt_public_id,  # type: ignore[attr-defined]
        "receipt_id": result.receipt_id,  # type: ignore[attr-defined]
        "confirmation_public_id": result.confirmation_public_id,  # type: ignore[attr-defined]
        "proposal_content_hash": result.proposal_content_hash,  # type: ignore[attr-defined]
        "command_material_hash": result.command_material_hash,  # type: ignore[attr-defined]
        "conversion_result_hash": result.conversion_result_hash,  # type: ignore[attr-defined]
    }


# ---------------------------------------------------------------------------
# 8.4 Idempotency: exact replay
# ---------------------------------------------------------------------------


def test_exact_replay_returns_recorded_result(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "replay")
    cmd = command("replay", public_id, expected)

    first = convert(conn, cmd)
    assert first.idempotent is False
    after_first = table_counts(conn)

    second = convert(conn, cmd)
    assert second.idempotent is True
    assert result_fields(second) == result_fields(first)
    assert table_counts(conn) == after_first
    audit_events = conn.execute(
        "SELECT COUNT(*) FROM financial_audit_events WHERE causation_public_id = 'rpfc_replay'"
    ).fetchone()[0]
    assert audit_events == 1


def test_replay_survives_reconnect(migrated_temp_db_path: Path, tmp_path: Path) -> None:
    conn = connect_temp_db(migrated_temp_db_path)
    try:
        seed_people(conn)
        _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "reconn")
        cmd = command("reconn", public_id, expected)
        first = convert(conn, cmd)
        assert first.idempotent is False
    finally:
        conn.close()

    reopened = connect_temp_db(migrated_temp_db_path)
    try:
        replayed = convert(reopened, cmd)
        assert replayed.idempotent is True
        assert result_fields(replayed) == result_fields(first)
    finally:
        reopened.close()


def test_replay_after_later_state_change_never_recomputes(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "drift")
    cmd = command("drift", public_id, expected)
    first = convert(conn, cmd)

    # Fixture surgery: drift the effective payload after conversion.  Replay
    # must return the recorded result and never recompute the chain state.
    conn.execute(
        "UPDATE parser_outputs SET parsed_payload = "
        "json_set(parsed_payload, '$.amount', '55.55') WHERE id = ?",
        (pid,),
    )
    conn.commit()

    replayed = convert(conn, cmd)
    assert replayed.idempotent is True
    assert result_fields(replayed) == result_fields(first)
    assert replayed.proposal_content_hash == expected


def test_replay_with_different_reason_only_is_exact_replay(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "reason")
    first = convert(conn, command("reason", public_id, expected, reason="original"))

    replayed = convert(conn, command("reason", public_id, expected, reason="changed later"))
    assert replayed.idempotent is True
    assert result_fields(replayed) == result_fields(first)
    # The recorded reason is append-only audit context and never rewritten.
    stored = conn.execute(
        "SELECT reason FROM receipt_proposal_conversions WHERE command_public_id = 'rpfc_reason'"
    ).fetchone()
    assert stored["reason"] == "original"


def test_order_independent_replay_uses_canonical_sorting(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "order")
    first = convert(
        conn,
        command(
            "order",
            public_id,
            expected,
            participants=entries(("person_owner", 1), ("person_alice", 1), ("person_bob", 0)),
        ),
    )

    replayed = convert(
        conn,
        command(
            "order",
            public_id,
            expected,
            participants=entries(("person_bob", 0), ("person_alice", 1), ("person_owner", 1)),
        ),
    )
    assert replayed.idempotent is True
    assert replayed.command_material_hash == first.command_material_hash
    assert result_fields(replayed) == result_fields(first)


# ---------------------------------------------------------------------------
# 8.4 Idempotency: material conflicts
# ---------------------------------------------------------------------------


def test_changed_payer_material_conflicts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "conflict")
    first = convert(conn, command("conflict", public_id, expected))
    before = table_counts(conn)

    with pytest.raises(ConversionIdempotencyConflictError, match="different canonical material"):
        convert(
            conn,
            command(
                "conflict",
                public_id,
                expected,
                payer_participant_public_id="person_alice",
            ),
        )

    assert table_counts(conn) == before
    registry = conn.execute(
        "SELECT command_material_hash FROM receipt_proposal_conversions "
        "WHERE command_public_id = 'rpfc_conflict'"
    ).fetchone()
    assert registry["command_material_hash"] == first.command_material_hash


def test_flipped_inclusion_material_conflicts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "flip")
    convert(
        conn,
        command(
            "flip",
            public_id,
            expected,
            participants=entries(("person_owner", 1), ("person_alice", 1)),
        ),
    )
    before = table_counts(conn)

    with pytest.raises(ConversionIdempotencyConflictError):
        convert(
            conn,
            command(
                "flip",
                public_id,
                expected,
                participants=entries(("person_owner", 1), ("person_alice", 0)),
            ),
        )
    assert table_counts(conn) == before


# ---------------------------------------------------------------------------
# 8.4 Concurrency: separate connections
# ---------------------------------------------------------------------------


def test_separate_connection_identical_command_replays(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "twoconn")
    cmd = command("twoconn", public_id, expected)
    first = convert(conn, cmd)

    other = connect_temp_db(migrated_temp_db_path)
    try:
        replayed = convert(other, cmd)
        assert replayed.idempotent is True
        assert result_fields(replayed) == result_fields(first)
    finally:
        other.close()
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_separate_connection_competing_commands_single_winner(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "race")
    convert(conn, command("race_winner", public_id, expected))

    other = connect_temp_db(migrated_temp_db_path)
    try:
        with pytest.raises(ReceiptFactsAlreadyConvertedError):
            convert(other, command("race_loser", public_id, expected))
    finally:
        other.close()
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0] == 1


def test_busy_write_lock_maps_to_persistence_error(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "busy")
    conn.execute("PRAGMA busy_timeout = 100")

    holder = connect_temp_db(migrated_temp_db_path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(ConversionPersistenceError) as excinfo:
            convert(conn, command("busy", public_id, expected))
        assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    finally:
        holder.rollback()
        holder.close()
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 8.4 Schema backstop (constraint-level fixtures per the Section 8 preamble)
# ---------------------------------------------------------------------------


def test_receipt_public_id_collision_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "collide")
    insert_manual_receipt(conn, derive_receipt_public_id("rpfc_collide"))
    conn.commit()
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    with pytest.raises(ConversionPersistenceError, match="no silent re-derivation"):
        convert(conn, command("collide", public_id, expected))

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert evidence_rows(conn) == before_evidence
    assert conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM financial_audit_events WHERE causation_public_id = 'rpfc_collide'"
        ).fetchone()[0]
        == 0
    )


def test_schema_backstop_registry_unique_constraints(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)

    # Converted proposal A supplies the duplicated values.
    pid_a, public_a, expected_a = seed_confirmed_receipt_proposal(conn, tmp_path, "bka")
    converted = convert(conn, command("bka", public_a, expected_a))
    # Confirmed-but-unconverted proposal C supplies fresh FK-valid values.
    pid_c, _public_c, _expected_c = seed_confirmed_receipt_proposal(conn, tmp_path, "bkc")
    fresh_receipt = insert_manual_receipt(conn, "rcpt_manual_backstop")
    conn.commit()

    duplicates = {
        "parser_output_id": dict(
            parser_output_id=pid_a,
            root_parser_output_id=pid_c,
            receipt_id=fresh_receipt,
            confirmation_public_id="pca_bkc",
        ),
        "supersession_root": dict(
            parser_output_id=pid_c,
            root_parser_output_id=pid_a,
            receipt_id=fresh_receipt,
            confirmation_public_id="pca_bkc",
        ),
        "receipt_id": dict(
            parser_output_id=pid_c,
            root_parser_output_id=pid_c,
            receipt_id=converted.receipt_id,
            confirmation_public_id="pca_bkc",
        ),
        "confirmation_public_id": dict(
            parser_output_id=pid_c,
            root_parser_output_id=pid_c,
            receipt_id=fresh_receipt,
            confirmation_public_id="pca_bka",
        ),
    }
    for label, columns in duplicates.items():
        with pytest.raises(sqlite3.IntegrityError):
            forge_registry_row(conn, command_public_id=f"rpfc_forged_{label}", **columns)
        conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0] == 1


def test_schema_backstop_registry_append_only_triggers(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "trig")
    convert(conn, command("trig", public_id, expected))

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE receipt_proposal_conversions SET reason = 'tampered' "
            "WHERE command_public_id = 'rpfc_trig'"
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM receipt_proposal_conversions WHERE command_public_id = 'rpfc_trig'"
        )
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0] == 1


def test_receipts_parser_output_partial_unique_index_backstop(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "pidx")
    convert(conn, command("pidx", public_id, expected))

    with pytest.raises(sqlite3.IntegrityError):
        insert_manual_receipt(conn, "rcpt_manual_dup_pointer", parser_output_id=pid)
    conn.rollback()

    # NULL parser_output_id rows remain unconstrained (partial index).
    insert_manual_receipt(conn, "rcpt_manual_null_a")
    insert_manual_receipt(conn, "rcpt_manual_null_b")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 3


# ---------------------------------------------------------------------------
# 8.5 Failure injection and rollback
# ---------------------------------------------------------------------------

_INJECTION_STAGES = (
    "before_receipt_insert",
    "before_participants_insert",
    "before_conversion_registry_insert",
    "before_audit_append",
    "before_persisted_verification",
    "before_commit",
)


@pytest.mark.parametrize("stage", _INJECTION_STAGES)
def test_failure_injection_rolls_back_completely(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, stage: str
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "inject")
    cmd = command("inject", public_id, expected)
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    def hook(reached_stage: str) -> None:
        if reached_stage == stage:
            raise RuntimeError(f"injected failure at {stage}")

    conversion_module._failure_injection_hook = hook
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            convert(conn, cmd)
    finally:
        conversion_module._failure_injection_hook = None

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert evidence_rows(conn) == before_evidence

    # The proposal remains confirmed and convertible after the rollback.
    result = convert(conn, cmd)
    assert result.idempotent is False
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(2)


def test_audit_transaction_error_maps_to_persistence_error_with_exact_cause(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 fix F10: the AuditChainTransactionError branch is exercised
    directly.

    The audit append itself raises the transaction-context error after the
    conversion's receipt writes; the conversion must translate it into the
    Section 7 taxonomy with the injected error as the exact ``__cause__``,
    roll back every write, and stay retryable once the fault is removed.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "audtx")
    cmd = command("audtx", public_id, expected)
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    injected = AuditChainTransactionError("injected audit transaction-context failure")

    def broken_append(*args: object, **kwargs: object) -> object:
        raise injected

    monkeypatch.setattr(conversion_module, "append_financial_audit_event", broken_append)
    with pytest.raises(
        ConversionPersistenceError,
        match="audit event could not be appended atomically",
    ) as excinfo:
        convert(conn, cmd)

    # The precise audit-specific mapping, not just the generic outer wrapper.
    assert excinfo.value.__cause__ is injected
    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert evidence_rows(conn) == before_evidence

    # With the real audit append restored, the same command still converts.
    monkeypatch.undo()
    result = convert(conn, cmd)
    assert result.idempotent is False
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(2)


def test_caller_owned_transaction_rejected_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "caller")
    cmd = command("caller", public_id, expected)
    before = table_counts(conn)

    conn.execute("BEGIN")
    try:
        with pytest.raises(ConversionCallerOwnedTransactionError):
            convert(conn, cmd)
        # The caller's transaction is left untouched, not rolled back.
        assert conn.in_transaction
    finally:
        conn.rollback()

    assert table_counts(conn) == before
    result = convert(conn, cmd)
    assert result.idempotent is False


# ---------------------------------------------------------------------------
# 8.7 Database safety
# ---------------------------------------------------------------------------


def test_foreign_keys_off_rejected_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """R4-F1: a legitimate staging connection with FK enforcement disabled
    must be rejected fail-closed before the transaction begins, with zero
    receipt/participant/registry/evidence/audit writes."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "fkoff")
    cmd = command("fkoff", public_id, expected)
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    conn.execute("PRAGMA foreign_keys = OFF")
    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
    try:
        with pytest.raises(ConversionForeignKeysDisabledError) as excinfo:
            convert(conn, cmd)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    # The precise typed cause, not just the conversion-taxonomy wrapper.
    assert isinstance(excinfo.value.__cause__, ForeignKeysDisabledError)
    assert not conn.in_transaction
    # Zero writes anywhere: receipts, participants, registry, audit, evidence.
    assert table_counts(conn) == before
    assert evidence_rows(conn) == before_evidence

    # With enforcement restored, the identical command converts normally.
    result = convert(conn, cmd)
    assert result.idempotent is False
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(2)


def test_plain_database_rejected_without_writes(tmp_path: Path) -> None:
    plain_path = tmp_path / "plain_untrusted.sqlite"
    conn = sqlite3.connect(str(plain_path))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ConversionStagingDatabaseRejectedError):
            convert(conn, command("plainzz", "prop_none", "0" * 64))
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()


def test_copied_staging_database_rejected(migrated_temp_db_path: Path, tmp_path: Path) -> None:
    copied_path = tmp_path / "copied_staging.sqlite"
    shutil.copy2(migrated_temp_db_path, copied_path)
    conn = sqlite3.connect(str(copied_path))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ConversionStagingDatabaseRejectedError):
            convert(conn, command("copyzz", "prop_none", "0" * 64))
        assert not conn.in_transaction
    finally:
        conn.close()


@pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason="live database not present")
def test_live_database_rejected_via_readonly_connection() -> None:
    conn = sqlite3.connect(f"file:{LIVE_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ConversionStagingDatabaseRejectedError):
            convert(conn, command("livezz", "prop_none", "0" * 64))
        assert not conn.in_transaction
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Review fix B (B41-PRECOMMIT-REVALIDATION): in-transaction mutations at the
# pre-commit verification seam fail closed and roll back completely
# ---------------------------------------------------------------------------


def convert_with_mutation_at_verification(
    conn: sqlite3.Connection,
    cmd: ReceiptFactsConversionCommand,
    mutate: Callable[[], None],
    match: str,
) -> ConversionPersistenceError:
    """Run one conversion whose state is mutated at the pre-commit seam.

    The mutation runs on the same connection inside the conversion's own
    transaction, after all conversion writes but before the Section 5.5
    revalidation.  A failed revalidation must roll the mutation back too.
    Returns the raised error so callers can assert its precise cause.
    """

    def hook(stage: str) -> None:
        if stage == "before_persisted_verification":
            mutate()

    conversion_module._failure_injection_hook = hook
    try:
        with pytest.raises(ConversionPersistenceError, match=match) as excinfo:
            convert(conn, cmd)
    finally:
        conversion_module._failure_injection_hook = None
    return excinfo.value


def test_mutation_at_hook_authorization_revoked_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A revocation landing inside the transaction voids the conversion."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "mutauth")
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    def mutate() -> None:
        conn.execute(
            "UPDATE parser_proposal_authorizations SET confirmation_state = 'revoked', "
            "revoked_at = '2026-07-25T00:00:00+00:00' WHERE parser_output_id = ?",
            (pid,),
        )

    error = convert_with_mutation_at_verification(
        conn,
        command("mutauth", public_id, expected),
        mutate,
        "Pre-commit revalidation failed",
    )
    # The precise cause is the revalidated guard, not a generic failure.
    assert isinstance(error.__cause__, StaleConfirmationHashError)
    assert "revoked" in str(error.__cause__)

    assert not conn.in_transaction
    assert table_counts(conn) == before
    # The revocation itself rolled back with the conversion writes: the
    # durable authorization is byte-identical to its pre-conversion state.
    assert evidence_rows(conn) == before_evidence

    result = convert(conn, command("mutauth_retry", public_id, expected))
    assert result.idempotent is False
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(2)


def test_mutation_at_hook_payload_drift_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An effective-content change inside the transaction voids the conversion."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "mutdrift")
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    def mutate() -> None:
        conn.execute(
            "UPDATE parser_outputs SET parsed_payload = "
            "json_set(parsed_payload, '$.amount', '77.77') WHERE id = ?",
            (pid,),
        )

    error = convert_with_mutation_at_verification(
        conn,
        command("mutdrift", public_id, expected),
        mutate,
        "proposal row changed",
    )
    # The snapshot comparison itself is the precise failure: no wrapped cause.
    assert error.__cause__ is None

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert evidence_rows(conn) == before_evidence

    result = convert(conn, command("mutdrift_retry", public_id, expected))
    assert result.idempotent is False


def test_mutation_at_hook_chain_topology_change_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A parent-pointer rewrite inside the transaction voids the conversion."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, _public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "muttopo")
    supersession = supersede_receipt_total_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=expected,
        field_updates={"amount": "20.00"},
        correction_public_id="rcor_muttopo_1",
    )
    child_id = int(supersession["replacement_parser_output_id"])
    child_public_id = str(supersession["replacement_proposal_public_id"])
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_muttopo_child")
    # An unrelated proposal supplies the forged new chain root.
    other_pid, _other_public_id = seed_receipt_proposal(conn, tmp_path, "muttopo_other")
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    def mutate() -> None:
        # The converted leaf's own row stays untouched; the topology change
        # happens one hop up, so only the fresh chain walk can catch it.
        conn.execute(
            "UPDATE parser_outputs SET parent_parser_output_id = ? WHERE id = ?",
            (other_pid, pid),
        )

    error = convert_with_mutation_at_verification(
        conn,
        command("muttopo", child_public_id, hash_of(conn, child_id)),
        mutate,
        "supersession chain topology changed",
    )
    # The chain-walk comparison itself is the precise failure: no wrapped cause.
    assert error.__cause__ is None

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert evidence_rows(conn) == before_evidence

    result = convert(conn, command("muttopo_retry", child_public_id, hash_of(conn, child_id)))
    assert result.idempotent is False


def test_mutation_at_hook_second_raw_intake_pointer_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A second raw-intake pointer inside the transaction voids the conversion."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "mutptr")
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    def mutate() -> None:
        conn.execute(
            "INSERT INTO raw_intake_records "
            "(public_id, source_type, source_channel, raw_input, received_at, "
            "parser_output_id) "
            "VALUES ('raw_mutptr_dup', 'telegram_text', 'telegram', 'dup', "
            "'2026-07-19T15:00:00+00:00', ?)",
            (pid,),
        )

    error = convert_with_mutation_at_verification(
        conn,
        command("mutptr", public_id, expected),
        mutate,
        "Pre-commit revalidation failed",
    )
    # The precise cause is the ambiguous raw-intake binding guard.
    assert isinstance(error.__cause__, ConversionEvidenceLineageError)
    assert "raw intake" in str(error.__cause__)

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert evidence_rows(conn) == before_evidence

    result = convert(conn, command("mutptr_retry", public_id, expected))
    assert result.idempotent is False


def test_mutation_at_hook_second_source_binding_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A second telegram source binding inside the transaction voids the conversion."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "mutbind")
    intake = conn.execute(
        "SELECT id FROM raw_intake_records WHERE parser_output_id = ?", (pid,)
    ).fetchone()
    binding = conn.execute(
        "SELECT * FROM telegram_attachment_source WHERE raw_intake_record_id = ?",
        (int(intake["id"]),),
    ).fetchone()
    assert binding is not None
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    def mutate() -> None:
        cursor = conn.execute(
            "INSERT INTO raw_intake_records "
            "(public_id, source_type, source_channel, raw_input, received_at) "
            "VALUES ('raw_mutbind_dup', 'telegram_text', 'telegram', 'dup', "
            "'2026-07-19T15:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO telegram_attachment_source (public_id, attachment_id, "
            "raw_intake_record_id, original_attachment_path, observed_file_size, "
            "content_hash, source_evidence_payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "tas_mutbind_dup",
                binding["attachment_id"],
                cursor.lastrowid,
                binding["original_attachment_path"],
                binding["observed_file_size"],
                binding["content_hash"],
                binding["source_evidence_payload"],
            ),
        )

    error = convert_with_mutation_at_verification(
        conn,
        command("mutbind", public_id, expected),
        mutate,
        "Pre-commit revalidation failed",
    )
    # The precise cause is the source-binding cardinality guard.
    assert isinstance(error.__cause__, ConversionEvidenceLineageError)
    assert "exactly one telegram attachment source binding" in str(error.__cause__)

    assert not conn.in_transaction
    assert table_counts(conn) == before
    assert evidence_rows(conn) == before_evidence

    result = convert(conn, command("mutbind_retry", public_id, expected))
    assert result.idempotent is False


# ---------------------------------------------------------------------------
# Review fix E (B41-TRUE-CONCURRENCY): two live writer threads on separate
# connections
# ---------------------------------------------------------------------------


def run_conversion_worker(
    db_path: Path,
    cmd: ReceiptFactsConversionCommand,
    results: dict[str, object],
    key: str,
    write_lock_queued: threading.Event | None = None,
) -> None:
    """Worker body: own connection, bounded busy wait, exceptions captured."""
    conn = connect_temp_db(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        if write_lock_queued is not None:
            # Round 3 fix F9: the deterministic overlap signal fires from
            # SQLite's own trace hook the moment this writer actually issues
            # BEGIN IMMEDIATE - i.e. while it is queueing on the held write
            # lock - not merely when the thread reaches convert().

            def trace(statement: str) -> None:
                if "BEGIN IMMEDIATE" in statement:
                    write_lock_queued.set()

            conn.set_trace_callback(trace)
        results[key] = convert(conn, cmd)
    except BaseException as exc:  # noqa: BLE001 - surfaced by the main thread
        results[key] = exc
    finally:
        conn.close()


def test_true_concurrency_identical_command_exactly_once(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """A live second writer against a paused first writer stays exactly-once.

    The first writer pauses at ``before_commit`` while holding the write
    transaction; the second writer queues on the SQLite write lock.  After
    the release, the second writer must either replay idempotently or fail
    closed as a typed busy persistence error - never a second receipt.
    """
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "thrsame")
    cmd = command("thrsame", public_id, expected)

    first_at_commit = threading.Event()
    release_first = threading.Event()
    second_queued_on_lock = threading.Event()

    def hook(stage: str) -> None:
        if stage != "before_commit":
            return
        if threading.current_thread().name != "b41-first-writer":
            return
        first_at_commit.set()
        if not release_first.wait(timeout=10.0):
            raise RuntimeError("first writer was never released")

    results: dict[str, object] = {}
    first = threading.Thread(
        target=run_conversion_worker,
        args=(migrated_temp_db_path, cmd, results, "first"),
        name="b41-first-writer",
    )
    second = threading.Thread(
        target=run_conversion_worker,
        args=(migrated_temp_db_path, cmd, results, "second", second_queued_on_lock),
        name="b41-second-writer",
    )
    conversion_module._failure_injection_hook = hook
    try:
        first.start()
        assert first_at_commit.wait(timeout=10.0), "first writer never reached before_commit"
        second.start()
        # Round 3 fix F9: deterministic overlap proof.  The event fires from
        # the second connection's SQLite trace hook when its BEGIN IMMEDIATE
        # statement executes, while the paused first writer still holds the
        # write transaction - the overlap is observed, not assumed, and the
        # first writer is only released after that observation.  No
        # wall-clock sleep is involved anywhere.
        assert second_queued_on_lock.wait(timeout=10.0), (
            "second writer never issued BEGIN IMMEDIATE"
        )
        assert first_at_commit.is_set() and not release_first.is_set()
        release_first.set()
        first.join(timeout=15.0)
        second.join(timeout=15.0)
    finally:
        conversion_module._failure_injection_hook = None
        release_first.set()
    assert not first.is_alive() and not second.is_alive()

    first_result = results["first"]
    if isinstance(first_result, BaseException):
        raise AssertionError(f"first writer failed: {first_result!r}") from first_result
    assert first_result.idempotent is False  # type: ignore[attr-defined]

    second_result = results["second"]
    if isinstance(second_result, BaseException):
        # Documented acceptable outcome: the write-lock wait was exhausted
        # and the second writer failed closed without any partial writes.
        assert isinstance(second_result, ConversionPersistenceError)
        assert isinstance(second_result.__cause__, sqlite3.OperationalError)
    else:
        assert second_result.idempotent is True  # type: ignore[attr-defined]
        assert result_fields(second_result) == result_fields(first_result)

    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0] == 1
    audit_events = conn.execute(
        "SELECT COUNT(*) FROM financial_audit_events WHERE causation_public_id = 'rpfc_thrsame'"
    ).fetchone()[0]
    assert audit_events == 1


def test_true_concurrency_competing_commands_single_winner(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """Two distinct commands racing on one proposal produce exactly one winner."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "thrrace")
    barrier = threading.Barrier(2, timeout=10.0)
    results: dict[str, object] = {}

    def racer(key: str, cmd: ReceiptFactsConversionCommand) -> None:
        conn_local = connect_temp_db(migrated_temp_db_path)
        try:
            conn_local.execute("PRAGMA busy_timeout = 10000")
            barrier.wait()
            results[key] = convert(conn_local, cmd)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the main thread
            results[key] = exc
        finally:
            conn_local.close()

    threads = [
        threading.Thread(
            target=racer,
            args=("a", command("thrrace_a", public_id, expected)),
            name="b41-racer-a",
        ),
        threading.Thread(
            target=racer,
            args=("b", command("thrrace_b", public_id, expected)),
            name="b41-racer-b",
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)
    assert all(not thread.is_alive() for thread in threads)

    winners = [value for value in results.values() if not isinstance(value, BaseException)]
    losers = [value for value in results.values() if isinstance(value, BaseException)]
    assert len(winners) == 1 and len(losers) == 1
    assert winners[0].idempotent is False  # type: ignore[attr-defined]
    loser = losers[0]
    if isinstance(loser, ConversionPersistenceError):
        # Documented acceptable outcome: lock wait exhausted, no partial writes.
        assert isinstance(loser.__cause__, sqlite3.OperationalError)
    else:
        assert isinstance(loser, ReceiptFactsAlreadyConvertedError)

    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM receipt_proposal_conversions").fetchone()[0] == 1
    audit_events = conn.execute(
        "SELECT COUNT(*) FROM financial_audit_events "
        "WHERE causation_public_id IN ('rpfc_thrrace_a', 'rpfc_thrrace_b')"
    ).fetchone()[0]
    assert audit_events == 1
