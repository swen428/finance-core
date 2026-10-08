"""Independent receipt authority readers reject substituted historical authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from finance_core.receipt_finalization import (
    FinalizationAuthorizationError,
    FinalizationIdempotencyError,
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    finalize_receipt_split,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.application_conditional import (
    APPLICATION_CONDITIONAL_VERSION,
    ApplicationConditionalAuthorityError,
    require_application_conditional_authority,
)
from finance_core.receipt_finalization.fact_set_bridge import (
    BridgeAuthorizationConflictError,
    BridgeRecoveryError,
    _build_finalization_input,
    authorize_application_conditional_receipt_finalization,
    load_persisted_receipt_finalization_authorization,
    verify_finalized_prepared_receipt,
)
from tests.test_receipt_fact_set_calculation_bridge_v1 import _setup_active_fact_set


@pytest.mark.parametrize("reader", ["fresh", "replay", "recovery"])
def test_substituted_manual_authority_requires_independent_proof(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, reader: str
) -> None:
    """All three readers exercise real owning services and missing proof refusal."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, f"independent_{reader}")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    if reader == "replay":
        finalize_prepared_receipt(conn, authorization)
    # Deliberate corruption in a disposable DB: a version relabel cannot
    # promote an old human authorization into independent decision authority.
    conn.execute(
        "UPDATE receipt_finalization_authorizations SET authorization_version = ? "
        "WHERE authorization_id = ?",
        (APPLICATION_CONDITIONAL_VERSION, prepared.authorization_id),
    )
    conn.commit()
    fin_input = _build_finalization_input(prepared, actor_type="human", actor_id="owner")
    if reader == "recovery":
        with pytest.raises(BridgeRecoveryError, match="Independent conditional proof is missing"):
            load_persisted_receipt_finalization_authorization(conn, prepared.authorization_id)
    elif reader == "fresh":
        with pytest.raises(
            FinalizationAuthorizationError, match="Independent conditional proof is missing"
        ):
            finalize_receipt_split(conn, fin_input)
    else:
        with pytest.raises(
            FinalizationIdempotencyError, match="Independent conditional proof is missing"
        ):
            finalize_receipt_split(conn, fin_input)
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == (
        1 if reader == "replay" else 0
    )


def test_independent_bridge_cannot_adopt_manual_confirmation(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "independent_missing_acceptance")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    with pytest.raises(BridgeAuthorizationConflictError, match="acceptance is missing"):
        authorize_application_conditional_receipt_finalization(
            conn,
            prepared,
            attempt_id="unaccepted-attempt",
            persistence_effect=lambda connection, result: None,
        )
    assert not conn.in_transaction
    assert (
        conn.execute("SELECT count(*) FROM receipt_finalization_authorizations").fetchone()[0] == 0
    )
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_missing_proof_validation_preserves_caller_transaction(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(ApplicationConditionalAuthorityError, match="proof is missing"):
        require_application_conditional_authority(conn, {"authorization_id": "missing"})
    assert conn.in_transaction
    conn.rollback()


def test_finalizer_revalidation_failure_rolls_back_complete_financial_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "effect_failure")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    def reject_locked_write(connection, result):
        assert connection is conn and connection.in_transaction
        assert (
            connection.execute(
                "SELECT count(*) FROM transactions WHERE public_id = ?",
                (result.transaction_public_id,),
            ).fetchone()[0]
            == 1
        )
        raise ValueError("durable human/source evidence changed")

    with pytest.raises(ValueError, match="human/source evidence changed"):
        finalize_prepared_receipt(conn, authorization, persistence_effect=reject_locked_write)
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM receipt_finalization_audit").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM receipt_groups").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT authorization_state FROM receipt_finalization_authorizations"
        ).fetchone()[0]
        == "authorized"
    )


def test_finalizer_replay_revalidation_runs_inside_coherent_snapshot(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "effect_replay")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    final = finalize_prepared_receipt(conn, authorization)
    observed = []

    def verify_locked_replay(connection, result):
        assert connection is conn and connection.in_transaction
        observed.append(result.transaction_public_id)

    replay = finalize_prepared_receipt(conn, authorization, persistence_effect=verify_locked_replay)
    assert observed == [final.transaction_public_id]
    assert replay.transaction_public_id == final.transaction_public_id
    assert replay.status == "already_finalized"
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1


