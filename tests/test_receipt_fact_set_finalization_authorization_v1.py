"""IAF.7 human finalization authorization tests.

Covers the authorization stage of the locked bridge design
(``docs/design/receipt_fact_set_calculation_finalization_bridge_v1.md``,
Section 3 and Section 10):

- only a human actor may create a finalization authorization; system / cli /
  agent actors and empty actor ids fail closed with zero persisted records;
- authorization + confirmation records are created with the finalization
  content hash and the ``authorized`` / ``confirmed`` states;
- the authorization content hash equals the finalization fingerprint and is
  bound to the active fact-set four-tuple (a different fact set yields a
  different content hash);
- the four-tuple binding strings are persisted in the authorization source
  evidence;
- authorization is idempotent; finalization consumes the authorization.

Fixtures are built through the public B1 -> confirmation -> B4.1 -> IAF
boundaries.  Only disposable staging databases are used; ``database/finance.db``
and seed data are untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from finance_core.parser_proposals.receipt_item_allocation_facts import (
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
)
from finance_core.receipt_finalization import (
    BridgeAuthorizationActorError,
    BridgeAuthorizationConflictError,
    FinalizationInput,
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.fact_set_bridge import _build_finalization_input
from finance_core.receipt_finalization.models import build_finalization_content_fingerprint
from tests.test_receipt_item_allocation_facts_service_v1 import iaf_command, setup_receipt
from tests.test_receipt_item_allocation_facts_supersession_v1 import correction_command


def _setup_active_fact_set(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str
) -> tuple[Any, Any]:
    ctx = setup_receipt(conn, tmp_path, suffix)
    result = persist_receipt_item_allocation_facts(conn, iaf_command(suffix, ctx))
    conn.commit()
    return ctx, result


def _authorization_row(conn: sqlite3.Connection, authorization_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM receipt_finalization_authorizations WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    return None if row is None else dict(row)


def _confirmation_row(conn: sqlite3.Connection, confirmation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM receipt_finalization_confirmations WHERE confirmation_id = ?",
        (confirmation_id,),
    ).fetchone()
    return None if row is None else dict(row)


def _record_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    confirmations = int(
        conn.execute("SELECT COUNT(*) FROM receipt_finalization_confirmations").fetchone()[0]
    )
    authorizations = int(
        conn.execute("SELECT COUNT(*) FROM receipt_finalization_authorizations").fetchone()[0]
    )
    return confirmations, authorizations


# ---------------------------------------------------------------------------
# Human-only authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("actor_type", ["system", "cli", "agent"])
def test_authorize_rejects_non_human_actor(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, actor_type: str
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, f"actor_{actor_type}")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    before = _record_counts(conn)
    with pytest.raises(BridgeAuthorizationActorError):
        authorize_receipt_finalization(conn, prepared, actor_id="owner", actor_type=actor_type)
    assert _record_counts(conn) == before
    assert not conn.in_transaction


@pytest.mark.parametrize("actor_id", ["", "   "])
def test_authorize_rejects_empty_actor_id(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, actor_id: str
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "emptyactor")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    before = _record_counts(conn)
    with pytest.raises(BridgeAuthorizationActorError):
        authorize_receipt_finalization(conn, prepared, actor_id=actor_id)
    assert _record_counts(conn) == before


# ---------------------------------------------------------------------------
# Authorization record content
# ---------------------------------------------------------------------------


def test_authorize_creates_confirmation_and_authorization(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "create")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    conf = _confirmation_row(conn, prepared.confirmation_id)
    auth = _authorization_row(conn, prepared.authorization_id)
    assert conf is not None and auth is not None
    assert conf["confirmation_state"] == "confirmed"
    assert auth["authorization_state"] == "authorized"
    assert conf["actor_type"] == "human"
    assert auth["actor_type"] == "human"
    assert auth["actor_id"] == "owner"
    assert conf["content_hash"] == authorization.content_hash
    assert auth["content_hash"] == authorization.content_hash
    assert auth["confirmation_id"] == prepared.confirmation_id
    assert auth["calculation_snapshot_id"] == prepared.calculation_snapshot_id


def test_authorization_content_hash_equals_finalization_fingerprint(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "fp")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    fin_input = _build_finalization_input(prepared, actor_type="human", actor_id="owner")
    assert authorization.content_hash == build_finalization_content_fingerprint(fin_input)
    assert len(authorization.content_hash) == 64


def test_authorization_content_hash_binds_four_tuple(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(conn, tmp_path, "bind4")

    prepared_v1 = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization_v1 = authorize_receipt_finalization(conn, prepared_v1, actor_id="owner")

    supersede_receipt_item_allocation_facts(conn, correction_command("bind4", ctx, result))
    conn.commit()
    prepared_v2 = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization_v2 = authorize_receipt_finalization(conn, prepared_v2, actor_id="owner")

    # A different active fact-set four-tuple must change the content hash.
    assert prepared_v1.active_fact_set_binding != prepared_v2.active_fact_set_binding
    assert authorization_v1.content_hash != authorization_v2.content_hash


def test_authorization_persists_four_tuple_source_evidence(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(conn, tmp_path, "evi")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorize_receipt_finalization(conn, prepared, actor_id="owner")

    auth = _authorization_row(conn, prepared.authorization_id)
    assert auth is not None
    evidence_json = auth["source_evidence_refs_json"]
    assert result.fact_set_public_id in evidence_json
    assert result.fact_set_result_hash in evidence_json
    assert ctx.receipt_public_id in evidence_json


# ---------------------------------------------------------------------------
# Idempotency + consumption
# ---------------------------------------------------------------------------


def test_authorize_is_idempotent(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "authidem")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    first = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    second = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    assert first.authorization_id == second.authorization_id
    assert first.content_hash == second.content_hash
    assert _record_counts(conn) == (1, 1)


def test_authorize_conflicting_actor_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """INSERT OR IGNORE keeps the first durable authorization; a different
    human actor for the same identity is refused and the record untouched."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "actorconf")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorize_receipt_finalization(conn, prepared, actor_id="owner")

    with pytest.raises(BridgeAuthorizationConflictError):
        authorize_receipt_finalization(conn, prepared, actor_id="mallory")

    auth = _authorization_row(conn, prepared.authorization_id)
    assert auth is not None
    assert auth["actor_id"] == "owner"
    assert auth["authorization_state"] == "authorized"
    assert _record_counts(conn) == (1, 1)
    assert not conn.in_transaction


