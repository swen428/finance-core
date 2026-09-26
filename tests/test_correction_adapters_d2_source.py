"""Synthetic historical D2 source replay through the correction adapter."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.application.corrections import CorrectionService
from finance_core.correction_adapters import local_authority
from finance_core.correction_adapters.d2_source import D2OriginalSourceVerifier, D2SourceError
from finance_core.correction_adapters.local_authority import LocalApprovalAuthority
from finance_core.correction_adapters.policy import open_local_authority_connection, provision
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.posting_authority import (
    begin_posting_review_delivery,
    confirm_and_post,
    prepare_posting_review,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from tests.test_d2_initial_card_delivery_authority_v1 import (
    _file_connection,
    _record_delivery,
    _seed_initial_text,
)
from tests.test_d2_posting_authority_v1 import (
    _file_connection as _receipt_file_connection,
)
from tests.test_d2_posting_authority_v1 import (
    _issue_and_activate,
    _published_receipt_card,
    _published_text_card,
)


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_initial_text_replays_source_delivery_confirmation_and_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "owner_runtime"
    (runtime_root / "database").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root.resolve()))
    conn = _file_connection(tmp_path)
    proposal_id = _seed_initial_text(conn)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2b-source-initial-text",
        proposal_public_id=proposal_id,
        admitted_source_message_id="77",
        context=context,
        clock=lambda: 1000,
    )
    key = b"synthetic-d2b-source-key"
    manifest = begin_posting_review_delivery(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        clock=lambda: 1001,
    )
    _record_delivery(conn, manifest=manifest, context=context, provider_message_id=901, now=1002)
    control = next(item for item in manifest.controls if item.action == "confirm")
    status = confirm_and_post(
        conn,
        key=key,
        reference=control.callback_value.removeprefix("post:"),
        context=context,
        callback_id="d2b-source-confirm",
        callback_message_id=901,
        clock=lambda: 1003,
    )
    assert status.transaction_public_id is not None
    original = D2OriginalSourceVerifier().verify_original(conn, status.transaction_public_id)
    assert original.route == "text"
    assert original.actor == "111"
    assert original.fields.amount == "12.50"
    assert original.fact_set_id is None
    assert original.source_hash and original.original_hash

    database = Path(
        next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    )
    database.chmod(0o600)
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    conn.close()
    provision(database, "111")
    tick = [int(time.time())]
    authority = LocalApprovalAuthority(clock=lambda: tick[0])
    with open_local_authority_connection() as trusted:
        service = CorrectionService(trusted, D2OriginalSourceVerifier(), authority)
        plan = service.preview(status.transaction_public_id, {"amount": "13.50"}, "correct total")
        tick[0] = plan.created_at_epoch + 1
        nonces = iter(("2" * 64, "3" * 64))
        monkeypatch.setattr(local_authority.secrets, "token_hex", lambda count: next(nonces))
        signed = authority.sign_with_terminal(
            plan,
            input_stream=_Terminal(f"CONFIRM {plan.plan_id} {'2' * 64}\n"),
            output_stream=_Terminal(),
        )
        result = service.apply(plan.plan_id, signed)
        assert result.current.fields.amount == "13.50"
        assert result.current.version == 1
        assert service.recover(plan.plan_id).applied.correction_id == plan.correction_id

    # Exercise the installed command entry against that same temporary D2
    # ledger; a piped "confirm" must not silently mint human authority.
    cli = [sys.executable, "-m", "finance_core.correction_adapters.cli"]
    shown = subprocess.run(
        [*cli, "show", status.transaction_public_id],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(shown.stdout)["fields"]["amount"] == "13.50"
    previewed = subprocess.run(
        [
            *cli,
            "preview",
            status.transaction_public_id,
            "--reason",
            "second visible correction",
            "--amount",
            "14.00",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    pending_id = json.loads(previewed.stdout)["plan_id"]
    refused = subprocess.run(
        [*cli, "confirm", pending_id],
        input=f"CONFIRM {pending_id} ignored\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert refused.returncode == 2
    assert "terminal" in refused.stderr
    with open_local_authority_connection() as trusted:
        resumed = CorrectionService(trusted, D2OriginalSourceVerifier(), LocalApprovalAuthority())
        assert resumed.recover(pending_id) is None
        assert resumed.lookup(status.transaction_public_id).version == 1


def test_personal_receipt_replays_conditional_fact_snapshot_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "owner_runtime"
    (runtime_root / "database").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root.resolve()))
    conn = _receipt_file_connection(tmp_path)
    published = _published_receipt_card(conn, tmp_path, monkeypatch)
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2b-source-receipt",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    key = b"synthetic-d2b-receipt-key"
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=key,
        context=context,
        provider_message_id=300,
    )
    status = confirm_and_post(
        conn,
        key=key,
        reference=issued.reference,
        context=context,
        callback_id="d2b-source-receipt-confirm",
        callback_message_id=300,
        clock=lambda: 1004,
    )
    assert status.transaction_public_id is not None
    original = D2OriginalSourceVerifier().verify_original(conn, status.transaction_public_id)
    assert original.route == "receipt"
    assert original.fact_set_id is not None
    assert original.fact_set_version == 1
    assert original.snapshot_hash is not None
    assert original.aggregate_id is not None

    database = Path(
        next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    )
    database.chmod(0o600)
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    conn.close()
    provision(database, "111")
    tick = [int(time.time())]
    authority = LocalApprovalAuthority(clock=lambda: tick[0])
    with open_local_authority_connection() as trusted:
        service = CorrectionService(trusted, D2OriginalSourceVerifier(), authority)
        before = service.lookup(status.transaction_public_id)
        assert before.version == 0
        plan = service.preview(
            status.transaction_public_id,
            {"amount": "14.00"},
            "correct personal receipt total",
        )
        assert plan.fact_id and plan.fact_hash and plan.snapshot_id and plan.snapshot_hash
        assert service.recover(plan.plan_id) is None
        tick[0] = plan.created_at_epoch + 1
        nonces = iter(("4" * 64, "5" * 64))
        monkeypatch.setattr(local_authority.secrets, "token_hex", lambda count: next(nonces))
        terminal_out = _Terminal()
        signed = authority.sign_with_terminal(
            plan,
            input_stream=_Terminal(f"CONFIRM {plan.plan_id} {'4' * 64}\n"),
            output_stream=terminal_out,
        )
        assert "Complete receipt calculation:" in terminal_out.getvalue()
        applied = service.apply(plan.plan_id, signed)
        assert applied.current.version == 1
        assert applied.current.fields.amount == "14.00"
        assert applied.applied.snapshot_id == plan.snapshot_id
        assert (
            trusted.execute(
                "SELECT fact_id FROM correction_receipt_facts WHERE correction_id=?",
                (plan.correction_id,),
            ).fetchone()[0]
            == plan.fact_id
        )
        assert service.lookup(status.transaction_public_id) == applied.current
        assert service.recover(plan.plan_id).applied == applied.applied
        assert Decimal(
            str(
                trusted.execute(
                    "SELECT amount FROM transactions WHERE public_id=?",
                    (status.transaction_public_id,),
                ).fetchone()[0]
            )
        ) == Decimal(original.fields.amount)

    # A forged historical snapshot event must invalidate the original source.
    tampered = sqlite3.connect(database)
    tampered.row_factory = sqlite3.Row
    tampered.execute("PRAGMA foreign_keys = ON")
    tampered.execute("DROP TRIGGER trg_financial_audit_events_no_update")
    tampered.execute(
        "UPDATE financial_audit_events SET event_payload_json='{}' "
        "WHERE aggregate_type='calculation_snapshot' AND aggregate_public_id=?",
        (original.snapshot_id,),
    )
    tampered.commit()
    with pytest.raises(D2SourceError, match="snapshot audit"):
        D2OriginalSourceVerifier().verify_original(tampered, status.transaction_public_id)
    tampered.close()


def test_d1_text_rechecks_historical_reply_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "owner_runtime"
    (runtime_root / "database").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(runtime_root.resolve()))
    conn, published = _published_text_card(tmp_path, monkeypatch)
    conn.execute("UPDATE raw_intake_records SET source_channel='telegram'")
    conn.commit()
    context = HumanActionContext("111", "acct", "111", "binding")
    review = prepare_posting_review(
        conn,
        review_idempotency_key="d2b-d1-text-source",
        card_generation_public_id=published.card_generation_public_id,
        context=context,
        clock=lambda: 1002,
    )
    issued, _ = _issue_and_activate(
        conn,
        review_public_id=review.review_public_id,
        key=b"synthetic-d2b-d1-text-key",
        context=context,
        provider_message_id=302,
    )
    status = confirm_and_post(
        conn,
        key=b"synthetic-d2b-d1-text-key",
        reference=issued.reference,
        context=context,
        callback_id="d2b-d1-text-confirm",
        callback_message_id=302,
        clock=lambda: 1004,
    )
    assert status.transaction_public_id is not None
    verifier = D2OriginalSourceVerifier()
    assert verifier.verify_original(conn, status.transaction_public_id).route == "text"
    original_reply = conn.execute(
        "SELECT raw_utf8 FROM parser_human_draft_reply_evidence"
    ).fetchone()[0]
    conn.execute("DROP TRIGGER trg_parser_human_draft_reply_evidence_no_update")
    conn.execute(
        "UPDATE parser_human_draft_reply_evidence SET raw_utf8=?",
        (b"X" + original_reply[1:],),
    )
    conn.commit()
    with pytest.raises(ValueError, match="reply|revision|lineage|hash"):
        verifier.verify_original(conn, status.transaction_public_id)
    conn.execute("UPDATE parser_human_draft_reply_evidence SET raw_utf8=?", (original_reply,))
    conn.commit()
    assert verifier.verify_original(conn, status.transaction_public_id).route == "text"
    root_before = conn.execute(
        "SELECT raw_text FROM parser_outputs WHERE public_id='prop_d1_source'"
    ).fetchone()[0]
    conn.execute(
        "UPDATE parser_outputs SET raw_text=? WHERE public_id='prop_d1_source'",
        (root_before + " forged",),
    )
    conn.commit()
    with pytest.raises(D2SourceError, match="lineage"):
        verifier.verify_original(conn, status.transaction_public_id)
    conn.execute(
        "UPDATE parser_outputs SET raw_text=? WHERE public_id='prop_d1_source'",
        (root_before,),
    )
    conn.commit()
    assert verifier.verify_original(conn, status.transaction_public_id).route == "text"
    conn.execute("DROP TRIGGER trg_parser_human_draft_publications_no_delete")
    conn.execute("DELETE FROM parser_human_draft_publications")
    conn.commit()
    with pytest.raises(D2SourceError, match="publication"):
        verifier.verify_original(conn, status.transaction_public_id)
    conn.close()
