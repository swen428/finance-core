"""D1 text publication and human-lineage tests on temporary databases only."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from finance_core.parser_proposals import human_drafts
from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    verify_ai_fallback_child,
)
from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card
from finance_core.parser_proposals.human_revision import (
    HumanRevisionLineageError,
    publish_human_revision_in_transaction,
    verify_human_revision_descendant,
)
from finance_core.parser_proposals.service import ParserConfirmationError, confirm_parser_proposal
from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
    migration_ledger_rows,
)
from tests.test_ai_fallback_service_v1 import (
    _prepared_claim,
    _response_body,
    canonical_response_sha256,
    record_ai_fallback_result,
)
from tests.test_parser_human_drafts_v1 import _connection, _start, _validation_payload


def test_migration_048_narrowly_admits_sealed_d1_pointer_edges() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:47])
        before = [tuple(row) for row in migration_ledger_rows(conn)]
        old_sql = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'trigger'
              AND name = 'trg_ai_fallback_raw_intake_no_lineage_escape'
            """
        ).fetchone()[0]
        assert "parser_human_draft_publications" not in old_sql
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:48])
        assert [tuple(row) for row in migration_ledger_rows(conn)[:-1]] == before
        assert migration_ledger_rows(conn)[-1]["migration_id"] == "048"
        new_sql = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'trigger'
              AND name = 'trg_ai_fallback_raw_intake_no_lineage_escape'
            """
        ).fetchone()[0]
        assert "parser_human_draft_publications" in new_sql
        assert "operation.result_completeness = 'complete'" in new_sql
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        assert migration_ledger_rows(conn)[-1]["migration_id"] == "050"
        rows = [tuple(row) for row in migration_ledger_rows(conn)]
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        assert [tuple(row) for row in migration_ledger_rows(conn)] == rows
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def _publish_text_revision(monkeypatch: pytest.MonkeyPatch):
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn, payload=_validation_payload(merchant="Original Cafe"))
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    fields = {**started.field_values, "merchant": "Cafe"}
    text = (
        f"资料卡编号：{started.card_generation_public_id}\r\n"
        f"金额: {fields['amount']}\r\n币种：{fields['currency']}\r\n"
        f"日期: {fields['transaction_date']}\r\n商户: {fields['merchant']}\r\n"
        f"描述: {fields['description']}\r\n分类: {fields['category']}"
    )
    result = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op-text-publication-101",
            "111",
            "acct",
            "111",
            "binding",
            text,
            fields,
        ),
        publish=publish_human_revision_in_transaction,
    )
    assert result.operation_outcome == "accepted", result
    return conn, started, result, text


def test_text_revision_is_append_only_and_preserves_source_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing child creation, pointer advance, or raw-source copying must fail."""
    conn, started, result, text = _publish_text_revision(monkeypatch)
    try:
        assert result.proposal_public_id is not None
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        parent = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = 'prop_d1_source'"
        ).fetchone()
        assert child is not None and parent is not None
        assert child["id"] != parent["id"]
        assert child["parent_parser_output_id"] == parent["id"]
        assert parent["parse_status"] == "superseded"
        assert child["parse_status"] == "parsed_pending_confirmation"
        assert child["raw_text"] == parent["raw_text"] == "lunch"
        assert child["source_public_id"] == parent["source_public_id"] == "intake_d1"
        assert json.loads(child["parsed_payload"])["merchant"] == "Cafe"
        assert (
            conn.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE public_id = 'intake_d1'"
            ).fetchone()[0]
            == child["id"]
        )
        evidence = conn.execute(
            "SELECT raw_utf8, sha256 FROM parser_human_draft_reply_evidence"
        ).fetchone()
        assert bytes(evidence["raw_utf8"]) == text.encode("utf-8")
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0] == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

        lineage = verify_human_revision_descendant(
            conn,
            dict(child),
            content_hash=result.proposal_content_hash,
            proposal_version=result.proposal_version,
        )
        assert lineage is not None
        assert lineage["proposal_origin"] == "human_revision"
        assert lineage["root_proposal_origin"] == "deterministic"
        assert lineage["human_operation_ids"] == ("d1op-text-publication-101",)
        assert lineage["ambiguity_flags"] == ()
    finally:
        conn.close()


