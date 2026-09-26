"""B4.0/B4.1 receipt conversion mutual-exclusion guard tests.

These tests prove that receipt proposals with persisted OCR link evidence
never enter the legacy simple-expense converter, and that the
``receipt_proposal_conversions`` registry (real migration 035 as of B4.1)
is recognised read-only by the legacy converter, completion, and
supersession boundaries.  Every rejection is asserted to leave zero
financial changes.

Forward-compatibility coverage from B4.0 is preserved: the absent-registry
and malformed-registry scenarios run against a partially migrated staging
database (migrations 001-034 only), because a fully migrated database now
always contains the real registry table.  Registry rows inserted here are
schema-legal fixture SQL against the real migration 035 table; no test-only
production schema is introduced.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from finance_core.intake.raw_text_service import process_raw_text_input
from finance_core.parser_proposals import (
    complete_proposal,
    confirm_proposal,
    reject_proposal,
    supersede_receipt_total_proposal,
)
from finance_core.parser_proposals.completion import InvalidCompletionStatusError
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.human_drafts import (
    HumanDraftError,
    require_current_human_draft_publication_in_transaction,
)
from finance_core.parser_proposals.human_revision import (
    HumanRevisionLineageError,
    verify_human_revision_descendant,
)
from finance_core.parser_proposals.receipt_supersession import (
    InvalidSupersessionStatusError,
    SupersessionPersistenceError,
)
from finance_core.parser_proposals.service import (
    AlreadyConvertedProposalError,
    InvalidProposalStatusError,
    UnsupportedProposalTypeError,
    convert_confirmed_parser_proposal,
)
from tests.conftest import MIGRATION_PATHS, apply_migrations, connect_temp_db
from tests.test_parser_proposal_conversion import (
    count_rows,
    create_confirmed_simple_proposal,
    update_payload,
)
from tests.test_receipt_ocr_proposal_ingestion_v1 import (
    JPEG,
    _ingest,
    _prepare_extraction,
    _sgd_blocks,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_FINANCIAL_TABLES = (
    "transactions",
    "parser_proposal_conversion_audit",
    "financial_audit_events",
    "receipt_proposal_revisions",
    "parser_proposal_completions",
    "parser_outputs",
    "parser_proposal_events",
    "parser_proposal_field_evidence",
    "parser_proposal_confirmations",
    "parser_proposal_authorizations",
    "receipt_ocr_proposal_links",
    "raw_intake_records",
)

_REGISTRY_MIGRATION_NAME = "035_receipt_proposal_conversions.sql"

# TEST-ONLY DDL: a present-but-malformed registry without parser_output_id,
# created inside a partially migrated (pre-035) temporary database to prove
# the read-only guards fail closed instead of silently treating an invalid
# registry as "no registry state".
_TEST_ONLY_MALFORMED_REGISTRY_DDL = """
CREATE TABLE receipt_proposal_conversions (
    wrong_column TEXT
)
"""


@pytest.fixture()
def pre035_db_connection(temp_db_path: Path) -> Iterator[sqlite3.Connection]:
    """A staging database migrated through 034 only (no conversion registry)."""
    assert MIGRATION_PATHS[34].name == _REGISTRY_MIGRATION_NAME
    conn = connect_temp_db(temp_db_path)
    try:
        apply_migrations(conn, MIGRATION_PATHS[:34])
        conn.commit()
        yield conn
    finally:
        conn.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _create_pending_simple_proposal(conn: sqlite3.Connection) -> int:
    result = process_raw_text_input(conn, "Coffee SGD 6.40 at Starbucks")
    parser_output_id = int(result["parser_output"]["id"])
    update_payload(conn, parser_output_id, {"transaction_date": "2026-06-01"})
    return parser_output_id


def _verify_human_lineage(conn: sqlite3.Connection, parser_output_id: int) -> object:
    proposal = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (parser_output_id,)
    ).fetchone()
    assert proposal is not None
    projection = dict(proposal)
    content_hash = compute_effective_proposal_content_hash(conn, projection)
    _payload, _completion_id, proposal_version = resolve_effective_payload(conn, projection)
    return verify_human_revision_descendant(
        conn,
        projection,
        content_hash=content_hash,
        proposal_version=proposal_version,
    )


def _seed_receipt_proposal(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str
) -> tuple[int, str]:
    """Create one B1 receipt total proposal; return (parser_output_id, hash)."""
    extraction_public_id = _prepare_extraction(
        conn,
        tmp_path,
        suffix=suffix,
        blocks=_sgd_blocks(),
        # Suffix-unique attachment bytes keep extraction fingerprints distinct
        # when one staging database seeds multiple proposals.
        content=JPEG + f"-{suffix}".encode("utf-8"),
    )
    result = _ingest(
        conn,
        extraction_public_id,
        proposal=f"prop_{suffix}",
        link=f"ropl_{suffix}",
    )
    return result.parser_output_id, _hash_of(conn, result.parser_output_id)


def _hash_of(conn: sqlite3.Connection, parser_output_id: int) -> str:
    return compute_effective_proposal_content_hash(conn, {"id": parser_output_id})


def _create_malformed_registry(conn: sqlite3.Connection) -> None:
    conn.execute(_TEST_ONLY_MALFORMED_REGISTRY_DDL)
    conn.commit()


def _ensure_fixture_participant(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM participants WHERE public_id = 'person_registry_fixture'"
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO participants (public_id, display_name, is_self) "
        "VALUES ('person_registry_fixture', 'Registry Fixture', 0)"
    )
    lastrowid = cursor.lastrowid
    assert lastrowid is not None
    return int(lastrowid)


def _insert_registry_row(
    conn: sqlite3.Connection,
    parser_output_id: int,
    suffix: str,
    *,
    confirmation_public_id: str | None = None,
) -> tuple[str, int]:
    """Insert a schema-legal migration 035 registry row via fixture SQL.

    Returns (command_public_id, receipt_id).  When no confirmation is
    supplied, the proposal's own authorization row is used; unconfirmed
    proposals must pass a donor ``confirmation_public_id`` to satisfy the
    registry's foreign key.
    """
    if confirmation_public_id is None:
        row = conn.execute(
            "SELECT confirmation_public_id FROM parser_proposal_authorizations "
            "WHERE parser_output_id = ?",
            (parser_output_id,),
        ).fetchone()
        assert row is not None
        confirmation_public_id = str(row["confirmation_public_id"])
    payer_id = _ensure_fixture_participant(conn)
    receipt_cursor = conn.execute(
        "INSERT INTO receipts ("
        "  public_id, merchant, net_paid_amount, net_paid_amount_canonical_text,"
        "  currency, payer_participant_id"
        ") VALUES (?, 'REGISTRY FIXTURE', '9.99', '9.99', 'SGD', ?)",
        (f"rcpt_fixture_{suffix}", payer_id),
    )
    receipt_lastrowid = receipt_cursor.lastrowid
    assert receipt_lastrowid is not None
    receipt_id = int(receipt_lastrowid)
    command_public_id = f"rpfc_{suffix}"
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
            parser_output_id,
            receipt_id,
            confirmation_public_id,
            _sha(f"fixture-proposal:{suffix}"),
            _sha(f"fixture-material:{suffix}"),
            _sha(f"fixture-result:{suffix}"),
        ),
    )
    conn.commit()
    return command_public_id, receipt_id


def _financial_state(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    tables = list(_FINANCIAL_TABLES)
    # The registry participates in the snapshot when present so rejected
    # commands provably never mutate or delete its rows.
    registry_present = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'receipt_proposal_conversions'"
        ).fetchone()
        is not None
    )
    if registry_present:
        tables.append("receipt_proposal_conversions")
    return {
        table: [
            dict(row)
            for row in conn.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
        ]
        for table in tables
    }


def _registry_rows(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
    return [
        (row["command_public_id"], row["parser_output_id"], row["receipt_id"])
        for row in conn.execute(
            "SELECT command_public_id, parser_output_id, receipt_id "
            "FROM receipt_proposal_conversions ORDER BY rowid"
        ).fetchall()
    ]


def _status_of(conn: sqlite3.Connection, parser_output_id: int) -> str:
    row = conn.execute(
        "SELECT parse_status FROM parser_outputs WHERE id = ?", (parser_output_id,)
    ).fetchone()
    assert row is not None
    return str(row["parse_status"])


def _supersede(
    conn: sqlite3.Connection,
    parser_output_id: int,
    expected_hash: str,
    field_updates: dict[str, Any],
    *,
    cid: str,
) -> dict[str, Any]:
    return supersede_receipt_total_proposal(
        conn,
        parser_output_id,
        actor="owner",
        expected_content_hash=expected_hash,
        field_updates=field_updates,
        correction_public_id=cid,
    )


# ---------------------------------------------------------------------------
# A. Legacy converter rejects OCR-linked receipt proposals
# ---------------------------------------------------------------------------


def test_legacy_converter_rejects_initial_link_receipt_proposal(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, _expected = _seed_receipt_proposal(conn, tmp_path, "excl_init")
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_excl_init_1")
    before = _financial_state(conn)

    with pytest.raises(UnsupportedProposalTypeError, match="OCR link evidence"):
        convert_confirmed_parser_proposal(conn, pid)

    assert _financial_state(conn) == before
    assert count_rows(conn, "transactions") == 0
    assert count_rows(conn, "parser_proposal_conversion_audit") == 0


def test_legacy_converter_rejects_superseding_correction_replacement(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "excl_child")
    result = _supersede(conn, pid, expected, {"amount": "20.00"}, cid="rcor_excl_child_1")
    child_id = int(result["replacement_parser_output_id"])
    confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_excl_child_1")
    before = _financial_state(conn)

    # The replacement carries a superseding_correction OCR link and must be
    # rejected by the same guard as an initial-link proposal.
    with pytest.raises(UnsupportedProposalTypeError, match="OCR link evidence"):
        convert_confirmed_parser_proposal(conn, child_id)

    assert _financial_state(conn) == before
    assert count_rows(conn, "transactions") == 0


def test_status_guard_precedes_ocr_link_guard(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, _expected = _seed_receipt_proposal(conn, tmp_path, "excl_status")

    # Deterministic guard order: the confirmed-status check still fires
    # before the OCR-link guard for unconfirmed receipt proposals.
    with pytest.raises(InvalidProposalStatusError):
        convert_confirmed_parser_proposal(conn, pid)
    assert count_rows(conn, "transactions") == 0


# ---------------------------------------------------------------------------
# B. Registry recognition in the legacy converter
# ---------------------------------------------------------------------------


def test_absent_registry_keeps_legitimate_conversion_working(
    pre035_db_connection: sqlite3.Connection,
) -> None:
    # Forward-compatibility: on a pre-035 database without the registry the
    # legacy converter still treats absence as "no registry state".
    conn = pre035_db_connection
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_proposal_conversions'"
        ).fetchone()
        is None
    )
    pid = create_confirmed_simple_proposal(conn)

    result = convert_confirmed_parser_proposal(conn, pid)

    assert result["final_transaction_created"] is True
    assert result["idempotent"] is False
    assert count_rows(conn, "transactions") == 1


@pytest.mark.parametrize(
    "remaining_d1_table",
    [
        "parser_human_drafts",
        "parser_human_draft_reply_evidence",
        "parser_human_draft_operations",
        "parser_human_draft_cards",
        "parser_human_draft_card_delivery_attempts",
        "parser_human_draft_card_delivery_outcomes",
        "parser_human_draft_publications",
        "parser_human_draft_action_bindings",
    ],
)
def test_partial_d1_schema_cannot_masquerade_as_legacy(
    pre035_db_connection: sqlite3.Connection,
    remaining_d1_table: str,
) -> None:
    conn = pre035_db_connection
    pid = create_confirmed_simple_proposal(conn)
    conn.execute(f"CREATE TABLE {remaining_d1_table} (id INTEGER PRIMARY KEY)")

    with pytest.raises(HumanRevisionLineageError, match="publication schema is missing"):
        _verify_human_lineage(conn, pid)


def test_post_047_ledger_state_cannot_masquerade_as_legacy(
    pre035_db_connection: sqlite3.Connection,
) -> None:
    conn = pre035_db_connection
    pid = create_confirmed_simple_proposal(conn)
    conn.execute(
        """
        INSERT INTO schema_migrations (
            migration_id, migration_filename, migration_sequence,
            checksum_sha256, schema_fingerprint, applied_at,
            runner_version, application_version, adoption_mode
        ) VALUES ('048', '048_d1_human_ai_lineage_transition.sql', 48,
                  ?, ?, '2026-09-16T00:00:00+00:00',
                  'test-runner', NULL, 'applied')
        """,
        ("0" * 64, "1" * 64),
    )

    with pytest.raises(HumanRevisionLineageError, match="publication schema is missing"):
        _verify_human_lineage(conn, pid)


def test_damaged_migration_ledger_cannot_masquerade_as_legacy(
    pre035_db_connection: sqlite3.Connection,
) -> None:
    conn = pre035_db_connection
    pid = create_confirmed_simple_proposal(conn)
    conn.execute("ALTER TABLE schema_migrations RENAME TO damaged_schema_migrations")
    conn.execute("CREATE TABLE schema_migrations (wrong_column TEXT)")

    with pytest.raises(HumanRevisionLineageError, match="migration ledger is invalid"):
        _verify_human_lineage(conn, pid)


def test_post_047_trigger_cannot_masquerade_as_legacy(
    pre035_db_connection: sqlite3.Connection,
) -> None:
    conn = pre035_db_connection
    pid = create_confirmed_simple_proposal(conn)
    conn.execute(
        """
        CREATE TRIGGER test_d1_publication_schema_marker
        AFTER UPDATE ON parser_outputs
        BEGIN
            SELECT 1 FROM parser_human_draft_publications;
        END
        """
    )

    with pytest.raises(HumanRevisionLineageError, match="publication schema is missing"):
        _verify_human_lineage(conn, pid)


@pytest.mark.parametrize("marker", ["proposal", "event"])
def test_d1_marker_cannot_masquerade_as_legacy(
    pre035_db_connection: sqlite3.Connection,
    marker: str,
) -> None:
    conn = pre035_db_connection
    pid = create_confirmed_simple_proposal(conn)
    if marker == "proposal":
        conn.execute(
            "UPDATE parser_outputs SET parser_name = 'human_revision' WHERE id = ?",
            (pid,),
        )
    else:
        conn.execute(
            "UPDATE parser_proposal_events SET event_reason = 'D1 human revision forged' "
            "WHERE parser_output_id = ?",
            (pid,),
        )

    with pytest.raises(HumanRevisionLineageError, match="publication schema is missing"):
        _verify_human_lineage(conn, pid)


def test_absent_d1_schema_keeps_legacy_reject_and_exact_replay_working(
    pre035_db_connection: sqlite3.Connection,
) -> None:
    conn = pre035_db_connection
    pid = _create_pending_simple_proposal(conn)

    initial = reject_proposal(
        conn,
        pid,
        actor="owner",
        reason="not mine",
        confirmation_public_id="pca_pre_d1_reject",
    )
    replay = reject_proposal(
        conn,
        pid,
        actor="owner",
        reason="not mine",
        confirmation_public_id="pca_pre_d1_reject",
    )

    assert initial["idempotent"] is False
    assert replay["idempotent"] is True
    assert (
        conn.execute("SELECT parse_status FROM parser_outputs WHERE id = ?", (pid,)).fetchone()[0]
        == "rejected"
    )
    authorization = conn.execute(
        "SELECT * FROM parser_proposal_authorizations WHERE parser_output_id = ?", (pid,)
    ).fetchone()
    assert authorization is not None
    assert authorization["confirmation_state"] == "rejected"
    assert count_rows(conn, "parser_proposal_authorizations") == 1
    assert count_rows(conn, "transactions") == 0


@pytest.mark.parametrize(
    "damage",
    [
        "post_047_ledger",
        "damaged_ledger",
        "post_047_trigger",
        "proposal_marker",
        "event_marker",
        "noncore_d1_table",
        "core_table_pair_only",
    ],
)
def test_confirm_guard_rejects_nonlegacy_d1_damage_without_financial_writes(
    pre035_db_connection: sqlite3.Connection,
    damage: str,
) -> None:
    conn = pre035_db_connection
    pid = create_confirmed_simple_proposal(conn)
    if damage == "post_047_ledger":
        conn.execute(
            """
            INSERT INTO schema_migrations (
                migration_id, migration_filename, migration_sequence,
                checksum_sha256, schema_fingerprint, applied_at,
                runner_version, application_version, adoption_mode
            ) VALUES ('048', '048_d1_human_ai_lineage_transition.sql', 48,
                      ?, ?, '2026-09-16T00:00:00+00:00',
                      'test-runner', NULL, 'applied')
            """,
            ("0" * 64, "1" * 64),
        )
    elif damage == "damaged_ledger":
        conn.execute("ALTER TABLE schema_migrations RENAME TO damaged_schema_migrations")
        conn.execute("CREATE TABLE schema_migrations (wrong_column TEXT)")
    elif damage == "post_047_trigger":
        conn.execute(
            """
            CREATE TRIGGER test_direct_guard_d1_publication_marker
            AFTER UPDATE ON parser_outputs
            BEGIN
                SELECT 1 FROM parser_human_draft_publications;
            END
            """
        )
    elif damage == "proposal_marker":
        conn.execute(
            "UPDATE parser_outputs SET parser_name = 'human_revision' WHERE id = ?", (pid,)
        )
    elif damage == "event_marker":
        conn.execute(
            "UPDATE parser_proposal_events SET event_reason = 'D1 human revision forged' "
            "WHERE parser_output_id = ?",
            (pid,),
        )
    elif damage == "noncore_d1_table":
        conn.execute("CREATE TABLE parser_human_draft_action_bindings (id INTEGER PRIMARY KEY)")
    else:
        conn.execute("CREATE TABLE parser_human_drafts (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE parser_human_draft_publications (id INTEGER PRIMARY KEY)")
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    before = _financial_state(conn)

    with pytest.raises(HumanDraftError, match="decision_lineage_schema_invalid"):
        require_current_human_draft_publication_in_transaction(
            conn,
            parser_output_id=pid,
            authenticated_actor_id="owner",
            decision_binding=None,
            now_epoch=1000,
        )

    assert _financial_state(conn) == before


def test_present_empty_registry_keeps_conversion_and_replay_working(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    # The fully migrated database now always contains the real (empty)
    # migration 035 registry; eligible simple proposals convert normally.
    conn = migrated_temp_db_connection
    assert _registry_rows(conn) == []
    pid = create_confirmed_simple_proposal(conn, source_type="manual_entry")

    first = convert_confirmed_parser_proposal(conn, pid)
    replay = convert_confirmed_parser_proposal(conn, pid)

    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert replay["transaction_public_id"] == first["transaction_public_id"]
    assert count_rows(conn, "transactions") == 1


def test_registry_row_blocks_legacy_conversion_with_zero_writes(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    pid = create_confirmed_simple_proposal(conn, source_type="manual_entry")
    cmd_id, receipt_id = _insert_registry_row(conn, pid, "excl_blocked")
    before = _financial_state(conn)

    with pytest.raises(AlreadyConvertedProposalError, match="receipt conversion registry"):
        convert_confirmed_parser_proposal(conn, pid)

    assert _financial_state(conn) == before
    assert count_rows(conn, "transactions") == 0
    assert count_rows(conn, "parser_proposal_conversion_audit") == 0
    assert _registry_rows(conn) == [(cmd_id, pid, receipt_id)]


def test_malformed_registry_fails_closed_in_legacy_converter(
    pre035_db_connection: sqlite3.Connection,
) -> None:
    conn = pre035_db_connection
    pid = create_confirmed_simple_proposal(conn)
    _create_malformed_registry(conn)
    before = _financial_state(conn)

    # A present-but-invalid registry must fail closed instead of being
    # silently treated as "no registry state".
    with pytest.raises(sqlite3.OperationalError):
        convert_confirmed_parser_proposal(conn, pid)

    assert not conn.in_transaction
    assert _financial_state(conn) == before
    assert count_rows(conn, "transactions") == 0


def test_registry_guard_precedes_legacy_replay(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    pid = create_confirmed_simple_proposal(conn, source_type="manual_entry")
    first = convert_confirmed_parser_proposal(conn, pid)
    assert first["idempotent"] is False
    cmd_id, receipt_id = _insert_registry_row(conn, pid, "excl_replay")
    before = _financial_state(conn)

    # A registry row is never a legacy-conversion replay: the registry guard
    # fires before the legacy conversion-audit replay lookup.
    with pytest.raises(AlreadyConvertedProposalError, match="receipt conversion registry"):
        convert_confirmed_parser_proposal(conn, pid)

    assert _financial_state(conn) == before
    assert count_rows(conn, "transactions") == 1
    assert _registry_rows(conn) == [(cmd_id, pid, receipt_id)]


def test_ocr_link_guard_precedes_registry_guard(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, _expected = _seed_receipt_proposal(conn, tmp_path, "excl_order")
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_excl_order_1")
    _insert_registry_row(conn, pid, "excl_order")

    with pytest.raises(UnsupportedProposalTypeError, match="OCR link evidence"):
        convert_confirmed_parser_proposal(conn, pid)
    assert count_rows(conn, "transactions") == 0


# ---------------------------------------------------------------------------
# C. Completion rejects registry-recorded proposals
# ---------------------------------------------------------------------------


def test_completion_rejects_registry_recorded_proposal(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "excl_comp")
    # The target proposal is unconfirmed, so a donor confirmation satisfies
    # the registry's confirmation foreign key for this forced fixture.
    donor_pid, _donor_hash = _seed_receipt_proposal(conn, tmp_path, "excl_comp_donor")
    confirm_proposal(conn, donor_pid, actor="owner", confirmation_public_id="pca_excl_comp_donor")
    cmd_id, receipt_id = _insert_registry_row(
        conn, pid, "excl_comp", confirmation_public_id="pca_excl_comp_donor"
    )
    before = _financial_state(conn)
    events_before = count_rows(conn, "parser_proposal_events")

    with pytest.raises(InvalidCompletionStatusError, match="receipt conversion registry"):
        complete_proposal(
            conn,
            pid,
            actor="owner",
            expected_content_hash=expected,
            field_updates={"merchant": "Sheng Siong"},
            completion_public_id="pco_excl_comp_1",
        )

    assert _financial_state(conn) == before
    assert count_rows(conn, "parser_proposal_completions") == 0
    # The rejected command creates no lifecycle events and leaves the
    # registry row untouched.
    assert count_rows(conn, "parser_proposal_events") == events_before
    assert _registry_rows(conn) == [(cmd_id, pid, receipt_id)]
    assert _status_of(conn, pid) == "parsed_pending_confirmation"


def test_completion_succeeds_with_present_empty_registry(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "excl_comp_ok")
    assert _registry_rows(conn) == []

    # The real present-but-empty registry must not change behaviour for
    # eligible proposals: completion still succeeds normally.
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=expected,
        field_updates={"merchant": "Sheng Siong"},
        completion_public_id="pco_excl_comp_ok_1",
    )

    assert count_rows(conn, "parser_proposal_completions") == 1
    assert _status_of(conn, pid) == "edited_pending_confirmation"
    assert _registry_rows(conn) == []


def test_completion_malformed_registry_fails_closed(
    pre035_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = pre035_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "excl_compbad")
    _create_malformed_registry(conn)
    before = _financial_state(conn)

    with pytest.raises(sqlite3.OperationalError):
        complete_proposal(
            conn,
            pid,
            actor="owner",
            expected_content_hash=expected,
            field_updates={"merchant": "Sheng Siong"},
            completion_public_id="pco_excl_compbad_1",
        )

    assert not conn.in_transaction
    assert _financial_state(conn) == before
    assert count_rows(conn, "parser_proposal_completions") == 0


# ---------------------------------------------------------------------------
# D. Supersession rejects registry-recorded proposals
# ---------------------------------------------------------------------------


def test_supersession_rejects_registry_recorded_proposal(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "excl_sup")
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_excl_sup_1")
    cmd_id, receipt_id = _insert_registry_row(conn, pid, "excl_sup")
    before = _financial_state(conn)
    events_before = count_rows(conn, "parser_proposal_events")

    # A confirmed-but-unconverted receipt proposal is normally supersedable;
    # a registry row proves canonical receipt facts exist and blocks it.
    with pytest.raises(InvalidSupersessionStatusError, match="receipt conversion registry"):
        _supersede(conn, pid, expected, {"amount": "83.00"}, cid="rcor_excl_sup_1")

    assert _financial_state(conn) == before
    assert count_rows(conn, "receipt_proposal_revisions") == 0
    # The rejected command creates no lifecycle events and leaves the
    # registry row untouched.
    assert count_rows(conn, "parser_proposal_events") == events_before
    assert _registry_rows(conn) == [(cmd_id, pid, receipt_id)]
    assert _status_of(conn, pid) == "confirmed"


def test_supersession_succeeds_with_present_empty_registry(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "excl_sup_ok")
    assert _registry_rows(conn) == []

    # The real present-but-empty registry must not change behaviour for
    # eligible proposals: supersession still succeeds normally.
    result = _supersede(conn, pid, expected, {"amount": "83.00"}, cid="rcor_excl_sup_ok_1")

    child_id = int(result["replacement_parser_output_id"])
    assert count_rows(conn, "receipt_proposal_revisions") == 1
    assert _status_of(conn, pid) == "superseded"
    assert _status_of(conn, child_id) == "parsed_pending_confirmation"
    assert _registry_rows(conn) == []


def test_supersession_malformed_registry_fails_closed(
    pre035_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = pre035_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "excl_supbad")
    _create_malformed_registry(conn)
    before = _financial_state(conn)

    with pytest.raises(SupersessionPersistenceError) as excinfo:
        _supersede(conn, pid, expected, {"amount": "84.00"}, cid="rcor_excl_supbad_1")

    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    assert not conn.in_transaction
    assert _financial_state(conn) == before
    assert count_rows(conn, "receipt_proposal_revisions") == 0


# ---------------------------------------------------------------------------
# E. Temporary database safety
# ---------------------------------------------------------------------------


def test_exclusion_tests_use_temporary_database_only(
    migrated_temp_db_connection: sqlite3.Connection, temp_db_path: Path
) -> None:
    database_path = Path(
        migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    )
    assert database_path == temp_db_path
