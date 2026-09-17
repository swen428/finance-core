"""PR #249 acceptance-gap regressions: receipt-scoped membership (R3-06/R3-07/R3-07-1).

PR #249 (merge commit ``936eb1b``) replaced the FIX-06B unanimous-membership
rule with a group-wide "most included wins" collapse keyed by participant
public ID alone, and deferred the receipt-scoped membership contract
(``R3-07-1``).  These regressions freeze the closed contract:

- membership is resolved and verified against the exact authoritative receipt
  bound to the snapshot/finalization (IAF path);
- legacy group-scoped finalizations without an IAF binding accept
  uncontradicted membership only: contradictory ``is_included`` or ``role``
  across the receipts carrying a participant fails closed instead of being
  collapsed;
- membership from receipt A never authorizes behavior for receipt B;
- every finalization records durable receipt-scoped membership evidence
  (migration 039), and replay verifies it fail-closed with zero writes.

Direct SQL appears only in explicitly marked forged-corruption fixtures and
SELECT assertions.  Only disposable staging databases are used;
``database/finance.db`` and seed data are untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from finance_core.receipt_finalization import (
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    finalize_receipt_split,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.models import (
    FinalizationBlockReason,
    FinalizationIdempotencyError,
    IneligibleForFinalizationError,
)
from tests.test_iaf_finalization_invariants_v1 import (
    FAIL_CLOSED_ERRORS,
    _counts,
    _setup_active_fact_set,
)
from tests.test_receipt_finalization_settlement_runtime import (
    _build_finalization_input,
    _seed_authorization_for_input,
)

MEMBERSHIP_EVIDENCE_TABLE = "receipt_finalization_membership_evidence"

REPLAY_WRITE_TABLES = (
    "transactions",
    "calculation_runs",
    "calculation_participant_shares",
    "settlement_obligations",
    "receipt_groups",
    "receipt_group_receipts",
    "receipt_finalization_audit",
    "receipt_finalization_idempotency",
    "receipt_finalization_membership_evidence",
    "receipt_fact_set_binding_evidence",
    "financial_audit_events",
)

# Row counts cannot detect an in-place lifecycle UPDATE, so replay refusals also
# freeze the mutable lifecycle columns the finalizer owns.
LIFECYCLE_STATE_QUERIES = (
    "SELECT public_id, status FROM receipt_groups ORDER BY public_id",
    "SELECT authorization_id, authorization_state FROM receipt_finalization_authorizations"
    " ORDER BY authorization_id",
    "SELECT idempotency_key, status, finalization_audit_id"
    " FROM receipt_finalization_idempotency ORDER BY idempotency_key",
    "SELECT public_id, status FROM receipts ORDER BY public_id",
)


def _lifecycle_state(conn: sqlite3.Connection) -> list[list[tuple[Any, ...]]]:
    return [[tuple(row) for row in conn.execute(sql).fetchall()] for sql in LIFECYCLE_STATE_QUERIES]


PEOPLE = ("person_owner", "person_alice", "person_bob")


# ---------------------------------------------------------------------------
# Legacy (group-scoped, no IAF binding) multi-receipt fixtures
# ---------------------------------------------------------------------------


def _drop_membership_freeze_triggers(conn: sqlite3.Connection) -> None:
    """FORGED-CORRUPTION FIXTURE ONLY: bypass the migration 035 membership guards.

    Production code can never do this; the drift matrix needs membership rows
    the schema itself refuses to mutate, to prove replay still fails closed on
    already-corrupt durable state.
    """
    for name in (
        "trg_receipt_participants_conversion_bound_freeze",
        "trg_receipt_participants_conversion_bound_no_delete",
        "trg_receipt_participants_conversion_bound_no_insert",
        "trg_receipt_participants_no_insert_collision",
        "trg_receipt_participants_no_update_collision",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def _seed_people(conn: sqlite3.Connection) -> None:
    for pub_id in PEOPLE:
        conn.execute(
            "INSERT OR IGNORE INTO participants (public_id, display_name, aliases, is_self)"
            " VALUES (?, ?, '[]', ?)",
            (pub_id, pub_id.split("_")[1].title(), 1 if pub_id == "person_owner" else 0),
        )


def _seed_multi_receipt_group(
    conn: sqlite3.Connection,
    *,
    group_pub_id: str,
    receipts: list[tuple[str, list[tuple[str, str, int]]]],
) -> None:
    """Seed one legacy receipt group holding several receipts.

    ``receipts`` maps each receipt public ID to its membership rows as
    ``(participant_public_id, role, is_included)`` tuples, inserted in the
    given physical order.
    """
    _seed_people(conn)
    conn.execute(
        "INSERT INTO receipt_groups (public_id, currency, status) VALUES (?, 'SGD', 'calculated')",
        (group_pub_id,),
    )
    group_id = int(
        conn.execute(
            "SELECT id FROM receipt_groups WHERE public_id = ?", (group_pub_id,)
        ).fetchone()["id"]
    )
    for seq, (receipt_pub_id, members) in enumerate(receipts, start=1):
        conn.execute(
            "INSERT INTO receipts (public_id, merchant, receipt_datetime, gross_amount,"
            " subtotal_amount, net_paid_amount, currency, payer_participant_id,"
            " source_channel, raw_input, status)"
            " VALUES (?, 'Test Merchant', '2026-01-01 12:00:00', 10.00, 10.00, 10.00, 'SGD',"
            " (SELECT id FROM participants WHERE public_id = 'person_owner'),"
            " 'manual_test_case', 'test', 'confirmed')",
            (receipt_pub_id,),
        )
        conn.execute(
            "INSERT INTO receipt_group_receipts (public_id, receipt_group_id, receipt_id,"
            " sequence_number)"
            " VALUES (?, ?, (SELECT id FROM receipts WHERE public_id = ?), ?)",
            (f"rgr_{receipt_pub_id}", group_id, receipt_pub_id, seq),
        )
        for participant_pub_id, role, is_included in members:
            conn.execute(
                "INSERT INTO receipt_participants (public_id, receipt_id, participant_id,"
                " role, is_included)"
                " VALUES (?, (SELECT id FROM receipts WHERE public_id = ?),"
                " (SELECT id FROM participants WHERE public_id = ?), ?, ?)",
                (
                    f"rp_{receipt_pub_id}_{participant_pub_id}",
                    receipt_pub_id,
                    participant_pub_id,
                    role,
                    is_included,
                ),
            )
    conn.commit()


def _two_person_snapshot(
    *,
    payer: str = "person_owner",
    debtor: str = "person_alice",
) -> dict[str, Any]:
    """A minimal deterministic snapshot: 10.00 paid, one 5.00 obligation."""
    return {
        "case_id": "pr249-membership",
        "currency": "SGD",
        "payer": payer,
        "participants": [payer, debtor],
        "participant_shares": {payer: "5.00", debtor: "5.00"},
        "total_paid": "10.00",
        "payer_own_share": "5.00",
        "payer_paid_amounts": {payer: "10.00"},
        "receipts": [],
        "settlement_obligations": [
            {"debtor": debtor, "creditor": payer, "amount": "5.00", "currency": "SGD"}
        ],
    }


def _legacy_finalize(
    conn: sqlite3.Connection,
    *,
    group_pub_id: str,
    snapshot: dict[str, Any],
    tag: str,
):
    fin_input = _build_finalization_input(
        snapshot,
        calc_pub_id=f"calc_{tag}",
        group_pub_id=group_pub_id,
        authorization_id=f"auth_{tag}",
        confirmation_id=f"conf_{tag}",
        idempotency_key=f"idem_{tag}",
    )
    _seed_authorization_for_input(
        conn,
        fin_input,
        authorization_id=f"auth_{tag}",
        confirmation_id=f"conf_{tag}",
    )
    conn.commit()
    return finalize_receipt_split(conn, fin_input)


# ---------------------------------------------------------------------------
# R3-07-1: legacy multi-receipt membership must not collapse group-wide
# ---------------------------------------------------------------------------


def test_included_on_a_excluded_on_b_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A debtor included on receipt A but excluded on receipt B is contradictory.

    The group-wide "most included wins" collapse must not let receipt A's
    inclusion authorize settlement behavior contradicted by receipt B.
    """
    conn = migrated_temp_db_connection
    _seed_multi_receipt_group(
        conn,
        group_pub_id="rg_ab_incl",
        receipts=[
            ("r_ab_incl_a", [("person_owner", "payer", 1), ("person_alice", "participant", 1)]),
            ("r_ab_incl_b", [("person_owner", "payer", 1), ("person_alice", "participant", 0)]),
        ],
    )
    before = _counts(conn, REPLAY_WRITE_TABLES)
    with pytest.raises(IneligibleForFinalizationError):
        _legacy_finalize(
            conn,
            group_pub_id="rg_ab_incl",
            snapshot=_two_person_snapshot(),
            tag="ab_incl",
        )
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert not conn.in_transaction