def test_completed_independent_receipt_replays_after_payer_rights_change(tmp_path: Path) -> None:
    from tests.test_application_posting_recovery_v1 import _prepare_receipt_subject

    conn, _, _, service, review, _, _, decision_id, *_ = _prepare_receipt_subject(
        tmp_path, "proof_history"
    )
    try:
        posted = service.submit_post(review.review_id, decision_id)
        assert posted.state == "finalized"
        auth_id = conn.execute(
            "SELECT authorization_id FROM application_conditional_authorization_proofs"
        ).fetchone()[0]
        conn.execute("UPDATE participants SET is_self = 0, is_active = 0")
        conn.commit()
        verified = verify_finalized_prepared_receipt(conn, auth_id)
        loaded = load_persisted_receipt_finalization_authorization(conn, auth_id)
        replay = finalize_prepared_receipt(conn, loaded)
        assert verified.transaction_public_id == replay.transaction_public_id
        assert replay.transaction_public_id == posted.transaction_public_id
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    "column,value",
    [
        ("attempt_id", "missing-attempt"),
        ("authorization_id", "missing-authorization"),
        ("review_id", "missing-review"),
        ("confirmation_public_id", "missing-confirmation"),
        ("fact_set_public_id", "missing-fact-set"),
        ("fact_set_version", 2),
        ("fact_set_input_hash", "0" * 64),
        ("fact_set_result_hash", "0" * 64),
        ("calculation_snapshot_id", "missing-snapshot"),
        ("calculation_snapshot_hash", "0" * 64),
        ("reviewed_projection_hash", "0" * 64),
        ("snapshot_projection_hash", "0" * 64),
        ("payer_participant_public_id", "missing-payer"),
        ("participant_authority_hash", "0" * 64),
        ("equality_proof_hash", "0" * 64),
        ("payer_was_active_self", 0),
        ("active_self_count", 2),
        ("proof_version", "d2_conditional_v1"),
        ("created_at", "2000-01-01T00:00:00+00:00"),
    ],
)
@pytest.mark.parametrize("phase", ["fresh", "replay"])
def test_independent_completed_proof_tamper_refuses_all_replay_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str, value: str | int, phase: str
) -> None:
    from finance_core.application import posting
    from tests.test_application_posting_recovery_v1 import _prepare_receipt_subject

    conn, _, _, service, review, _, _, decision_id, *_ = _prepare_receipt_subject(
        tmp_path, "proof_tamper"
    )
    try:
        if phase == "fresh":

            def stop_before_finalization(stage):
                if stage == "after_conditional_authorization_commit":
                    raise RuntimeError("stop before finalization")

            monkeypatch.setattr(posting, "_failure_injection_hook", stop_before_finalization)
            with pytest.raises(RuntimeError, match="stop before finalization"):
                service.submit_post(review.review_id, decision_id)
        else:
            service.submit_post(review.review_id, decision_id)
        auth_id = conn.execute(
            "SELECT authorization_id FROM application_conditional_authorization_proofs"
        ).fetchone()[0]
        loaded = load_persisted_receipt_finalization_authorization(conn, auth_id)
        # Simulate storage corruption, then restore normal constraints/triggers.
        # Refusal must come from proof validation rather than a disabled-schema gate.
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'application_conditional_proofs_no_update'"
        ).fetchone()[0]
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("DROP TRIGGER application_conditional_proofs_no_update")
        conn.execute(
            f"UPDATE application_conditional_authorization_proofs SET {column} = ?", (value,)
        )
        conn.execute(trigger_sql)
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA ignore_check_constraints = OFF")
        with pytest.raises(BridgeRecoveryError):
            load_persisted_receipt_finalization_authorization(conn, auth_id)
        error = FinalizationAuthorizationError if phase == "fresh" else FinalizationIdempotencyError
        with pytest.raises(error):
            finalize_prepared_receipt(conn, loaded)
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == (
            0 if phase == "fresh" else 1
        )
    finally:
        conn.close()


