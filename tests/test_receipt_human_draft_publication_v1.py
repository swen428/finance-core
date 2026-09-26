"""D1 receipt publication tests on temporary databases and evidence only."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import finance_core.parser_proposals.receipt_supersession as supersession_module
from finance_core.financial_audit import FinancialAuditRepository, verify_financial_audit_chain
from finance_core.financial_audit import chain as audit_chain
from finance_core.parser_proposals import human_drafts
from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card
from finance_core.parser_proposals.human_revision import (
    HumanRevisionLineageError,
    publish_human_revision_in_transaction,
    verify_human_revision_descendant,
)
from finance_core.parser_proposals.receipt_supersession import (
    ReceiptSupersessionError,
    supersede_receipt_total_proposal,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from tests.test_parser_human_drafts_v1 import _connection
from tests.test_parser_human_revision_v1 import _start_existing_text_proposal
from tests.test_receipt_proposal_revision_v1 import _seed_receipt_proposal

LEGACY_D1_MIGRATION_PATHS = TEMP_DB_MIGRATION_PATHS[:-1]


def _card_text(card_id: str, fields: dict[str, str]) -> str:
    return (
        f"资料卡编号：{card_id}\n"
        f"金额: {fields['amount']}\n币种: {fields['currency']}\n"
        f"日期: {fields['transaction_date']}\n商户: {fields['merchant']}\n"
        f"描述: {fields['description']}\n分类: {fields['category']}"
    )


def _table_counts(conn) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "financial_audit_events",
            "parser_human_draft_operations",
            "parser_human_draft_publications",
            "parser_human_draft_reply_evidence",
            "parser_outputs",
            "parser_proposal_events",
            "parser_proposal_field_evidence",
            "receipt_ocr_proposal_links",
            "receipt_proposal_revisions",
        )
    }


def _embedded_evidence(payload: dict[str, object], field: str) -> dict[str, object]:
    rows = [
        row
        for row in payload["field_evidence"]  # type: ignore[union-attr]
        if row.get("field_name") == field
    ]
    assert len(rows) == 1
    return rows[0]


def _resign_audit_event(conn, event_type: str, **updates: object) -> None:
    row = conn.execute(
        "SELECT event_public_id FROM financial_audit_events WHERE event_type = ?",
        (event_type,),
    ).fetchone()
    assert row is not None
    event = FinancialAuditRepository(conn).fetch(str(row["event_public_id"]))
    assert event is not None
    forged = dataclasses.replace(event, **updates)
    forged = dataclasses.replace(
        forged,
        event_hash=audit_chain._event_hash(
            forged,
            previous_state_hash=forged.previous_state_hash,
            new_state_hash=forged.new_state_hash,
        ),
    )
    forged.verify()
    conn.execute("DROP TRIGGER trg_financial_audit_events_no_update")
    conn.execute(
        "UPDATE financial_audit_events SET calculation_snapshot_public_id = ?, "
        "calculation_snapshot_hash = ?, created_at = ?, event_hash = ? "
        "WHERE event_public_id = ?",
        (
            forged.calculation_snapshot_public_id,
            forged.calculation_snapshot_hash,
            forged.created_at,
            forged.event_hash,
            forged.event_public_id,
        ),
    )
    conn.commit()
    chain = verify_financial_audit_chain(
        conn,
        aggregate_type=forged.aggregate_type,
        aggregate_public_id=forged.aggregate_public_id,
    )
    assert chain.valid


def _publish_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    suffix: str,
    updates: dict[str, str],
):
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    parser_output_id, _source_hash = _seed_receipt_proposal(conn, tmp_path, suffix)
    started = _start_existing_text_proposal(conn, parser_output_id, suffix=suffix)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    fields = {**started.field_values, **updates}
    result = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            f"d1op-receipt-{suffix}",
            "111",
            "acct",
            "111",
            "binding",
            _card_text(started.card_generation_public_id, fields),
            fields,
        ),
        publish=publish_human_revision_in_transaction,
    )
    return conn, parser_output_id, started, result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transaction_date", "2026-09-14"),
        ("merchant", "Human Cafe"),
        ("description", "Team lunch"),
        ("category", "meals"),
    ],
)
def test_receipt_nonmonetary_edits_use_distinct_d1_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    conn, parent_id, _started, result = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix=f"nonmon-{field}",
        updates={field: value},
    )
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        assert child is not None
        assert child["parent_parser_output_id"] == parent_id
        assert child["parse_status"] == "parsed_pending_confirmation"
        payload = json.loads(child["parsed_payload"])
        assert payload[field] == value
        embedded = _embedded_evidence(payload, field)
        assert embedded["evidence_source_type"] == "user_message"
        assert embedded["proposed_value"] == value
        assert payload["field_confidence"].get(field) is None
        assert conn.execute("SELECT COUNT(*) FROM receipt_proposal_revisions").fetchone()[0] == 0
        link = conn.execute(
            "SELECT link_role FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
            (child["id"],),
        ).fetchone()
        assert link is not None and link["link_role"] == "superseding_correction"
        lineage = verify_human_revision_descendant(
            conn,
            dict(child),
            content_hash=result.proposal_content_hash,
            proposal_version=result.proposal_version,
        )
        assert lineage is not None
        assert lineage["human_operation_ids"] == (f"d1op-receipt-nonmon-{field}",)
        assert (
            conn.execute("SELECT COUNT(*) FROM parser_proposal_authorizations").fetchone()[0] == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    finally:
        conn.close()


def test_receipt_amount_edit_uses_monetary_revision_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, parent_id, _started, result = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="money",
        updates={"amount": "22.00"},
    )
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        assert child is not None and child["parent_parser_output_id"] == parent_id
        revision = conn.execute("SELECT * FROM receipt_proposal_revisions").fetchone()
        assert revision is not None
        assert revision["superseded_parser_output_id"] == parent_id
        assert revision["replacement_parser_output_id"] == child["id"]
        assert json.loads(revision["applied_field_updates_json"]) == {"amount": "22.00"}
        payload = json.loads(child["parsed_payload"])
        assert payload["amount"] == "22.00"
        embedded = _embedded_evidence(payload, "amount")
        assert embedded["evidence_source_type"] == "user_message"
        assert embedded["proposed_value"] == "22.00"
        assert payload["field_confidence"]["amount"] is None
        assert (
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
            is not None
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("updates", "tamper_target"),
    [
        ({"merchant": "Human Cafe"}, "link"),
        ({"amount": "22.00"}, "link"),
        ({"amount": "22.00"}, "revision"),
    ],
    ids=("nonmonetary-link", "monetary-link", "monetary-revision"),
)
def test_receipt_d1_evidence_timestamp_tamper_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, str],
    tamper_target: str,
) -> None:
    conn, _parent_id, _started, result = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix=f"timestamp-tamper-{tamper_target}-{next(iter(updates))}",
        updates=updates,
    )
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (result.proposal_public_id,),
        ).fetchone()
        assert child is not None
        if tamper_target == "link":
            conn.execute("DROP TRIGGER trg_receipt_ocr_proposal_links_no_update")
            conn.execute(
                "UPDATE receipt_ocr_proposal_links SET created_at = ? WHERE parser_output_id = ?",
                ("2099-01-01T00:00:00+00:00", child["id"]),
            )
            error_match = "OCR publication link"
        else:
            conn.execute("DROP TRIGGER trg_receipt_proposal_revisions_no_update")
            conn.execute(
                "UPDATE receipt_proposal_revisions SET created_at = ? "
                "WHERE replacement_parser_output_id = ?",
                ("2099-01-01T00:00:00+00:00", child["id"]),
            )
            error_match = "monetary revision evidence"
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match=error_match):
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
    finally:
        conn.close()


def test_public_monetary_revision_preserves_prior_d1_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _parent_id, _started, d1 = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="d1-then-public-money",
        updates={"merchant": "Human Cafe"},
    )
    try:
        d1_child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (d1.proposal_public_id,)
        ).fetchone()
        assert d1_child is not None
        d1_payload = json.loads(d1_child["parsed_payload"])
        inherited = _embedded_evidence(d1_payload, "merchant")

        public = supersede_receipt_total_proposal(
            conn,
            int(d1_child["id"]),
            actor="111",
            expected_content_hash=d1.proposal_content_hash,
            field_updates={"amount": "44.00"},
            correction_public_id="rcor_after_d1",
            correction_channel="cli",
        )
        leaf = conn.execute(
            "SELECT * FROM parser_outputs WHERE id = ?",
            (public["replacement_parser_output_id"],),
        ).fetchone()
        assert leaf is not None
        leaf_payload = json.loads(leaf["parsed_payload"])
        assert _embedded_evidence(leaf_payload, "merchant") == inherited
        lineage = verify_human_revision_descendant(
            conn,
            dict(leaf),
            content_hash=str(public["replacement_content_hash"]),
            proposal_version=0,
        )
        assert lineage is not None
        assert lineage["human_operation_ids"] == ("d1op-receipt-d1-then-public-money",)
    finally:
        conn.close()


def test_two_public_monetary_revisions_preserve_leaf_lineage_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _parent_id, _started, d1 = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="d1-two-public-money",
        updates={"merchant": "Human Cafe"},
    )
    try:
        d1_child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (d1.proposal_public_id,)
        ).fetchone()
        assert d1_child is not None
        first = supersede_receipt_total_proposal(
            conn,
            int(d1_child["id"]),
            actor="111",
            expected_content_hash=d1.proposal_content_hash,
            field_updates={"amount": "44.00"},
            correction_public_id="rcor_after_d1_first",
            correction_channel="cli",
        )
        second = supersede_receipt_total_proposal(
            conn,
            int(first["replacement_parser_output_id"]),
            actor="111",
            expected_content_hash=str(first["replacement_content_hash"]),
            field_updates={"amount": "55.00"},
            correction_public_id="rcor_after_d1_second",
            correction_channel="cli",
        )
        leaf = conn.execute(
            "SELECT * FROM parser_outputs WHERE id = ?",
            (second["replacement_parser_output_id"],),
        ).fetchone()
        assert leaf is not None
        lineage = verify_human_revision_descendant(
            conn,
            dict(leaf),
            content_hash=str(second["replacement_content_hash"]),
            proposal_version=0,
        )
        assert lineage is not None
        assert lineage["human_operation_ids"] == ("d1op-receipt-d1-two-public-money",)

        first_replay = supersede_receipt_total_proposal(
            conn,
            int(d1_child["id"]),
            actor="111",
            expected_content_hash=d1.proposal_content_hash,
            field_updates={"amount": "44.00"},
            correction_public_id="rcor_after_d1_first",
            correction_channel="cli",
        )
        second_replay = supersede_receipt_total_proposal(
            conn,
            int(first["replacement_parser_output_id"]),
            actor="111",
            expected_content_hash=str(first["replacement_content_hash"]),
            field_updates={"amount": "55.00"},
            correction_public_id="rcor_after_d1_second",
            correction_channel="cli",
        )
        assert first_replay["idempotent"] is True
        assert second_replay["idempotent"] is True
    finally:
        conn.close()


def test_public_monetary_replay_rejects_missing_inherited_d1_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _parent_id, _started, d1 = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="d1-public-replay-tamper",
        updates={"merchant": "Human Cafe"},
    )
    try:
        d1_child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (d1.proposal_public_id,)
        ).fetchone()
        assert d1_child is not None
        public = supersede_receipt_total_proposal(
            conn,
            int(d1_child["id"]),
            actor="111",
            expected_content_hash=d1.proposal_content_hash,
            field_updates={"amount": "44.00"},
            correction_public_id="rcor_after_d1_replay",
            correction_channel="cli",
        )
        conn.execute(
            "UPDATE parser_outputs SET parser_name = 'finance_ai_proposal', "
            "parser_version = 'forged-v1' WHERE id IN (?, ?)",
            (d1_child["id"], public["replacement_parser_output_id"]),
        )
        conn.execute("DROP TRIGGER trg_parser_human_draft_publications_no_delete")
        conn.execute(
            "DELETE FROM parser_human_draft_publications WHERE parser_output_id = ?",
            (d1_child["id"],),
        )
        conn.commit()

        with pytest.raises(ReceiptSupersessionError, match="D1.*lineage"):
            supersede_receipt_total_proposal(
                conn,
                int(d1_child["id"]),
                actor="111",
                expected_content_hash=d1.proposal_content_hash,
                field_updates={"amount": "44.00"},
                correction_public_id="rcor_after_d1_replay",
                correction_channel="cli",
            )
        assert public["idempotent"] is False
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("audit_scope", "updates"),
    [
        (
            "d1",
            {
                "calculation_snapshot_public_id": "forged_snapshot",
                "calculation_snapshot_hash": "0" * 64,
            },
        ),
        (
            "public",
            {
                "calculation_snapshot_public_id": "forged_snapshot",
                "calculation_snapshot_hash": "0" * 64,
            },
        ),
        ("public", {"created_at": "2099-01-01T00:00:00.000000Z"}),
    ],
)
def test_receipt_chain_valid_audit_tamper_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_scope: str,
    updates: dict[str, object],
) -> None:
    conn, _parent_id, _started, d1 = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix=f"audit-tamper-{audit_scope}-{len(updates)}",
        updates={"merchant": "Human Cafe"},
    )
    try:
        leaf = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (d1.proposal_public_id,)
        ).fetchone()
        assert leaf is not None
        expected_hash = d1.proposal_content_hash
        event_type = "parser_proposal_human_revision"
        if audit_scope == "public":
            public = supersede_receipt_total_proposal(
                conn,
                int(leaf["id"]),
                actor="111",
                expected_content_hash=d1.proposal_content_hash,
                field_updates={"amount": "44.00"},
                correction_public_id=f"rcor_audit_tamper_{len(updates)}",
                correction_channel="cli",
            )
            leaf = conn.execute(
                "SELECT * FROM parser_outputs WHERE id = ?",
                (public["replacement_parser_output_id"],),
            ).fetchone()
            assert leaf is not None
            expected_hash = str(public["replacement_content_hash"])
            event_type = "receipt_proposal_superseded"
        _resign_audit_event(conn, event_type, **updates)
        with pytest.raises(HumanRevisionLineageError, match="audit evidence"):
            verify_human_revision_descendant(
                conn,
                dict(leaf),
                content_hash=expected_hash,
                proposal_version=0,
            )
    finally:
        conn.close()


def test_public_monetary_revision_event_timestamp_tamper_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _parent_id, _started, d1 = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="public-event-time-tamper",
        updates={"merchant": "Human Cafe"},
    )
    try:
        d1_child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (d1.proposal_public_id,)
        ).fetchone()
        assert d1_child is not None
        public = supersede_receipt_total_proposal(
            conn,
            int(d1_child["id"]),
            actor="111",
            expected_content_hash=d1.proposal_content_hash,
            field_updates={"amount": "44.00"},
            correction_public_id="rcor_event_time_tamper",
            correction_channel="cli",
        )
        leaf = conn.execute(
            "SELECT * FROM parser_outputs WHERE id = ?",
            (public["replacement_parser_output_id"],),
        ).fetchone()
        assert leaf is not None
        conn.execute(
            "UPDATE parser_proposal_events SET created_at = ? "
            "WHERE parser_output_id = ? AND event_type = 'created'",
            ("2099-01-01T00:00:00+00:00", leaf["id"]),
        )
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="lifecycle evidence"):
            verify_human_revision_descendant(
                conn,
                dict(leaf),
                content_hash=str(public["replacement_content_hash"]),
                proposal_version=0,
            )
    finally:
        conn.close()


def test_receipt_modified_ocr_link_hash_fails_lineage_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _parent_id, _started, result = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="tampered-link",
        updates={"merchant": "Human Cafe"},
    )
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (result.proposal_public_id,),
        ).fetchone()
        assert child is not None
        conn.execute("DROP TRIGGER trg_receipt_ocr_proposal_links_no_update")
        conn.execute(
            "UPDATE receipt_ocr_proposal_links SET proposal_input_hash = ? "
            "WHERE parser_output_id = ?",
            ("0" * 64, child["id"]),
        )
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


def test_receipt_raw_intake_status_drift_fails_lineage_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _parent_id, _started, result = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="raw-intake-status-drift",
        updates={"merchant": "Human Cafe"},
    )
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        assert child is not None
        conn.execute(
            "UPDATE raw_intake_records SET status = 'confirmed' WHERE parser_output_id = ?",
            (child["id"],),
        )
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="raw-intake pointer"):
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
    finally:
        conn.close()


def test_receipt_missing_monetary_revision_fails_lineage_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _parent_id, _started, result = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="missing-revision",
        updates={"amount": "22.00"},
    )
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (result.proposal_public_id,),
        ).fetchone()
        assert child is not None
        conn.execute("DROP TRIGGER trg_receipt_proposal_revisions_no_delete")
        conn.execute(
            "DELETE FROM receipt_proposal_revisions WHERE replacement_parser_output_id = ?",
            (child["id"],),
        )
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="monetary revision evidence"):
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=result.proposal_content_hash,
                proposal_version=result.proposal_version,
            )
    finally:
        conn.close()


def test_receipt_missing_intermediate_d1_edge_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _parent_id, _started, first = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="missing-intermediate",
        updates={"merchant": "First Human Cafe"},
    )
    try:
        monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1002)
        fields = {**first.field_values, "description": "second edit"}
        second = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                first.card_generation_public_id,
                102,
                "d1op-receipt-missing-intermediate-2",
                "111",
                "acct",
                "111",
                "binding",
                _card_text(first.card_generation_public_id, fields),
                fields,
            ),
            publish=publish_human_revision_in_transaction,
        )
        leaf = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (second.proposal_public_id,),
        ).fetchone()
        first_child_id = conn.execute(
            "SELECT id FROM parser_outputs WHERE public_id = ?",
            (first.proposal_public_id,),
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_parser_human_draft_publications_no_delete")
        conn.execute(
            "DELETE FROM parser_human_draft_publications WHERE parser_output_id = ?",
            (first_child_id,),
        )
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="publication edge is missing"):
            verify_human_revision_descendant(
                conn,
                dict(leaf),
                content_hash=second.proposal_content_hash,
                proposal_version=second.proposal_version,
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field_name", "evidence_source_type"),
    [("merchant", "user_message"), ("amount", "ocr")],
)
def test_receipt_missing_inherited_relational_evidence_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    evidence_source_type: str,
) -> None:
    conn, _parent_id, _started, first = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix=f"missing-inherited-{evidence_source_type}",
        updates={"merchant": "First Human Cafe"},
    )
    try:
        monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1002)
        fields = {**first.field_values, "description": "second edit"}
        second = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                first.card_generation_public_id,
                102,
                f"d1op-receipt-missing-inherited-{evidence_source_type}-2",
                "111",
                "acct",
                "111",
                "binding",
                _card_text(first.card_generation_public_id, fields),
                fields,
            ),
            publish=publish_human_revision_in_transaction,
        )
        leaf = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (second.proposal_public_id,),
        ).fetchone()
        assert leaf is not None
        conn.execute(
            "DELETE FROM parser_proposal_field_evidence "
            "WHERE parser_output_id = ? AND field_name = ? AND evidence_source_type = ?",
            (leaf["id"], field_name, evidence_source_type),
        )
        conn.commit()
        with pytest.raises(HumanRevisionLineageError, match="relational evidence"):
            verify_human_revision_descendant(
                conn,
                dict(leaf),
                content_hash=second.proposal_content_hash,
                proposal_version=second.proposal_version,
            )
    finally:
        conn.close()


@pytest.mark.parametrize("field", ["description", "category"])
def test_receipt_optional_metadata_set_then_clear_is_canonical_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    conn, _parent_id, _started, first = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix=f"clear-{field}",
        updates={field: "temporary value"},
    )
    try:
        monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1002)
        fields = {**first.field_values, field: ""}
        second = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                first.card_generation_public_id,
                102,
                f"d1op-receipt-clear-{field}-2",
                "111",
                "acct",
                "111",
                "binding",
                _card_text(first.card_generation_public_id, fields),
                fields,
            ),
            publish=publish_human_revision_in_transaction,
        )
        payload = json.loads(
            conn.execute(
                "SELECT parsed_payload FROM parser_outputs WHERE public_id = ?",
                (second.proposal_public_id,),
            ).fetchone()[0]
        )
        assert payload[field] is None
        assert second.field_values[field] == ""
        operation = conn.execute(
            "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
            (f"d1op-receipt-clear-{field}-2",),
        ).fetchone()
        assert json.loads(operation["explicit_clears_json"]) == {field: ["temporary value", None]}
        leaf = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (second.proposal_public_id,)
        ).fetchone()
        lineage = verify_human_revision_descendant(
            conn,
            dict(leaf),
            content_hash=second.proposal_content_hash,
            proposal_version=second.proposal_version,
        )
        assert lineage is not None
        assert lineage["human_operation_ids"] == (
            f"d1op-receipt-clear-{field}",
            f"d1op-receipt-clear-{field}-2",
        )
    finally:
        conn.close()


def test_receipt_combined_money_and_nonmoney_changes_share_one_atomic_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = {
        "amount": "22.00",
        "currency": "USD",
        "transaction_date": "2026-09-14",
        "merchant": "Combined Cafe",
    }
    conn, parent_id, started, result = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="combined",
        updates=updates,
    )
    try:
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?", (result.proposal_public_id,)
        ).fetchone()
        payload = json.loads(child["parsed_payload"])
        assert {
            field: payload[field]
            for field in ("amount", "currency", "transaction_date", "merchant")
        } == {
            "amount": "22.00",
            "currency": "USD",
            "transaction_date": "2026-09-14",
            "merchant": "Combined Cafe",
        }
        revision = conn.execute("SELECT * FROM receipt_proposal_revisions").fetchone()
        assert revision["superseded_parser_output_id"] == parent_id
        assert sorted(json.loads(revision["applied_field_updates_json"])) == [
            "amount",
            "currency",
            "merchant",
            "transaction_date",
        ]
        replay = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                started.card_generation_public_id,
                101,
                "d1op-receipt-combined",
                "111",
                "acct",
                "111",
                "binding",
                _card_text(
                    started.card_generation_public_id,
                    {**started.field_values, **updates},
                ),
                {**started.field_values, **updates},
            ),
            publish=publish_human_revision_in_transaction,
        )
        assert replay.idempotent_replay is True
        assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 2
    finally:
        conn.close()


def test_receipt_monetary_revision_can_clear_optional_metadata_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _parent_id, _started, first = _publish_receipt(
        tmp_path,
        monkeypatch,
        suffix="money-clear",
        updates={"description": "temporary value"},
    )
    try:
        monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1002)
        fields = {**first.field_values, "amount": "23.00", "description": ""}
        second = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                first.card_generation_public_id,
                102,
                "d1op-receipt-money-clear-2",
                "111",
                "acct",
                "111",
                "binding",
                _card_text(first.card_generation_public_id, fields),
                fields,
            ),
            publish=publish_human_revision_in_transaction,
        )
        child = conn.execute(
            "SELECT * FROM parser_outputs WHERE public_id = ?",
            (second.proposal_public_id,),
        ).fetchone()
        assert child is not None
        assert json.loads(child["parsed_payload"])["description"] is None
        revision = conn.execute(
            "SELECT applied_field_updates_json FROM receipt_proposal_revisions"
        ).fetchone()
        assert json.loads(revision[0]) == {"amount": "23.00", "description": None}
        assert (
            verify_human_revision_descendant(
                conn,
                dict(child),
                content_hash=second.proposal_content_hash,
                proposal_version=second.proposal_version,
            )
            is not None
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"amount": "22.00"},
        {"merchant": "Rollback Cafe"},
    ],
    ids=("monetary", "nonmonetary"),
)
def test_receipt_d1_publication_failure_rolls_back_every_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, str],
) -> None:
    conn = _connection()
    apply_migration_paths(conn, LEGACY_D1_MIGRATION_PATHS)
    parent_id, _source_hash = _seed_receipt_proposal(
        conn, tmp_path, f"rollback-{next(iter(updates))}"
    )
    started = _start_existing_text_proposal(
        conn, parent_id, suffix=f"rollback-{next(iter(updates))}"
    )
    before_counts = _table_counts(conn)
    parent_before = dict(
        conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (parent_id,)
        ).fetchone()
    )
    intake_before = dict(
        conn.execute(
            "SELECT parser_output_id, status FROM raw_intake_records WHERE parser_output_id = ?",
            (parent_id,),
        ).fetchone()
    )

    class InjectedFailure(RuntimeError):
        pass

    def fail_before_link(stage: str) -> None:
        if stage == "before_link_insert":
            raise InjectedFailure(stage)

    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    monkeypatch.setattr(supersession_module, "_failure_injection_hook", fail_before_link)
    fields = {**started.field_values, **updates}
    with pytest.raises(InjectedFailure):
        apply_human_draft_card(
            conn,
            HumanDraftCommand(
                started.card_generation_public_id,
                101,
                f"d1op-receipt-rollback-{next(iter(updates))}",
                "111",
                "acct",
                "111",
                "binding",
                _card_text(started.card_generation_public_id, fields),
                fields,
            ),
            publish=publish_human_revision_in_transaction,
        )

    assert conn.in_transaction is False
    assert _table_counts(conn) == before_counts
    assert (
        dict(
            conn.execute(
                "SELECT parse_status FROM parser_outputs WHERE id = ?", (parent_id,)
            ).fetchone()
        )
        == parent_before
    )
    assert (
        dict(
            conn.execute(
                "SELECT parser_output_id, status FROM raw_intake_records "
                "WHERE parser_output_id = ?",
                (parent_id,),
            ).fetchone()
        )
        == intake_before
    )
    conn.close()