def test_contradictory_roles_across_receipts_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The same participant with different durable roles across receipts is refused."""
    conn = migrated_temp_db_connection
    _seed_multi_receipt_group(
        conn,
        group_pub_id="rg_ab_role",
        receipts=[
            ("r_ab_role_a", [("person_owner", "payer", 1), ("person_alice", "participant", 1)]),
            ("r_ab_role_b", [("person_owner", "payer", 1), ("person_alice", "observer", 1)]),
        ],
    )
    before = _counts(conn, REPLAY_WRITE_TABLES)
    with pytest.raises(IneligibleForFinalizationError):
        _legacy_finalize(
            conn,
            group_pub_id="rg_ab_role",
            snapshot=_two_person_snapshot(),
            tag="ab_role",
        )
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert not conn.in_transaction


def test_payer_role_contradiction_across_receipts_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A payer role valid on one receipt but demoted on another is contradictory."""
    conn = migrated_temp_db_connection
    _seed_multi_receipt_group(
        conn,
        group_pub_id="rg_ab_payer",
        receipts=[
            ("r_ab_payer_a", [("person_owner", "payer", 1), ("person_alice", "participant", 1)]),
            (
                "r_ab_payer_b",
                [("person_owner", "participant", 1), ("person_alice", "participant", 1)],
            ),
        ],
    )
    before = _counts(conn, REPLAY_WRITE_TABLES)
    with pytest.raises(IneligibleForFinalizationError):
        _legacy_finalize(
            conn,
            group_pub_id="rg_ab_payer",
            snapshot=_two_person_snapshot(),
            tag="ab_payer",
        )
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert not conn.in_transaction