def test_fingerprint_domain_separation_from_legacy(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A binding-absent (legacy) input keeps its legacy fingerprint shape and
    can never match an IAF authorization content hash (fail closed)."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "domain")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    with_binding = _build_finalization_input(prepared, actor_type="human", actor_id="owner")
    without_binding = FinalizationInput(
        calculation_run_public_id=prepared.calculation_run_public_id,
        receipt_group_public_id=prepared.receipt_group_public_id,
        currency=prepared.currency,
        payer_participant_public_id=prepared.payer_participant_public_id,
        settlement_obligations=with_binding.settlement_obligations,
        calculation_snapshot=prepared.calculation_result,
        authorization_id=prepared.authorization_id,
        confirmation_id=prepared.confirmation_id,
        idempotency_key=prepared.idempotency_key,
        calculation_snapshot_id=prepared.calculation_snapshot_id,
        calculation_snapshot_hash=prepared.calculation_snapshot_hash,
        currency_contract_version=prepared.currency_contract_version,
        actor_type="human",
        actor_id="owner",
        source_evidence_refs=prepared.source_evidence_refs,
        active_fact_set_binding=None,
    )
    assert build_finalization_content_fingerprint(
        with_binding
    ) != build_finalization_content_fingerprint(without_binding)


def test_finalize_consumes_authorization(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "consume")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    auth_before = _authorization_row(conn, prepared.authorization_id)
    assert auth_before is not None
    assert auth_before["authorization_state"] == "authorized"

    finalize_prepared_receipt(conn, authorization)

    auth_after = _authorization_row(conn, prepared.authorization_id)
    assert auth_after is not None
    assert auth_after["authorization_state"] == "consumed"