def test_text_revision_exact_replay_creates_no_second_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping deterministic publication replay protection must fail."""
    conn, started, first, text = _publish_text_revision(monkeypatch)
    try:
        fields = {**started.field_values, "merchant": "Cafe"}
        replay = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                started.card_generation_public_id,
                101,
                "d1op-text-publication-101",
                "111",
                "acct",
                "111",
                "binding",
                text,
                fields,
            ),
            publish=publish_human_revision_in_transaction,
        )
        assert replay.idempotent_replay is True
        assert replay.proposal_public_id == first.proposal_public_id
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_human_draft_publications").fetchone()[0] == 1
        )
    finally:
        conn.close()


def test_text_revision_missing_publication_edge_fails_lineage_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trusting a parent pointer without the accepted D1 edge must fail."""
    conn, _started, result, _text = _publish_text_revision(monkeypatch)
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        conn.execute("DROP TRIGGER trg_parser_human_draft_publications_no_delete")
        conn.execute("DELETE FROM parser_human_draft_publications")
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="publication"):
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
    finally:
        conn.close()


def test_text_revision_missing_publication_schema_fails_lineage_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A removed migration-047 table must not masquerade as a legacy database."""
    conn, _started, result, _text = _publish_text_revision(monkeypatch)
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        conn.execute("DROP TRIGGER trg_parser_human_draft_publications_no_delete")
        conn.execute("DROP TABLE parser_human_draft_publications")
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="publication schema"):
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
    finally:
        conn.close()


@pytest.mark.parametrize("cycle_kind", ["self", "two-node"])
def test_text_revision_parent_cycle_fails_lineage_verification(
    monkeypatch: pytest.MonkeyPatch,
    cycle_kind: str,
) -> None:
    conn, _started, result, _text = _publish_text_revision(monkeypatch)
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        assert child is not None
        if cycle_kind == "self":
            conn.execute(
                "UPDATE parser_outputs SET parent_parser_output_id = id WHERE id = ?",
                (child["id"],),
            )
        else:
            conn.execute(
                "UPDATE parser_outputs SET parent_parser_output_id = ? WHERE id = ?",
                (child["id"], child["parent_parser_output_id"]),
            )
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="parent chain contains a cycle"):
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
    finally:
        conn.close()


def test_text_revision_modified_reply_bytes_fail_lineage_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, _started, result, _text = _publish_text_revision(monkeypatch)
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        raw = bytes(
            conn.execute("SELECT raw_utf8 FROM parser_human_draft_reply_evidence").fetchone()[0]
        )
        conn.execute("DROP TRIGGER trg_parser_human_draft_reply_evidence_no_update")
        conn.execute(
            "UPDATE parser_human_draft_reply_evidence SET raw_utf8 = ?",
            (bytes([raw[0] ^ 1]) + raw[1:],),
        )
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="reply evidence"):
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
    finally:
        conn.close()


@pytest.mark.parametrize("missing", ["audit", "parent_event", "child_event"])
def test_text_revision_missing_lifecycle_or_audit_evidence_fails_lineage_verification(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    conn, _started, result, _text = _publish_text_revision(monkeypatch)
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        parent_id = child["parent_parser_output_id"]
        if missing == "audit":
            conn.execute("DROP TRIGGER trg_financial_audit_events_no_delete")
            conn.execute(
                "DELETE FROM financial_audit_events "
                "WHERE event_type = 'parser_proposal_human_revision'"
            )
        elif missing == "parent_event":
            conn.execute(
                "DELETE FROM parser_proposal_events "
                "WHERE parser_output_id = ? AND event_type = 'superseded'",
                (parent_id,),
            )
        else:
            conn.execute(
                "DELETE FROM parser_proposal_events "
                "WHERE parser_output_id = ? AND event_type = 'created'",
                (child["id"],),
            )
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="audit|lifecycle"):
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
    finally:
        conn.close()


def _start_existing_text_proposal(conn, parser_output_id: int, *, suffix: str):
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
    from finance_core.parser_proposals.effective_payload import resolve_effective_payload
    from finance_core.parser_proposals.human_drafts import begin_human_draft_in_transaction

    proposal = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (parser_output_id,)
    ).fetchone()
    assert proposal is not None
    content_hash = compute_effective_proposal_content_hash(conn, dict(proposal))
    proposal_version = resolve_effective_payload(conn, dict(proposal))[2]
    reference_material = f"d1-reference-{suffix}".encode()
    reference_public_id = "haref_" + hashlib.sha256(reference_material).hexdigest()[:32]
    conn.execute(
        """
        INSERT INTO openclaw_human_action_references (
            reference_public_id, reference_sha256, issuance_idempotency_key,
            parser_output_id, action, proposal_version, proposal_content_hash,
            authenticated_actor_id, channel, channel_account_id,
            channel_conversation_id, conversation_binding_id, ttl_seconds,
            expires_at, issued_at
        ) VALUES (?, ?, ?, ?, 'edit', ?, ?, '111', 'telegram', 'acct', '111',
                  'binding', 600, 2000, '1970-01-01T00:16:40+00:00')
        """,
        (
            reference_public_id,
            hashlib.sha256(reference_material).hexdigest(),
            "bridge-human-action-issue:" + hashlib.sha256(suffix.encode()).hexdigest()[:32],
            parser_output_id,
            proposal_version,
            content_hash,
        ),
    )
    reference = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE reference_public_id = ?",
        (reference_public_id,),
    ).fetchone()
    assert reference is not None
    redemption_hash = hashlib.sha256(f"callback-{suffix}".encode()).hexdigest()
    conn.execute(
        """
        INSERT INTO openclaw_human_action_redemptions (
            reference_id, callback_id_sha256, callback_message_id, redeemed_at
        ) VALUES (?, ?, 100, '1970-01-01T00:16:40+00:00')
        """,
        (reference["id"], redemption_hash),
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    locked = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE id = ?", (reference["id"],)
    ).fetchone()
    started = begin_human_draft_in_transaction(
        conn,
        locked_edit_reference_row=locked,
        source_edit_reference_id=reference["id"],
        reference_public_id=reference_public_id,
        reference_integrity_material=reference_material,
        callback_message_id=100,
        redemption_public_id=f"d1start_{suffix}",
        redemption_material_hash=redemption_hash,
        now_epoch=1000,
    )
    conn.commit()
    return started


def test_sealed_ai_child_then_exact_human_edge_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A D1 child inherits AI origin only through its verified sealed ancestor."""
    _workspace, conn, attempt, claim = _prepared_claim(
        tmp_path, "paid SGD 12.34 at Cafe on 2026-08-13"
    )
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        date_ref = response["field_evidence_refs"]["amount"][0]
        response["transaction_date"] = "2026-08-13"
        response["field_confidence_bps"]["transaction_date"] = 9000
        response["field_evidence_refs"]["transaction_date"] = [date_ref]
        body = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode()
        arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(body).decode("ascii"),
            "response_byte_count": len(body),
            "response_sha256": canonical_response_sha256(body),
        }
        created = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        ai_child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (created["proposal_public_id"],),
        ).fetchone()
        assert ai_child is not None
        started = _start_existing_text_proposal(conn, ai_child["id"], suffix="ai-human")
        monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
        fields = {**started.field_values, "merchant": "Human Cafe"}
        text = (
            f"资料卡编号：{started.card_generation_public_id}\n"
            f"金额: {fields['amount']}\n币种: {fields['currency']}\n"
            f"日期: {fields['transaction_date']}\n商户: {fields['merchant']}\n"
            f"描述: {fields['description']}\n分类: {fields['category']}"
        )
        result = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                started.card_generation_public_id,
                101,
                "d1op-ai-human-101",
                "111",
                "acct",
                "111",
                "binding",
                text,
                fields,
            ),
            publish=publish_human_revision_in_transaction,
        )
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        assert child is not None
        lineage = verify_human_revision_descendant(
            conn,
            dict(child),
            content_hash=result.proposal_content_hash,
            proposal_version=result.proposal_version,
        )
        assert lineage is not None
        assert lineage["root_proposal_origin"] == "ai_fallback"
        assert lineage["human_operation_ids"] == ("d1op-ai-human-101",)
        conn.execute("DROP TRIGGER trg_ai_fallback_child_no_hash_update")
        conn.execute("DROP TRIGGER trg_ai_fallback_parent_no_hash_update")
        conn.execute(
            "UPDATE parser_outputs SET ai_model = 'forged-model' WHERE id = ?",
            (ai_child["id"],),
        )
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="sealed AI ancestor"):
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
    finally:
        conn.close()