def test_participant_on_single_receipt_of_group_still_finalizes(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A participant present on only one receipt has no contradiction and finalizes."""
    conn = migrated_temp_db_connection
    _seed_multi_receipt_group(
        conn,
        group_pub_id="rg_ab_single",
        receipts=[
            ("r_ab_single_a", [("person_owner", "payer", 1), ("person_alice", "participant", 1)]),
            ("r_ab_single_b", [("person_owner", "payer", 1)]),
        ],
    )
    output = _legacy_finalize(
        conn,
        group_pub_id="rg_ab_single",
        snapshot=_two_person_snapshot(),
        tag="ab_single",
    )
    assert output.status == "finalized"


def test_identical_membership_across_receipts_still_finalizes(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Unanimous membership identity across receipts remains finalizable."""
    conn = migrated_temp_db_connection
    _seed_multi_receipt_group(
        conn,
        group_pub_id="rg_ab_same",
        receipts=[
            ("r_ab_same_a", [("person_owner", "payer", 1), ("person_alice", "participant", 1)]),
            ("r_ab_same_b", [("person_owner", "payer", 1), ("person_alice", "participant", 1)]),
        ],
    )
    output = _legacy_finalize(
        conn,
        group_pub_id="rg_ab_same",
        snapshot=_two_person_snapshot(),
        tag="ab_same",
    )
    assert output.status == "finalized"


def test_legacy_finalization_records_group_scoped_membership_evidence(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Legacy (non-IAF) finalizations record group-scoped evidence, not receipt-scoped.

    No single receipt owns a legacy membership decision, so the durable evidence
    must say so: scope ``receipt_group`` with no owning receipt.
    """
    conn = migrated_temp_db_connection
    _seed_multi_receipt_group(
        conn,
        group_pub_id="rg_ab_scope",
        receipts=[
            ("r_ab_scope_a", [("person_owner", "payer", 1), ("person_alice", "participant", 1)]),
            ("r_ab_scope_b", [("person_owner", "payer", 1), ("person_alice", "participant", 1)]),
        ],
    )
    output = _legacy_finalize(
        conn,
        group_pub_id="rg_ab_scope",
        snapshot=_two_person_snapshot(),
        tag="ab_scope",
    )
    rows = _membership_evidence_rows(conn, output.finalization_public_id)
    assert [row["participant_public_id"] for row in rows] == ["person_alice", "person_owner"]
    for row in rows:
        assert row["membership_scope"] == "receipt_group"
        assert row["receipt_public_id"] is None
        assert row["receipt_group_public_id"] == "rg_ab_scope"


def test_physical_row_order_does_not_change_the_refusal(
    tmp_path: Path,
) -> None:
    """Contradictory membership fails closed regardless of physical row order."""
    from tests.conftest import apply_migrations, connect_temp_db

    outcomes: list[str] = []
    for order_tag, members_a, members_b in (
        (
            "fwd",
            [("person_owner", "payer", 1), ("person_alice", "participant", 1)],
            [("person_owner", "payer", 1), ("person_alice", "participant", 0)],
        ),
        (
            "rev",
            [("person_owner", "payer", 1), ("person_alice", "participant", 0)],
            [("person_owner", "payer", 1), ("person_alice", "participant", 1)],
        ),
    ):
        conn = connect_temp_db(tmp_path / f"order_{order_tag}.db")
        try:
            apply_migrations(conn)
            conn.commit()
            _seed_multi_receipt_group(
                conn,
                group_pub_id="rg_order",
                receipts=[("r_order_a", members_a), ("r_order_b", members_b)],
            )
            try:
                _legacy_finalize(
                    conn,
                    group_pub_id="rg_order",
                    snapshot=_two_person_snapshot(),
                    tag="order",
                )
                outcomes.append("finalized")
            except IneligibleForFinalizationError:
                outcomes.append("refused")
        finally:
            conn.close()
    assert outcomes == ["refused", "refused"]


def test_membership_only_on_foreign_receipt_is_refused(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Membership on a receipt outside the group never authorizes finalization."""
    conn = migrated_temp_db_connection
    _seed_multi_receipt_group(
        conn,
        group_pub_id="rg_foreign_main",
        receipts=[("r_foreign_main", [("person_owner", "payer", 1)])],
    )
    # Alice's only membership row lives on a receipt in an unrelated group.
    _seed_multi_receipt_group(
        conn,
        group_pub_id="rg_foreign_other",
        receipts=[("r_foreign_other", [("person_alice", "participant", 1)])],
    )
    before = _counts(conn, REPLAY_WRITE_TABLES)
    with pytest.raises(IneligibleForFinalizationError):
        _legacy_finalize(
            conn,
            group_pub_id="rg_foreign_main",
            snapshot=_two_person_snapshot(),
            tag="foreign",
        )
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert not conn.in_transaction


def test_excluded_payer_with_nonzero_consumer_share_is_refused(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """R3-06 freeze: an excluded payer must never carry a nonzero consumer share."""
    conn = migrated_temp_db_connection
    _seed_multi_receipt_group(
        conn,
        group_pub_id="rg_expayer",
        receipts=[
            ("r_expayer", [("person_owner", "payer", 0), ("person_alice", "participant", 1)])
        ],
    )
    before = _counts(conn, REPLAY_WRITE_TABLES)
    with pytest.raises(IneligibleForFinalizationError):
        _legacy_finalize(
            conn,
            group_pub_id="rg_expayer",
            snapshot=_two_person_snapshot(),
            tag="expayer",
        )
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# IAF path: durable receipt-scoped membership evidence (migration 039)
# ---------------------------------------------------------------------------


def _finalized_iaf_receipt(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str
) -> tuple[Any, Any, Any]:
    ctx, _ = _setup_active_fact_set(conn, tmp_path, suffix)
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization)
    assert output.status == "finalized"
    return ctx, authorization, output


def _membership_evidence_rows(
    conn: sqlite3.Connection, finalization_id: str
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT membership_scope, receipt_public_id, receipt_group_public_id,"
            f" participant_public_id, role, is_included"
            f" FROM {MEMBERSHIP_EVIDENCE_TABLE} WHERE finalization_id = ?"
            f" ORDER BY participant_public_id",
            (finalization_id,),
        ).fetchall()
    ]


def test_iaf_finalization_records_receipt_scoped_membership_evidence(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Every IAF finalization records one receipt-scoped evidence row per participant."""
    conn = migrated_temp_db_connection
    ctx, _authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msevid")

    rows = _membership_evidence_rows(conn, output.finalization_public_id)
    assert [row["participant_public_id"] for row in rows] == ["person_alice", "person_owner"]
    for row in rows:
        assert row["membership_scope"] == "receipt"
        assert row["receipt_public_id"] == ctx.receipt_public_id
        live = conn.execute(
            "SELECT rp.role, rp.is_included FROM receipt_participants rp"
            " JOIN receipts r ON r.id = rp.receipt_id"
            " JOIN participants p ON p.id = rp.participant_id"
            " WHERE r.public_id = ? AND p.public_id = ?",
            (ctx.receipt_public_id, row["participant_public_id"]),
        ).fetchone()
        assert live is not None
        assert row["role"] == live["role"]
        assert int(row["is_included"]) == int(live["is_included"])


def test_membership_evidence_rows_are_append_only(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Migration 039 evidence rows refuse UPDATE and DELETE at the schema level."""
    conn = migrated_temp_db_connection
    _ctx, _authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msimmut")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"UPDATE {MEMBERSHIP_EVIDENCE_TABLE} SET is_included = 0 WHERE finalization_id = ?",
            (output.finalization_public_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"DELETE FROM {MEMBERSHIP_EVIDENCE_TABLE} WHERE finalization_id = ?",
            (output.finalization_public_id,),
        )


# ---------------------------------------------------------------------------
# IAF replay: membership drift and leakage fail closed with zero writes
# ---------------------------------------------------------------------------


def _assert_zero_write_replay_refusal(conn: sqlite3.Connection, authorization: Any) -> None:
    before = _counts(conn, REPLAY_WRITE_TABLES)
    before_state = _lifecycle_state(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert _lifecycle_state(conn) == before_state
    assert not conn.in_transaction


def _assert_replay_truth_mismatch(
    conn: sqlite3.Connection, authorization: Any, *, match: str
) -> None:
    """Assert the typed replay failure *and* the branch that produced it.

    ``FAIL_CLOSED_ERRORS`` alone would also accept a refusal raised by an
    earlier graph node, which would silently un-cover the branch under test.
    """
    before = _counts(conn, REPLAY_WRITE_TABLES)
    before_state = _lifecycle_state(conn)
    with pytest.raises(FinalizationIdempotencyError, match=match) as excinfo:
        finalize_prepared_receipt(conn, authorization)
    assert excinfo.value.reason == FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert _lifecycle_state(conn) == before_state
    assert not conn.in_transaction


def test_replay_accepts_intact_receipt_scoped_membership(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msok")
    before = _counts(conn, REPLAY_WRITE_TABLES)
    before_state = _lifecycle_state(conn)
    replay = finalize_prepared_receipt(conn, authorization)
    assert replay.status == "already_finalized"
    assert replay.finalization_public_id == output.finalization_public_id
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    # An accepted replay is also strictly zero-write, lifecycle state included.
    assert _lifecycle_state(conn) == before_state


def test_replay_refuses_missing_membership_row(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: deleting a consumed membership row breaks replay."""
    conn = migrated_temp_db_connection
    ctx, authorization, _output = _finalized_iaf_receipt(conn, tmp_path, "msdel")
    _drop_membership_freeze_triggers(conn)
    conn.execute(
        "DELETE FROM receipt_participants WHERE receipt_id ="
        " (SELECT id FROM receipts WHERE public_id = ?)"
        " AND participant_id = (SELECT id FROM participants WHERE public_id = 'person_alice')",
        (ctx.receipt_public_id,),
    )
    conn.commit()
    _assert_zero_write_replay_refusal(conn, authorization)


def test_replay_refuses_membership_role_flip(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: flipping a durable membership role breaks replay."""
    conn = migrated_temp_db_connection
    ctx, authorization, _output = _finalized_iaf_receipt(conn, tmp_path, "msrole")
    _drop_membership_freeze_triggers(conn)
    conn.execute(
        "UPDATE receipt_participants SET role = 'observer' WHERE receipt_id ="
        " (SELECT id FROM receipts WHERE public_id = ?)"
        " AND participant_id = (SELECT id FROM participants WHERE public_id = 'person_alice')",
        (ctx.receipt_public_id,),
    )
    conn.commit()
    _assert_zero_write_replay_refusal(conn, authorization)


def test_replay_refuses_membership_inclusion_flip(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: excluding a consumed debtor after finalization breaks replay."""
    conn = migrated_temp_db_connection
    ctx, authorization, _output = _finalized_iaf_receipt(conn, tmp_path, "msincl")
    _drop_membership_freeze_triggers(conn)
    conn.execute(
        "UPDATE receipt_participants SET is_included = 0 WHERE receipt_id ="
        " (SELECT id FROM receipts WHERE public_id = ?)"
        " AND participant_id = (SELECT id FROM participants WHERE public_id = 'person_alice')",
        (ctx.receipt_public_id,),
    )
    conn.commit()
    _assert_zero_write_replay_refusal(conn, authorization)


def test_replay_refuses_membership_retargeted_to_foreign_receipt(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: membership moved to a foreign receipt must not leak back.

    Receipt B is forged into the settled group and Alice's membership row is
    retargeted from the bound receipt to receipt B.  Under the receipt-scoped
    contract the bound receipt no longer carries Alice's membership, so replay
    must fail closed instead of accepting receipt B's row.
    """
    conn = migrated_temp_db_connection
    ctx, authorization, _output = _finalized_iaf_receipt(conn, tmp_path, "msretgt")
    _seed_people(conn)
    _drop_membership_freeze_triggers(conn)
    conn.execute(
        "INSERT INTO receipts (public_id, merchant, receipt_datetime, gross_amount,"
        " subtotal_amount, net_paid_amount, currency, payer_participant_id,"
        " source_channel, raw_input, status)"
        " VALUES ('r_msretgt_forged', 'Forged', '2026-01-02 12:00:00', 9.99, 9.99, 9.99,"
        " 'SGD', (SELECT id FROM participants WHERE public_id = 'person_owner'),"
        " 'manual_test_case', 'forged', 'confirmed')"
    )
    conn.execute(
        "INSERT INTO receipt_group_receipts (public_id, receipt_group_id, receipt_id,"
        " sequence_number)"
        " VALUES ('rgr_msretgt_forged',"
        " (SELECT id FROM receipt_groups WHERE public_id = ?),"
        " (SELECT id FROM receipts WHERE public_id = 'r_msretgt_forged'), 2)",
        (f"rgrp_{ctx.receipt_public_id}",),
    )
    conn.execute(
        "UPDATE receipt_participants SET receipt_id ="
        " (SELECT id FROM receipts WHERE public_id = 'r_msretgt_forged')"
        " WHERE receipt_id = (SELECT id FROM receipts WHERE public_id = ?)"
        " AND participant_id = (SELECT id FROM participants WHERE public_id = 'person_alice')",
        (ctx.receipt_public_id,),
    )
    conn.commit()
    _assert_zero_write_replay_refusal(conn, authorization)


def test_replay_refuses_forged_membership_evidence(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: evidence retargeted to a foreign receipt breaks replay."""
    conn = migrated_temp_db_connection
    ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msfevid")
    # Bypass the migration 039 append-only guards -- production code cannot.
    _unfreeze_membership_evidence(conn)
    _seed_people(conn)
    conn.execute(
        "INSERT INTO receipts (public_id, merchant, receipt_datetime, gross_amount,"
        " subtotal_amount, net_paid_amount, currency, payer_participant_id,"
        " source_channel, raw_input, status)"
        " VALUES ('r_msfevid_forged', 'Forged', '2026-01-02 12:00:00', 9.99, 9.99, 9.99,"
        " 'SGD', (SELECT id FROM participants WHERE public_id = 'person_owner'),"
        " 'manual_test_case', 'forged', 'confirmed')"
    )
    conn.execute(
        f"UPDATE {MEMBERSHIP_EVIDENCE_TABLE} SET receipt_public_id = 'r_msfevid_forged'"
        f" WHERE finalization_id = ? AND participant_public_id = 'person_alice'",
        (output.finalization_public_id,),
    )
    conn.commit()
    assert ctx.receipt_public_id != "r_msfevid_forged"
    _assert_replay_truth_mismatch(
        conn,
        authorization,
        match="is bound to receipt 'r_msfevid_forged', not the snapshot-bound receipt",
    )


def _unfreeze_membership_evidence(conn: sqlite3.Connection) -> None:
    """FORGED-CORRUPTION FIXTURE ONLY: bypass the migration 039 append-only guards.

    Production code can never mutate or delete membership evidence.  The drift
    matrix needs already-corrupt durable evidence to prove every branch of the
    replay verifier fails closed rather than trusting the forged row.
    """
    for name in (
        "trg_rfme_no_update",
        "trg_rfme_no_delete",
        "trg_rfme_no_insert_collision",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def test_replay_refuses_partially_deleted_membership_evidence(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: dropping one evidence row leaves the participant set short."""
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "mspartial")
    _unfreeze_membership_evidence(conn)
    conn.execute(
        f"DELETE FROM {MEMBERSHIP_EVIDENCE_TABLE} WHERE finalization_id = ?"
        f" AND participant_public_id = 'person_alice'",
        (output.finalization_public_id,),
    )
    conn.commit()
    assert len(_membership_evidence_rows(conn, output.finalization_public_id)) == 1
    _assert_replay_truth_mismatch(
        conn, authorization, match="participant set does not match the referenced participants"
    )


def test_replay_refuses_membership_evidence_role_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: evidence role that disagrees with the live row breaks replay."""
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msevrole")
    _unfreeze_membership_evidence(conn)
    conn.execute(
        f"UPDATE {MEMBERSHIP_EVIDENCE_TABLE} SET role = 'observer'"
        f" WHERE finalization_id = ? AND participant_public_id = 'person_alice'",
        (output.finalization_public_id,),
    )
    conn.commit()
    _assert_replay_truth_mismatch(
        conn, authorization, match="drifted from the durable membership evidence"
    )


def test_replay_refuses_membership_evidence_inclusion_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: evidence inclusion flip against the live row breaks replay."""
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msevincl")
    _unfreeze_membership_evidence(conn)
    conn.execute(
        f"UPDATE {MEMBERSHIP_EVIDENCE_TABLE} SET is_included = 0"
        f" WHERE finalization_id = ? AND participant_public_id = 'person_alice'",
        (output.finalization_public_id,),
    )
    conn.commit()
    _assert_replay_truth_mismatch(
        conn, authorization, match="drifted from the durable membership evidence"
    )


def test_replay_refuses_membership_evidence_scope_downgrade(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: an IAF finalization may not claim group-wide membership scope."""
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msevscope")
    _unfreeze_membership_evidence(conn)
    # The scope/receipt CHECK forces both columns to move together.
    conn.execute(
        f"UPDATE {MEMBERSHIP_EVIDENCE_TABLE}"
        f" SET membership_scope = 'receipt_group', receipt_public_id = NULL"
        f" WHERE finalization_id = ?",
        (output.finalization_public_id,),
    )
    conn.commit()
    _assert_replay_truth_mismatch(
        conn, authorization, match="scope for 'person_alice' is 'receipt_group', expected 'receipt'"
    )


def test_replay_refuses_membership_evidence_for_foreign_group(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: evidence retargeted to another group breaks replay."""
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msevgrp")
    _unfreeze_membership_evidence(conn)
    conn.execute(
        "INSERT INTO receipt_groups (public_id, currency, status)"
        " VALUES ('rg_msevgrp_forged', 'SGD', 'settled')"
    )
    conn.execute(
        f"UPDATE {MEMBERSHIP_EVIDENCE_TABLE} SET receipt_group_public_id = 'rg_msevgrp_forged'"
        f" WHERE finalization_id = ? AND participant_public_id = 'person_alice'",
        (output.finalization_public_id,),
    )
    conn.commit()
    _assert_replay_truth_mismatch(
        conn, authorization, match="evidence for 'person_alice' targets a foreign group"
    )


def test_replay_refuses_extra_membership_evidence_row(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: evidence for an unreferenced participant breaks replay."""
    conn = migrated_temp_db_connection
    ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msevextra")
    _unfreeze_membership_evidence(conn)
    _seed_people(conn)
    group_pub_id = str(
        conn.execute(
            "SELECT rg.public_id FROM receipt_groups rg"
            " JOIN receipt_group_receipts rgr ON rgr.receipt_group_id = rg.id"
            " JOIN receipts r ON r.id = rgr.receipt_id"
            " WHERE r.public_id = ?",
            (ctx.receipt_public_id,),
        ).fetchone()["public_id"]
    )
    conn.execute(
        f"INSERT INTO {MEMBERSHIP_EVIDENCE_TABLE} (membership_evidence_public_id,"
        f" finalization_id, receipt_group_public_id, membership_scope, receipt_public_id,"
        f" participant_public_id, role, is_included, created_at)"
        f" VALUES ('rfme_msevextra_forged', ?, ?, 'receipt', ?, 'person_bob', 'participant', 1,"
        f" '2026-07-30T00:00:00+00:00')",
        (output.finalization_public_id, group_pub_id, ctx.receipt_public_id),
    )
    conn.commit()
    _assert_replay_truth_mismatch(
        conn, authorization, match="references 'person_bob', which is no longer a resolvable member"
    )


def test_replay_accepts_absent_membership_evidence_as_pre_039_finalization(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: pins the documented pre-039 fallback as a deliberate choice.

    A finalization committed before migration 039 legitimately carries no
    membership evidence, so absence falls back to live receipt-scoped
    verification instead of refusing every historical replay.  While the
    append-only triggers are in place production code cannot delete evidence,
    so absence is only reachable for pre-039 rows or by out-of-band DDL
    tampering (making that absence falsifiable is a recorded follow-up).  This
    test freezes the accepted trade-off so it cannot change silently.
    """
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msevnone")
    _unfreeze_membership_evidence(conn)
    conn.execute(
        f"DELETE FROM {MEMBERSHIP_EVIDENCE_TABLE} WHERE finalization_id = ?",
        (output.finalization_public_id,),
    )
    conn.commit()
    before = _counts(conn, REPLAY_WRITE_TABLES)
    before_state = _lifecycle_state(conn)
    replay = finalize_prepared_receipt(conn, authorization)
    assert replay.status == "already_finalized"
    assert replay.finalization_public_id == output.finalization_public_id
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert _lifecycle_state(conn) == before_state


def test_absent_membership_evidence_still_verifies_live_membership(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: the pre-039 fallback keeps its compensating control.

    Absence of evidence must fall back to live receipt-scoped verification, not
    skip membership verification altogether: with the evidence gone AND the
    live membership drifted, replay must still fail closed.
    """
    conn = migrated_temp_db_connection
    ctx, authorization, output = _finalized_iaf_receipt(conn, tmp_path, "msevboth")
    _unfreeze_membership_evidence(conn)
    conn.execute(
        f"DELETE FROM {MEMBERSHIP_EVIDENCE_TABLE} WHERE finalization_id = ?",
        (output.finalization_public_id,),
    )
    _drop_membership_freeze_triggers(conn)
    conn.execute(
        "UPDATE receipt_participants SET is_included = 0 WHERE receipt_id ="
        " (SELECT id FROM receipts WHERE public_id = ?)"
        " AND participant_id = (SELECT id FROM participants WHERE public_id = 'person_alice')",
        (ctx.receipt_public_id,),
    )
    conn.commit()
    assert _membership_evidence_rows(conn, output.finalization_public_id) == []
    _assert_replay_truth_mismatch(
        conn, authorization, match="Replay receipt-scoped membership verification failed"
    )