@pytest.mark.parametrize("version", ["v1", "d2_conditional_v1"])
@pytest.mark.parametrize("phase", ["fresh", "replay"])
def test_independent_authorization_cannot_be_downgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, phase: str
) -> None:
    from finance_core.application import posting
    from tests.test_application_posting_recovery_v1 import _prepare_receipt_subject

    conn, _, _, service, review, _, _, decision_id, *_ = _prepare_receipt_subject(
        tmp_path, "proof_downgrade"
    )
    try:
        if phase == "fresh":

            def stop_before_finalization(stage):
                if stage == "after_conditional_authorization_commit":
                    raise RuntimeError("stop before finalization")

            monkeypatch.setattr(posting, "_failure_injection_hook", stop_before_finalization)
            with pytest.raises(RuntimeError, match="stop before finalization"):
                service.submit_post(review.review_id, decision_id)
        else:
            service.submit_post(review.review_id, decision_id)
        auth_id = conn.execute(
            "SELECT authorization_id FROM application_conditional_authorization_proofs"
        ).fetchone()[0]
        loaded = load_persisted_receipt_finalization_authorization(conn, auth_id)
        conn.execute(
            "UPDATE receipt_finalization_authorizations SET authorization_version = ?",
            (version,),
        )
        conn.commit()
        with pytest.raises(BridgeRecoveryError, match="cannot use manual or D2"):
            load_persisted_receipt_finalization_authorization(conn, auth_id)
        error = FinalizationAuthorizationError if phase == "fresh" else FinalizationIdempotencyError
        with pytest.raises(error, match="cannot use manual or D2"):
            finalize_prepared_receipt(conn, loaded)
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == (
            0 if phase == "fresh" else 1
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "column,value",
    [
        ("confirmation_channel", "manual"),
        ("authenticated_actor_id", "another-human"),
        ("proposal_content_hash", "0" * 64),
        ("confirmation_state", "rejected"),
        ("revoked_at", "1970-01-01T00:33:21+00:00"),
    ],
)
def test_independent_replay_rejects_changed_parser_confirmation(
    tmp_path: Path, column: str, value: str
) -> None:
    from tests.test_application_posting_recovery_v1 import _prepare_receipt_subject

    conn, _, _, service, review, _, _, decision_id, *_ = _prepare_receipt_subject(
        tmp_path, "confirmation_drift"
    )
    try:
        service.submit_post(review.review_id, decision_id)
        auth_id = conn.execute(
            "SELECT authorization_id FROM application_conditional_authorization_proofs"
        ).fetchone()[0]
        loaded = load_persisted_receipt_finalization_authorization(conn, auth_id)
        if column == "revoked_at":
            conn.execute(
                "UPDATE parser_proposal_authorizations "
                "SET confirmation_state = 'revoked', revoked_at = ?",
                (value,),
            )
        else:
            conn.execute(f"UPDATE parser_proposal_authorizations SET {column} = ?", (value,))
        conn.commit()
        with pytest.raises(BridgeRecoveryError):
            load_persisted_receipt_finalization_authorization(conn, auth_id)
        with pytest.raises(FinalizationIdempotencyError):
            finalize_prepared_receipt(conn, loaded)
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["conversion", "fact_set", "snapshot", "authorization"])
@pytest.mark.parametrize("column", ["evidence_public_id", "evidence_hash", "material_json"])
def test_independent_receipt_evidence_tamper_refuses_replay(
    tmp_path: Path, kind: str, column: str
) -> None:
    from finance_core.calculation.authoritative_snapshot import canonical_json_text
    from tests.test_application_posting_recovery_v1 import _prepare_receipt_subject

    conn, _, _, service, review, _, _, decision_id, *_ = _prepare_receipt_subject(
        tmp_path, "evidence_tamper"
    )
    try:
        service.submit_post(review.review_id, decision_id)
        auth_id = conn.execute(
            "SELECT authorization_id FROM application_conditional_authorization_proofs"
        ).fetchone()[0]
        loaded = load_persisted_receipt_finalization_authorization(conn, auth_id)
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'application_posting_evidence_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER application_posting_evidence_no_update")
        value = (
            "different-reference"
            if column == "evidence_public_id"
            else "0" * 64
            if column == "evidence_hash"
            else canonical_json_text({"changed": True})
        )
        conn.execute(
            f"UPDATE application_posting_receipt_evidence SET {column} = ? WHERE evidence_type = ?",
            (value, kind),
        )
        conn.execute(trigger)
        conn.commit()
        with pytest.raises(BridgeRecoveryError):
            load_persisted_receipt_finalization_authorization(conn, auth_id)
        with pytest.raises(FinalizationIdempotencyError):
            finalize_prepared_receipt(conn, loaded)
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    finally:
        conn.close()


def test_dropped_independent_tables_cannot_enable_manual_replay(tmp_path: Path) -> None:
    from tests.test_application_posting_recovery_v1 import _prepare_receipt_subject

    conn, _, _, service, review, _, _, decision_id, *_ = _prepare_receipt_subject(
        tmp_path, "missing_schema"
    )
    try:
        service.submit_post(review.review_id, decision_id)
        auth_id = conn.execute(
            "SELECT authorization_id FROM application_conditional_authorization_proofs"
        ).fetchone()[0]
        loaded = load_persisted_receipt_finalization_authorization(conn, auth_id)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE application_conditional_authorization_proofs")
        conn.execute("DROP TABLE application_posting_receipt_evidence")
        conn.execute("UPDATE receipt_finalization_authorizations SET authorization_version = 'v1'")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(BridgeRecoveryError, match="cannot use manual or D2"):
            load_persisted_receipt_finalization_authorization(conn, auth_id)
        with pytest.raises(FinalizationIdempotencyError, match="cannot use manual or D2"):
            finalize_prepared_receipt(conn, loaded)
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    finally:
        conn.close()
