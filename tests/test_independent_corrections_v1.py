"""Independent post-entry corrections against actual signed posting fixtures.

The posting/source helpers below create durable synthetic HMAC evidence and
perform the real text or local-receipt posting path.  Correction approval uses
its own signed display/reply/decision records and schema.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from finance_core.application.correction_schema import CorrectionSchemaError
from finance_core.application.independent_correction_contract import (
    CORRECTION_DECISION_SCHEMA,
    IndependentCorrectionConflict,
    IndependentCorrectionIntegrityError,
    VerifiedCorrectionDecision,
    correction_hash,
)
from finance_core.application.independent_correction_source import (
    IndependentCorrectionFields,
)
from finance_core.application.independent_corrections import IndependentCorrectionService
from finance_core.application.posting import PostingService
from finance_core.reconciliation.adapter import InternalCandidateAdapter
from tests import test_application_posting_recovery_v1 as posting_fixtures
from tests.conftest import connect_temp_db

CORRECTION_KEY = b"independent correction synthetic human approval root"
CORRECTION_TABLES = (
    "synthetic_correction_displays",
    "synthetic_correction_replies",
    "synthetic_correction_decisions",
    "synthetic_correction_display_closures",
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _signature(table: str, value: object) -> str:
    message = (table + "\x00" + _canonical(value)).encode("utf-8")
    return hmac.new(CORRECTION_KEY, message, hashlib.sha256).hexdigest()


def _persist(
    connection: sqlite3.Connection, table: str, record_id: str, material: dict[str, Any]
) -> None:
    connection.execute(
        f"INSERT INTO {table}(id, material, signature) VALUES(?,?,?)",
        (record_id, _canonical(material), _signature(table, material)),
    )


def _load(connection: sqlite3.Connection, table: str, record_id: str) -> dict[str, Any]:
    row = connection.execute(
        f"SELECT material, signature FROM {table} WHERE id=?", (record_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"missing signed synthetic correction evidence: {table}")
    material = json.loads(row[0])
    if not hmac.compare_digest(str(row[1]), _signature(table, material)):
        raise ValueError(f"invalid signed synthetic correction evidence: {table}")
    return material


class _SyntheticCorrectionDecisionAuthority:
    """Check signed full display, direct reply, and decision in the owning DB."""

    def verify_persisted(self, connection, decision_record_id, expected):
        decision = _load(connection, CORRECTION_TABLES[2], decision_record_id)
        display = _load(connection, CORRECTION_TABLES[0], decision["display_id"])
        reply = _load(connection, CORRECTION_TABLES[1], decision["reply_id"])
        closures = []
        for row in connection.execute(
            f"SELECT id FROM {CORRECTION_TABLES[3]} ORDER BY id"
        ).fetchall():
            closure = _load(connection, CORRECTION_TABLES[3], row[0])
            if closure["display_id"] == display["display_id"]:
                closures.append(closure)
        if len(closures) > 1:
            raise ValueError("correction display has contradictory closure evidence")
        unsigned = {key: value for key, value in decision.items() if key != "decision_digest"}
        if decision.get("decision_digest") != correction_hash(unsigned):
            raise ValueError("correction decision digest mismatch")
        projection = expected.review_projection
        expected_identity = {
            "instance_id": expected.binding.instance_id,
            "human_principal_id": expected.binding.human_principal_id,
            "plan_id": expected.plan_id,
            "plan_hash": expected.plan_hash,
        }
        if any(display.get(key) != value for key, value in expected_identity.items()) or any(
            display.get(key) != value
            for key, value in {
                "schema": "finance-application-synthetic-correction-display-v1",
                "projection": projection,
                "delivered": True,
                "direct": True,
            }.items()
        ):
            raise ValueError("correction display is not the exact private delivery")
        if expected.accepted_correction_id is None:
            if display.get("current") is not True or closures:
                raise ValueError("fresh correction approval requires the current unclosed display")
        elif closures and closures[0]["superseded_at"] <= expected.checked_at:
            raise ValueError("historical correction display was closed before acceptance")
        if any(reply.get(key) != value for key, value in expected_identity.items()) or any(
            reply.get(key) != value
            for key, value in {
                "schema": "finance-application-synthetic-correction-reply-v1",
                "display_id": display["display_id"],
                "reply_to_display_id": display["display_id"],
                "action": "confirm",
                "human_authored": True,
            }.items()
        ):
            raise ValueError("correction reply is not a direct human reply to the display")
        source = vars(expected.source)
        decision_expected = {
            "schema": CORRECTION_DECISION_SCHEMA,
            "namespace": expected.binding.decision_namespace,
            "key_id": expected.binding.decision_key_id,
            **expected_identity,
            "decision_id": decision_record_id,
            "plan_id": expected.plan_id,
            "plan_hash": expected.plan_hash,
            "target_id": projection["target_id"],
            "source_evidence_id": source["evidence_id"],
            "source_evidence_digest": source["evidence_digest"],
            "display_id": display["display_id"],
            "display_evidence_digest": correction_hash(display),
            "reply_id": reply["reply_id"],
            "reply_evidence_digest": correction_hash(reply),
            "action": "confirm",
            "consumed": False,
        }
        if any(decision.get(key) != value for key, value in decision_expected.items()):
            raise ValueError("correction decision is not bound to this plan and source")
        if (
            type(decision.get("issued_at")) is not int
            or type(decision.get("expires_at")) is not int
            or not decision["issued_at"] <= expected.checked_at < decision["expires_at"]
        ):
            raise ValueError("correction decision expired")
        if expected.accepted_correction_id is not None:
            accepted = connection.execute(
                "SELECT decision_id,accepted_at FROM application_correction_decisions "
                "WHERE correction_id=? AND plan_id=?",
                (expected.accepted_correction_id, expected.plan_id),
            ).fetchone()
            if (
                accepted is None
                or accepted["decision_id"] != decision_record_id
                or accepted["accepted_at"] != expected.checked_at
            ):
                raise ValueError("correction decision lacks its immutable acceptance record")
        return VerifiedCorrectionDecision(
            schema=decision["schema"],
            namespace=decision["namespace"],
            key_id=decision["key_id"],
            instance_id=decision["instance_id"],
            human_principal_id=decision["human_principal_id"],
            decision_id=decision["decision_id"],
            plan_id=decision["plan_id"],
            plan_hash=decision["plan_hash"],
            source_evidence_id=decision["source_evidence_id"],
            source_evidence_digest=decision["source_evidence_digest"],
            display_id=decision["display_id"],
            display_evidence_digest=decision["display_evidence_digest"],
            reply_id=decision["reply_id"],
            reply_evidence_digest=decision["reply_evidence_digest"],
            action=decision["action"],
            issued_at=decision["issued_at"],
            expires_at=decision["expires_at"],
            consumed=decision["consumed"],
            decision_digest=decision["decision_digest"],
        )


def _persist_correction_approval(
    connection: sqlite3.Connection,
    plan,
    *,
    decision_id: str,
    mutation: str | None = None,
) -> str:
    for table in CORRECTION_TABLES:
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
        )
    projection = dict(plan.projection)
    display_id = f"correction-display-{decision_id}"
    reply_id = f"correction-reply-{decision_id}"
    display = {
        "schema": "finance-application-synthetic-correction-display-v1",
        "display_id": display_id,
        "instance_id": posting_fixtures.BINDING.instance_id,
        "human_principal_id": posting_fixtures.BINDING.human_principal_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "projection": projection,
        "delivered": True,
        "direct": True,
        "current": True,
    }
    reply = {
        "schema": "finance-application-synthetic-correction-reply-v1",
        "reply_id": reply_id,
        "instance_id": posting_fixtures.BINDING.instance_id,
        "human_principal_id": posting_fixtures.BINDING.human_principal_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "display_id": display_id,
        "reply_to_display_id": display_id,
        "action": "confirm",
        "human_authored": True,
    }
    decision = {
        "schema": CORRECTION_DECISION_SCHEMA,
        "namespace": posting_fixtures.BINDING.decision_namespace,
        "key_id": posting_fixtures.BINDING.decision_key_id,
        "instance_id": posting_fixtures.BINDING.instance_id,
        "human_principal_id": posting_fixtures.BINDING.human_principal_id,
        "decision_id": decision_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "target_id": projection["target_id"],
        "source_evidence_id": projection["source"]["evidence_id"],
        "source_evidence_digest": projection["source"]["evidence_digest"],
        "display_id": display_id,
        "display_evidence_digest": correction_hash(display),
        "reply_id": reply_id,
        "reply_evidence_digest": correction_hash(reply),
        "action": "confirm",
        "issued_at": projection["created_at"],
        "expires_at": projection["expires_at"] - 1,
        "consumed": False,
    }
    if mutation == "display_projection":
        display["projection"] = {**projection, "after": {**projection["after"], "amount": "999.00"}}
        decision["display_evidence_digest"] = correction_hash(display)
    elif mutation == "wrong_identity":
        decision["human_principal_id"] = "synthetic-other-human"
    elif mutation == "wrong_target":
        decision["target_id"] = "synthetic-other-target"
    elif mutation == "expired":
        decision["expires_at"] = projection["created_at"]
    elif mutation == "wrong_schema":
        decision["schema"] = "finance-application-posting-human-decision-v1"
    unsigned = dict(decision)
    decision["decision_digest"] = correction_hash(unsigned)
    _persist(connection, CORRECTION_TABLES[0], display_id, display)
    _persist(connection, CORRECTION_TABLES[1], reply_id, reply)
    _persist(connection, CORRECTION_TABLES[2], decision_id, decision)
    connection.commit()
    return decision_id


def _close_correction_display(
    connection: sqlite3.Connection,
    decision_id: str,
    *,
    superseded_at: int,
    replacement_decision_id: str,
) -> None:
    decision = _load(connection, CORRECTION_TABLES[2], decision_id)
    replacement_display_id = "correction-display-" + replacement_decision_id
    closure_id = "correction-closure-" + decision_id
    _persist(
        connection,
        CORRECTION_TABLES[3],
        closure_id,
        {
            "schema": "finance-application-synthetic-correction-display-closure-v1",
            "closure_id": closure_id,
            "display_id": decision["display_id"],
            "superseded_at": superseded_at,
            "replacement_display_id": replacement_display_id,
        },
    )
    connection.commit()


def _correction_service(posting_service: PostingService, *, authority=None, clock=None):
    return IndependentCorrectionService(
        posting_service,
        human_decision_authority=authority or _SyntheticCorrectionDecisionAuthority(),
        clock=clock or (lambda: posting_fixtures.NOW + 5),
    )


def _posted_text(connection: sqlite3.Connection, suffix: str = "correction"):
    service, review, _proposal, _intake, decision_id, _display_id = (
        posting_fixtures._prepare_text_subject(connection, suffix)
    )
    posted = service.submit_post(review.review_id, decision_id)
    correction_authority = _SyntheticCorrectionDecisionAuthority()
    correction_posting = PostingService(
        connection=connection,
        source_verifier=service._source_port,
        human_decision_authority=service._decision_port,
        binding=service._binding,
        clock=lambda: posting_fixtures.NOW + 5,
        correction_decision_authority=correction_authority,
    )
    return correction_posting, posted, correction_authority


def _posted_receipt(tmp_path: Path, suffix: str):
    values = posting_fixtures._prepare_receipt_subject(tmp_path, suffix)
    connection, _workspace, _manifest, service, review, _proposal, _intake, decision_id, _, _, _ = (
        values
    )
    assert review is not None and decision_id is not None
    posted = service.submit_post(review.review_id, decision_id)
    authority = _SyntheticCorrectionDecisionAuthority()
    correction_posting = PostingService(
        connection=connection,
        source_verifier=service._source_port,
        human_decision_authority=service._decision_port,
        binding=service._binding,
        clock=lambda: posting_fixtures.NOW + 5,
        correction_decision_authority=authority,
    )
    return connection, correction_posting, posted, authority


def _six_field_patch():
    return {
        "amount": "18.75",
        "currency": "SGD",
        "transaction_date": "2026-10-09",
        "merchant": "Cafe Two",
        "description": "Team lunch",
        "category": "meals",
    }


def test_text_correction_changes_all_six_fields_and_preserves_original_posting(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    posting, posted, authority = _posted_text(connection)
    service = _correction_service(posting, authority=authority)
    original = service.read_effective(posted.transaction_public_id)
    transaction_before = tuple(
        connection.execute(
            "SELECT * FROM transactions WHERE public_id=?", (posted.transaction_public_id,)
        ).fetchone()
    )
    source_before = tuple(
        connection.execute(
            "SELECT * FROM synthetic_sources WHERE id=?", ("synthetic-source-correction",)
        ).fetchone()
    )

    plan = service.prepare_correction(
        posted.transaction_public_id, _six_field_patch(), "Correct the receipt entry"
    )
    decision_id = _persist_correction_approval(
        connection, plan, decision_id="correction-decision-six"
    )
    result = service.submit(plan.plan_id, decision_id)
    refreshed = service.read_effective(posted.transaction_public_id)

    assert result.applied.version == result.current.version == 1
    assert result.applied.fields == IndependentCorrectionFields(**_six_field_patch())
    assert result.current.fields == result.applied.fields
    assert original.predecessor_hash == original.original_source.original_hash
    assert result.applied.predecessor_id == result.applied.history[-1]["correction_id"]
    assert result.applied.predecessor_hash == correction_hash(result.applied.history[-1])
    assert result.applied.original_source.original_hash == original.original_source.original_hash
    assert refreshed.original_source.source_json == original.original_source.source_json
    assert refreshed.original_source.source_hash == original.original_source.source_hash
    assert refreshed.original_source.original_hash == original.original_source.original_hash
    assert vars(refreshed.original_source.source) == vars(original.original_source.source)
    assert (
        tuple(
            connection.execute(
                "SELECT * FROM transactions WHERE public_id=?", (posted.transaction_public_id,)
            ).fetchone()
        )
        == transaction_before
    )
    assert (
        tuple(
            connection.execute(
                "SELECT * FROM synthetic_sources WHERE id=?", ("synthetic-source-correction",)
            ).fetchone()
        )
        == source_before
    )
    assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert (
        connection.execute(
            "SELECT count(*) FROM financial_audit_events WHERE aggregate_public_id=? "
            "AND event_type='independent_transaction_correction_applied'",
            (posted.transaction_public_id,),
        ).fetchone()[0]
        == 1
    )
    status = posting.get_status(posted.attempt_id)
    assert status.applied is not None and status.current is not None
    assert status.applied.version == 0
    assert status.current.version == 1
    assert status.current.fields == result.current.fields


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"description": "Late lunch"}, {"description": "Late lunch", "category": "food"}),
        ({"category": "transport"}, {"description": "Lunch", "category": "transport"}),
    ],
    ids=["description-only", "category-only"],
)
def test_text_metadata_only_patch_keeps_financial_fields(
    migrated_temp_db_connection: sqlite3.Connection,
    patch: dict[str, str],
    expected: dict[str, str],
) -> None:
    connection = migrated_temp_db_connection
    posting, posted, authority = _posted_text(connection, "metadata-" + next(iter(patch)))
    service = _correction_service(posting, authority=authority)
    before = service.read_effective(posted.transaction_public_id)
    plan = service.prepare_correction(
        posted.transaction_public_id, patch, "Correct bookkeeping metadata"
    )
    decision_id = _persist_correction_approval(
        connection, plan, decision_id="decision-" + next(iter(patch))
    )
    result = service.submit(plan.plan_id, decision_id)
    assert result.current.version == 1
    assert result.current.fields.amount == before.fields.amount
    assert result.current.fields.currency == before.fields.currency
    assert result.current.fields.transaction_date == before.fields.transaction_date
    assert result.current.fields.merchant == before.fields.merchant
    assert result.current.fields.description == expected["description"]
    assert result.current.fields.category == expected["category"]


def test_receipt_correction_changes_all_six_fields_and_creates_a_new_snapshot(
    tmp_path: Path,
) -> None:
    values = posting_fixtures._prepare_receipt_subject(tmp_path, "independent-correction-receipt")
    (
        connection,
        _workspace,
        _manifest,
        service,
        review,
        _proposal,
        _intake,
        decision_id,
        _display,
        _,
        _,
    ) = values
    assert review is not None and decision_id is not None
    try:
        posted = service.submit_post(review.review_id, decision_id)
        authority = _SyntheticCorrectionDecisionAuthority()
        correction_posting = PostingService(
            connection=connection,
            source_verifier=service._source_port,
            human_decision_authority=service._decision_port,
            binding=service._binding,
            clock=lambda: posting_fixtures.NOW + 5,
            correction_decision_authority=authority,
        )
        corrections = _correction_service(correction_posting, authority=authority)
        original = corrections.read_effective(posted.transaction_public_id)
        assert original.snapshot_id and original.snapshot_hash
        plan = corrections.prepare_correction(
            posted.transaction_public_id, _six_field_patch(), "Correct receipt-backed transaction"
        )
        correction_decision = _persist_correction_approval(
            connection, plan, decision_id="receipt-correction-decision"
        )
        result = corrections.submit(plan.plan_id, correction_decision)
        refreshed = corrections.read_effective(posted.transaction_public_id)
        assert result.current.version == 1
        assert result.current.fields == IndependentCorrectionFields(**_six_field_patch())
        assert result.current.snapshot_id != original.snapshot_id
        assert result.current.snapshot_hash != original.snapshot_hash
        assert refreshed.original_source.source_json == original.original_source.source_json
        assert refreshed.original_source.source_hash == original.original_source.source_hash
        assert refreshed.original_source.original_hash == original.original_source.original_hash
        assert refreshed.original_source.snapshot_id == original.original_source.snapshot_id
        assert refreshed.original_source.snapshot_hash == original.original_source.snapshot_hash
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM application_correction_receipt_facts WHERE target_id=?",
                (posted.transaction_public_id,),
            ).fetchone()[0]
            == 1
        )
        status = correction_posting.get_status(posted.attempt_id)
        assert status.applied is not None and status.current is not None
        assert status.applied.version == 0 and status.current.version == 1
    finally:
        connection.close()


def test_two_versions_replay_and_posting_applied_vs_current_are_unambiguous(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    posting, posted, authority = _posted_text(connection, "two-versions")
    corrections = _correction_service(posting, authority=authority)
    first = corrections.prepare_correction(
        posted.transaction_public_id, {"description": "First"}, "first"
    )
    first_decision = _persist_correction_approval(
        connection, first, decision_id="first-correction-decision"
    )
    first_result = corrections.submit(first.plan_id, first_decision)
    second = corrections.prepare_correction(
        posted.transaction_public_id, {"category": "travel"}, "second"
    )
    second_decision = _persist_correction_approval(
        connection, second, decision_id="second-correction-decision"
    )
    second_result = corrections.submit(second.plan_id, second_decision)

    replay = corrections.submit(first.plan_id, first_decision)
    assert replay.recovered is True
    assert replay.applied.version == 1
    assert replay.applied.fields.description == "First"
    assert replay.current.version == 2
    assert replay.current.fields.category == "travel"
    assert second_result.current.version == 2
    assert first_result.applied.version == 1
    status = posting.get_status(posted.attempt_id)
    assert status.applied is not None and status.current is not None
    assert status.applied.version == 0
    assert status.current.version == 2
    assert len(status.current.history) == 2
    assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert (
        connection.execute(
            "SELECT count(*) FROM financial_audit_events WHERE aggregate_public_id=? "
            "AND event_type='independent_transaction_correction_applied'",
            (posted.transaction_public_id,),
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(IndependentCorrectionConflict, match="different decision"):
        corrections.submit(first.plan_id, "some-other-signed-decision")


@pytest.mark.parametrize(
    "mutation", ["wrong_identity", "wrong_target", "expired", "display_projection", "wrong_schema"]
)
def test_correction_rejects_signed_but_mismatched_display_or_decision(
    migrated_temp_db_connection: sqlite3.Connection, mutation: str
) -> None:
    connection = migrated_temp_db_connection
    posting, posted, authority = _posted_text(connection, "bad-proof-" + mutation)
    corrections = _correction_service(posting, authority=authority)
    plan = corrections.prepare_correction(
        posted.transaction_public_id, {"description": "changed"}, "fix"
    )
    decision = _persist_correction_approval(
        connection, plan, decision_id="bad-proof-decision-" + mutation, mutation=mutation
    )
    before = connection.execute("SELECT count(*) FROM application_correction_versions").fetchone()[
        0
    ]
    with pytest.raises(ValueError):
        corrections.submit(plan.plan_id, decision)
    assert (
        connection.execute("SELECT count(*) FROM application_correction_versions").fetchone()[0]
        == before
    )
    assert not connection.in_transaction


@pytest.mark.parametrize(
    "stage",
    [
        "after_decision",
        "after_snapshot",
        "after_version",
        "after_receipt_fact",
        "after_audit",
        "before_commit",
    ],
)
@pytest.mark.parametrize("route", ["text", "receipt"])
def test_precommit_faults_leave_no_partial_correction(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    route: str,
) -> None:
    import finance_core.application.independent_corrections as correction_module

    if route == "text":
        connection = migrated_temp_db_connection
        posting, posted, authority = _posted_text(connection, "rollback-text-" + stage)
    else:
        connection, posting, posted, authority = _posted_receipt(
            tmp_path, "rollback-receipt-" + stage
        )
        request.addfinalizer(connection.close)
    corrections = _correction_service(posting, authority=authority)
    plan = corrections.prepare_correction(
        posted.transaction_public_id, {"description": "Changed"}, "fix"
    )
    decision = _persist_correction_approval(
        connection, plan, decision_id=f"rollback-decision-{route}-{stage}"
    )
    snapshots_before = connection.execute(
        "SELECT count(*) FROM authoritative_calculation_snapshots"
    ).fetchone()[0]
    audit_events_before = connection.execute(
        "SELECT count(*) FROM financial_audit_events"
    ).fetchone()[0]

    def fail_at(observed: str) -> None:
        if observed == stage:
            raise RuntimeError("synthetic correction failure at " + stage)

    monkeypatch.setattr(correction_module, "_failure_injection_hook", fail_at)
    with pytest.raises(RuntimeError, match="synthetic correction failure"):
        corrections.submit(plan.plan_id, decision)
    monkeypatch.setattr(correction_module, "_failure_injection_hook", None)
    assert not connection.in_transaction
    assert (
        connection.execute("SELECT count(*) FROM application_correction_versions").fetchone()[0]
        == 0
    )
    assert (
        connection.execute("SELECT count(*) FROM application_correction_decisions").fetchone()[0]
        == 0
    )
    assert (
        connection.execute("SELECT count(*) FROM application_correction_receipt_facts").fetchone()[
            0
        ]
        == 0
    )
    assert (
        connection.execute("SELECT count(*) FROM authoritative_calculation_snapshots").fetchone()[0]
        == snapshots_before
    )
    assert (
        connection.execute("SELECT count(*) FROM financial_audit_events").fetchone()[0]
        == audit_events_before
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM financial_audit_events "
            "WHERE event_type='independent_transaction_correction_applied'"
        ).fetchone()[0]
        == 0
    )
    assert corrections.recover(plan.plan_id) is None
    assert corrections.read_effective(posted.transaction_public_id).version == 0


def test_postcommit_reply_loss_recovers_after_database_reopen(
    migrated_temp_db_connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import finance_core.application.independent_corrections as correction_module

    connection = migrated_temp_db_connection
    posting, posted, authority = _posted_text(connection, "postcommit-recovery")
    corrections = _correction_service(posting, authority=authority)
    plan = corrections.prepare_correction(
        posted.transaction_public_id, {"category": "travel"}, "fix"
    )
    decision = _persist_correction_approval(connection, plan, decision_id="postcommit-decision")

    def lose_reply(stage: str) -> None:
        if stage == "after_commit":
            raise RuntimeError("synthetic response loss after durable commit")

    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(correction_module, "_failure_injection_hook", lose_reply)
    with pytest.raises(RuntimeError, match="response loss"):
        corrections.submit(plan.plan_id, decision)
    monkeypatch.setattr(correction_module, "_failure_injection_hook", None)
    assert not connection.in_transaction
    connection.close()

    reopened = connect_temp_db(database_path)
    try:
        reopened_posting = PostingService(
            connection=reopened,
            source_verifier=posting._source_port,
            human_decision_authority=posting._decision_port,
            binding=posting._binding,
            clock=lambda: posting_fixtures.NOW + 5,
            correction_decision_authority=authority,
        )
        recovered = _correction_service(reopened_posting, authority=authority).recover(plan.plan_id)
        assert recovered is not None and recovered.recovered is True
        assert recovered.applied.version == recovered.current.version == 1
        assert recovered.current.fields.category == "travel"
        assert (
            reopened.execute(
                "SELECT count(*) FROM financial_audit_events WHERE aggregate_public_id=? "
                "AND event_type='independent_transaction_correction_applied'",
                (posted.transaction_public_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        reopened.close()


def test_two_connections_racing_stale_predecessor_commit_only_one_version(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    posting, posted, authority = _posted_text(connection, "two-connections")
    corrections = _correction_service(posting, authority=authority)
    plan_a = corrections.prepare_correction(
        posted.transaction_public_id, {"description": "First"}, "first"
    )
    decision_a = _persist_correction_approval(connection, plan_a, decision_id="race-decision-a")
    plan_b = corrections.prepare_correction(
        posted.transaction_public_id, {"category": "travel"}, "second"
    )
    decision_b = _persist_correction_approval(connection, plan_b, decision_id="race-decision-b")
    path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    ready = threading.Barrier(2)

    def submit(plan_id: str, decision_id: str):
        separate = connect_temp_db(path)
        try:
            separate_posting = PostingService(
                connection=separate,
                source_verifier=posting._source_port,
                human_decision_authority=posting._decision_port,
                binding=posting._binding,
                clock=lambda: posting_fixtures.NOW + 5,
                correction_decision_authority=authority,
            )
            separate_service = _correction_service(separate_posting, authority=authority)
            ready.wait(timeout=10)
            try:
                result = separate_service.submit(plan_id, decision_id)
                return ("applied", result.current.version)
            except IndependentCorrectionConflict:
                return ("conflict", None)
        finally:
            separate.close()

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda item: submit(*item),
                ((plan_a.plan_id, decision_a), (plan_b.plan_id, decision_b)),
            )
        )
    assert sorted(outcome[0] for outcome in outcomes) == ["applied", "conflict"]
    assert (
        connection.execute(
            "SELECT count(*) FROM application_correction_versions WHERE target_id=?",
            (posted.transaction_public_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM financial_audit_events WHERE aggregate_public_id=? "
            "AND event_type='independent_transaction_correction_applied'",
            (posted.transaction_public_id,),
        ).fetchone()[0]
        == 1
    )


def test_legacy_posting_authority_cannot_authorize_correction(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    posting, posted, _authority = _posted_text(connection, "old-authority")
    legacy = _correction_service(
        posting, authority=posting_fixtures._DurablePostingDecisionAuthority()
    )
    plan = legacy.prepare_correction(
        posted.transaction_public_id, {"description": "Changed"}, "fix"
    )
    with pytest.raises((ValueError, AttributeError)):
        legacy.submit(plan.plan_id, "synthetic-posting-decision-old-authority")
    assert (
        connection.execute("SELECT count(*) FROM application_correction_versions").fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("corruption", ["deleted", "tampered"])
def test_missing_or_tampered_correction_audit_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, corruption: str
) -> None:
    connection = migrated_temp_db_connection
    posting, posted, authority = _posted_text(connection, "tampered-audit")
    corrections = _correction_service(posting, authority=authority)
    plan = corrections.prepare_correction(
        posted.transaction_public_id, {"description": "Changed"}, "fix"
    )
    decision = _persist_correction_approval(connection, plan, decision_id="tampered-audit-decision")
    corrections.submit(plan.plan_id, decision)
    event_id = connection.execute(
        "SELECT event_public_id FROM financial_audit_events WHERE aggregate_public_id=? "
        "AND event_type='independent_transaction_correction_applied'",
        (posted.transaction_public_id,),
    ).fetchone()[0]
    if corruption == "deleted":
        connection.execute("DROP TRIGGER IF EXISTS trg_financial_audit_events_no_delete")
        connection.execute(
            "DELETE FROM financial_audit_events WHERE event_public_id=?", (event_id,)
        )
    else:
        connection.execute("DROP TRIGGER IF EXISTS trg_financial_audit_events_no_update")
        connection.execute(
            "UPDATE financial_audit_events SET event_payload_json='{}' WHERE event_public_id=?",
            (event_id,),
        )
    connection.commit()
    with pytest.raises(ValueError):
        corrections.read_effective(posted.transaction_public_id)


def _run_temporary_schema_corruption(
    connection: sqlite3.Connection,
    *,
    trigger_names: tuple[str, ...],
    mutation_sql: str,
    parameters: tuple[object, ...],
) -> None:
    """Change only a temporary test DB, then restore the exact immutable triggers."""
    assert not connection.in_transaction
    connection.execute("PRAGMA foreign_keys=OFF")
    trigger_sql: list[str] = []
    for trigger_name in trigger_names:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
        ).fetchone()
        assert row is not None and row[0]
        trigger_sql.append(str(row[0]))
        connection.execute(f'DROP TRIGGER "{trigger_name}"')
    connection.commit()
    connection.execute(mutation_sql, parameters)
    connection.commit()
    for statement in trigger_sql:
        connection.execute(statement)
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("corruption", "route"),
    [
        ("missing_version", "text"),
        ("missing_decision", "text"),
        ("tampered_plan", "text"),
        ("tampered_result", "text"),
        ("tampered_source", "text"),
        ("missing_predecessor", "text"),
        ("extra_orphan_audit", "text"),
        ("missing_receipt_fact", "receipt"),
        ("missing_snapshot", "receipt"),
    ],
)
def test_each_correction_data_owner_corruption_fails_every_current_reader(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    corruption: str,
    route: str,
) -> None:
    if route == "text":
        connection = migrated_temp_db_connection
        posting, posted, authority = _posted_text(connection, "integrity-" + corruption)
    else:
        connection, posting, posted, authority = _posted_receipt(
            tmp_path, "integrity-" + corruption
        )
        request.addfinalizer(connection.close)
    corrections = _correction_service(posting, authority=authority)
    first = corrections.prepare_correction(
        posted.transaction_public_id, {"description": "First correction"}, "first"
    )
    first_decision = _persist_correction_approval(
        connection, first, decision_id="integrity-first-" + corruption
    )
    corrections.submit(first.plan_id, first_decision)
    healthy_current = corrections.read_effective(posted.transaction_public_id)
    assert healthy_current.version == 1
    assert healthy_current.fields.description == "First correction"

    if corruption in {"missing_predecessor"}:
        second = corrections.prepare_correction(
            posted.transaction_public_id, {"category": "travel"}, "second"
        )
        second_decision = _persist_correction_approval(
            connection, second, decision_id="integrity-second-" + corruption
        )
        corrections.submit(second.plan_id, second_decision)
        healthy_current = corrections.read_effective(posted.transaction_public_id)
        assert healthy_current.version == 2
        _run_temporary_schema_corruption(
            connection,
            trigger_names=("application_correction_versions_no_delete",),
            mutation_sql=(
                "DELETE FROM application_correction_versions WHERE target_id=? AND version=1"
            ),
            parameters=(posted.transaction_public_id,),
        )
    elif corruption == "missing_version":
        _run_temporary_schema_corruption(
            connection,
            trigger_names=("application_correction_versions_no_delete",),
            mutation_sql="DELETE FROM application_correction_versions WHERE plan_id=?",
            parameters=(first.plan_id,),
        )
    elif corruption == "missing_decision":
        _run_temporary_schema_corruption(
            connection,
            trigger_names=("application_correction_decisions_no_delete",),
            mutation_sql="DELETE FROM application_correction_decisions WHERE plan_id=?",
            parameters=(first.plan_id,),
        )
    elif corruption == "tampered_plan":
        _run_temporary_schema_corruption(
            connection,
            trigger_names=("application_correction_plans_no_update",),
            mutation_sql=(
                "UPDATE application_correction_plans SET material_json='{}' WHERE plan_id=?"
            ),
            parameters=(first.plan_id,),
        )
    elif corruption == "tampered_result":
        _run_temporary_schema_corruption(
            connection,
            trigger_names=("application_correction_versions_no_update",),
            mutation_sql=(
                "UPDATE application_correction_versions SET result_json='{}' WHERE plan_id=?"
            ),
            parameters=(first.plan_id,),
        )
    elif corruption == "tampered_source":
        _run_temporary_schema_corruption(
            connection,
            trigger_names=("application_correction_targets_no_update",),
            mutation_sql=(
                "UPDATE application_correction_targets SET source_json='{}' WHERE target_id=?"
            ),
            parameters=(posted.transaction_public_id,),
        )
    elif corruption == "missing_receipt_fact":
        _run_temporary_schema_corruption(
            connection,
            trigger_names=("application_correction_receipt_facts_no_delete",),
            mutation_sql=(
                "DELETE FROM application_correction_receipt_facts WHERE correction_id="
                "(SELECT correction_id FROM application_correction_plans WHERE plan_id=?)"
            ),
            parameters=(first.plan_id,),
        )
    elif corruption == "missing_snapshot":
        recovered = corrections.recover(first.plan_id)
        assert recovered is not None
        snapshot_id = recovered.applied.snapshot_id
        assert snapshot_id is not None
        _run_temporary_schema_corruption(
            connection,
            trigger_names=("trg_authoritative_snapshots_no_delete",),
            mutation_sql=(
                "DELETE FROM authoritative_calculation_snapshots WHERE snapshot_public_id=?"
            ),
            parameters=(snapshot_id,),
        )
    elif corruption == "extra_orphan_audit":
        from finance_core.financial_audit.chain import (
            AuditEventCommand,
            append_financial_audit_event,
            derive_audit_event_public_id,
        )

        original = corrections.read_effective(posted.transaction_public_id).original_source
        cause = "ic_orphan_audit_" + corruption
        digest = "a" * 64
        event_type = "independent_transaction_correction_applied"
        connection.execute("BEGIN IMMEDIATE")
        append_financial_audit_event(
            connection,
            AuditEventCommand(
                event_public_id=derive_audit_event_public_id(
                    aggregate_type="transaction",
                    aggregate_public_id=posted.transaction_public_id,
                    event_type=event_type,
                    causation_public_id=cause,
                ),
                aggregate_type="transaction",
                aggregate_public_id=posted.transaction_public_id,
                event_type=event_type,
                event_payload={"schema": "orphan-correction-audit-test-v1", "hash": digest},
                new_state={
                    "schema": "orphan-correction-state-test-v1",
                    "target_id": posted.transaction_public_id,
                    "version": 99,
                    "fields": original.fields.as_dict(),
                    "hash": digest,
                },
                actor_type="human",
                actor_public_id=original.actor,
                correlation_public_id="orphan-plan-" + corruption,
                causation_public_id=cause,
                created_at=datetime.fromtimestamp(2_100, timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
                authorization_public_id="orphan-decision-" + corruption,
                source_evidence_references=original.evidence_refs,
            ),
        )
        connection.commit()
    else:
        raise AssertionError("unhandled corruption case")

    adapter = InternalCandidateAdapter(
        connection, effective_reader=corrections.read_effective_in_snapshot
    )
    row_counts_before = tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "transactions",
            "application_correction_decisions",
            "application_correction_versions",
            "application_correction_receipt_facts",
            "authoritative_calculation_snapshots",
            "financial_audit_events",
        )
    )
    with pytest.raises((ValueError, CorrectionSchemaError, IndependentCorrectionIntegrityError)):
        corrections.read_effective(posted.transaction_public_id)
    with pytest.raises((ValueError, CorrectionSchemaError, IndependentCorrectionIntegrityError)):
        corrections.recover(first.plan_id)
    with pytest.raises((ValueError, CorrectionSchemaError, IndependentCorrectionIntegrityError)):
        posting.get_status(posted.attempt_id)
    with pytest.raises((ValueError, CorrectionSchemaError, IndependentCorrectionIntegrityError)):
        adapter.fetch_candidates()
    assert not connection.in_transaction
    row_counts_after = tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "transactions",
            "application_correction_decisions",
            "application_correction_versions",
            "application_correction_receipt_facts",
            "authoritative_calculation_snapshots",
            "financial_audit_events",
        )
    )
    assert row_counts_after == row_counts_before


@pytest.mark.parametrize("report_shape", ["plain_dict", "wrong_typed_report"])
def test_correction_core_rejects_untrusted_authority_return_type(
    migrated_temp_db_connection: sqlite3.Connection, report_shape: str
) -> None:
    connection = migrated_temp_db_connection
    posting, posted, authority = _posted_text(connection, "wrong-port-report-" + report_shape)
    plan_service = _correction_service(posting, authority=authority)
    plan = plan_service.prepare_correction(
        posted.transaction_public_id, {"description": "Changed"}, "fix"
    )
    decision = _persist_correction_approval(
        connection, plan, decision_id="wrong-port-report-decision-" + report_shape
    )

    class UntrustedAuthority:
        def verify_persisted(
            self, conn: sqlite3.Connection, decision_id: str, expected: Any
        ) -> Any:
            verified = authority.verify_persisted(conn, decision_id, expected)
            if report_shape == "plain_dict":
                return vars(verified)
            return replace(verified, plan_hash="0" * 64)

    guarded = _correction_service(posting, authority=UntrustedAuthority())
    with pytest.raises(ValueError):
        guarded.submit(plan.plan_id, decision)
    assert (
        connection.execute("SELECT count(*) FROM application_correction_versions").fetchone()[0]
        == 0
    )
    assert (
        connection.execute("SELECT count(*) FROM application_correction_decisions").fetchone()[0]
        == 0
    )


def test_closed_historical_display_replays_after_expiry_but_cannot_authorize_new_change(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    posting, posted, authority = _posted_text(connection, "closed-display-history")
    now = {"value": posting_fixtures.NOW + 5}
    corrections = _correction_service(posting, authority=authority, clock=lambda: now["value"])
    first = corrections.prepare_correction(
        posted.transaction_public_id, {"description": "First"}, "first"
    )
    first_decision = _persist_correction_approval(
        connection, first, decision_id="closed-display-first"
    )
    now["value"] += 1
    first_result = corrections.submit(first.plan_id, first_decision)

    second_decision_id = "closed-display-second"
    _close_correction_display(
        connection,
        first_decision,
        superseded_at=now["value"] + 2,
        replacement_decision_id=second_decision_id,
    )
    now["value"] += 2
    second = corrections.prepare_correction(
        posted.transaction_public_id, {"category": "travel"}, "second"
    )
    second_decision = _persist_correction_approval(
        connection, second, decision_id=second_decision_id
    )
    now["value"] += 1
    second_result = corrections.submit(second.plan_id, second_decision)
    assert first_result.applied.version == 1
    assert second_result.current.version == 2

    third = corrections.prepare_correction(
        posted.transaction_public_id, {"description": "Third"}, "third"
    )
    third_decision = _persist_correction_approval(
        connection, third, decision_id="closed-display-third"
    )
    _close_correction_display(
        connection,
        third_decision,
        superseded_at=now["value"] + 1,
        replacement_decision_id="never-delivered-replacement",
    )
    now["value"] += 2
    with pytest.raises(ValueError, match="current unclosed display"):
        corrections.submit(third.plan_id, third_decision)
    assert (
        connection.execute(
            "SELECT count(*) FROM application_correction_decisions WHERE plan_id=?",
            (third.plan_id,),
        ).fetchone()[0]
        == 0
    )

    now["value"] = posting_fixtures.NOW + 50_000
    replay = corrections.recover(first.plan_id)
    assert replay is not None and replay.applied.version == 1
    assert replay.current.version == 2
    original_acceptance = connection.execute(
        "SELECT a.review_id,d.decision_id FROM application_posting_attempts a "
        "JOIN application_posting_decisions d ON d.attempt_id=a.attempt_id "
        "WHERE a.attempt_id=?",
        (posted.attempt_id,),
    ).fetchone()
    posting_replay = posting.submit_post(
        original_acceptance["review_id"], original_acceptance["decision_id"]
    )
    assert posting_replay.applied is not None and posting_replay.applied.version == 0
    assert posting_replay.current is not None and posting_replay.current.version == 2
    assert posting_replay.current == replay.current
    status = posting.get_status(posted.attempt_id)
    assert status.applied is not None and status.current is not None
    assert status.applied.version == 0
    assert status.current.version == 2