def test_low_confidence_ai_root_remains_unresolved_after_unrelated_human_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace, conn, attempt, claim = _prepared_claim(
        tmp_path, "paid SGD 12.34 at Cafe on 2026-08-13"
    )
    try:
        body, arguments = _response_body(claim)
        response = json.loads(body)
        date_ref = response["field_evidence_refs"]["amount"][0]
        response["transaction_date"] = "2026-08-13"
        response["field_confidence_bps"]["transaction_date"] = 7000
        response["field_evidence_refs"]["transaction_date"] = [date_ref]
        for field in ("amount", "currency", "merchant"):
            response["field_confidence_bps"][field] = 7000
        body = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode()
        arguments = {
            **arguments,
            "response_utf8_b64": base64.b64encode(body).decode("ascii"),
            "response_byte_count": len(body),
            "response_sha256": canonical_response_sha256(body),
        }
        created = record_ai_fallback_result(
            conn,
            attempt_public_id=attempt["attempt_public_id"],
            transport_outcome="response_received",
            arguments=arguments,
            now_ms=100_002,
        )
        ai_child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (created["proposal_public_id"],),
        ).fetchone()
        started = _start_existing_text_proposal(
            conn, ai_child["id"], suffix="ai-low-confidence-human"
        )
        monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
        fields = {**started.field_values, "merchant": "Human Cafe"}
        result = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                started.card_generation_public_id,
                101,
                "d1op-ai-low-confidence-human-101",
                "111",
                "acct",
                "111",
                "binding",
                (
                    f"资料卡编号：{started.card_generation_public_id}\n"
                    f"金额: {fields['amount']}\n币种: {fields['currency']}\n"
                    f"日期: {fields['transaction_date']}\n商户: {fields['merchant']}\n"
                    f"描述: {fields['description']}\n分类: {fields['category']}"
                ),
                fields,
            ),
            publish=publish_human_revision_in_transaction,
        )
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        with pytest.raises(AiFallbackServiceError, match="ambiguity remains unresolved"):
            verify_ai_fallback_child(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
                require_resolved=True,
            )
        before_events = conn.execute("SELECT COUNT(*) FROM parser_proposal_events").fetchone()[0]
        with pytest.raises(ParserConfirmationError):
            confirm_parser_proposal(
                conn,
                int(child["id"]),
                authenticated_actor_id="111",
                expected_content_hash=result.proposal_content_hash,
                expected_version=result.proposal_version,
                clock=lambda: "2026-09-15T00:00:00+00:00",
            )
        assert child["parse_status"] == "parsed_pending_confirmation"
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0] == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_events").fetchone()[0]
            == before_events
        )
    finally:
        conn.close()
