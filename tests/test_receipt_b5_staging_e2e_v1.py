"""IAF.8 canonical B5 staging end-to-end tests (happy + stale paths).

Proves the whole B5 chain works through **public boundaries only** on a
disposable migrated staging database:

raw intake (``create_raw_intake_record``) -> Telegram attachment acquisition
(``acquire_and_persist_telegram_attachment`` with an injected fake transport)
-> receipt OCR evidence (``extract_and_persist_receipt_ocr_evidence`` with a
deterministic fake ``ReceiptOcrEngine``) -> total-expense proposal ingestion
-> human confirmation -> B4.1 conversion -> IAF fact-set persistence ->
readiness -> projection -> IAF.7 bridge (prepare / authorize / finalize) ->
``finalize_receipt_split``.

Section 9.4's canonical stale path is exercised end to end: a v1
authorization is refused after a v2 supersession with zero final facts, and
the re-prepared v2 authorization finalizes cleanly.

Section 9.5 failure/resource bullets covered here at E2E level: bad
attachment content signature, engine-failed OCR never reaching financial
facts, stale proposal confirmation hash, conversion replay conflict,
missing/incomplete fact set (not calculator-ready), stale active fact-set
authorization (Section 9.4), finalizer failure-injection rollback, staging
guard rejection, temp-file cleanup, and no real network/credentials (fake
transport call accounting).  Remaining bullets are covered by existing
focused suites and are intentionally reused, not duplicated: projection
unsupported mapping (``test_receipt_calculator_input_projection_v1.py``),
OCR resource limits and malformed persisted blocks
(``test_receipt_ocr_proposal_ingestion_v1.py``), and attachment transport
resource limits (``test_telegram_attachment_acquisition.py``).

No direct SQL seeds any happy-path lifecycle row; direct SQL appears only in
migration fixture infrastructure, SELECT assertions, and the participants
reference-data seed (``seed_people``) shared with the existing conversion
suites.  Participant reference data is a prerequisite fixture, not a lifecycle
boundary: the project has no public "create participant" boundary and the
conversion boundary requires participants to already exist.  The test's
"public boundaries only" claim is scoped to the receipt lifecycle chain
(intake → finalization), not to participant reference-data setup.  Only
disposable staging databases are used; ``database/finance.db`` and seed data
are untouched.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import finance_core.receipt_finalization.finalizer as finalizer
from finance_core.calculation.authoritative_snapshot import AuthoritativeSnapshotRepository
from finance_core.calculators.receipt_calculator_input_projection import (
    ReceiptCalculatorInputProjection,
    ReceiptNotCalculatorReadyError,
    project_receipt_calculator_input,
)
from finance_core.calculators.receipt_calculator_readiness import (
    ReceiptCalculatorReadinessReport,
    report_receipt_calculator_readiness,
)
from finance_core.financial_audit import verify_financial_audit_chain
from finance_core.intake.raw_text_repository import create_raw_intake_record
from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrEngineResult,
    ReceiptOcrExtractionStatus,
    ReceiptOcrLimits,
    extract_and_persist_receipt_ocr_evidence,
)
from finance_core.intake.receipt_ocr_proposal import (
    ReceiptTotalProposalIngestionResult,
    ingest_receipt_ocr_evidence_as_total_expense_proposal,
)
from finance_core.intake.telegram_attachment_acquisition import (
    ContentSignatureMismatchError,
    StagingDatabaseRejectedError,
    TelegramAttachmentAcquisitionResult,
    TelegramFileMetadata,
    acquire_and_persist_telegram_attachment,
)
from finance_core.parser_proposals import (
    confirm_proposal,
    convert_confirmed_receipt_proposal_to_facts,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.receipt_facts_conversion import (
    ConversionIdempotencyConflictError,
    ConversionPersistenceError,
    IncompleteReceiptInputsError,
    ReceiptFactsConversionCommand,
    ReceiptFactsConversionResult,
    StaleConfirmationHashError,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
)
from finance_core.receipt_finalization import (
    FinalizationAuthorizationError,
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.models import FinalizationBlockReason
from finance_core.staging_guard import StagingDatabaseError
from tests.test_receipt_facts_conversion_v1 import entries, seed_people
from tests.test_receipt_item_allocation_facts_service_v1 import (
    ConvertedReceipt,
    iaf_command,
)
from tests.test_receipt_item_allocation_facts_supersession_v1 import (
    correction_command,
)
from tests.test_receipt_ocr_proposal_ingestion_v1 import FakeEngine, _sgd_blocks
from tests.test_telegram_attachment_acquisition import FakeResponse, FakeTransport

# Canonical fact tables that only a successful finalization may populate.
CANONICAL_FACT_TABLES = (
    "transactions",
    "calculation_runs",
    "calculation_participant_shares",
    "settlement_obligations",
    "receipt_groups",
    "receipt_group_receipts",
    "receipt_finalization_audit",
)


def _receipt_jpeg(suffix: str) -> bytes:
    """Synthetic valid-magic JPEG bytes, suffix-unique for distinct hashes."""
    return b"\xff\xd8\xff\xe0" + f"b5-e2e-receipt-{suffix}".encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_storage(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _make_transport(content: bytes, *, mime: str = "image/jpeg") -> FakeTransport:
    return FakeTransport(
        content,
        metadata=TelegramFileMetadata(file_path="photos/receipt.jpg", file_size=len(content)),
        response=FakeResponse(
            content,
            headers={"Content-Length": str(len(content)), "Content-Type": mime},
        ),
    )


def _counts(conn: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables
    }


def _reconciliation_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'reconciliation%' ORDER BY name"
        ).fetchall()
    ]


@dataclasses.dataclass(frozen=True)
class B5Pipeline:
    """Artifacts of one canonical B5 run up to the persisted IAF fact set."""

    suffix: str
    content: bytes
    content_hash: str
    storage_root: Path
    transport: FakeTransport
    raw_intake_id: int
    raw_intake_public_id: str
    acquisition: TelegramAttachmentAcquisitionResult
    attachment_id: int
    extraction_public_id: str
    ingestion: ReceiptTotalProposalIngestionResult
    proposal_content_hash: str
    conversion: ReceiptFactsConversionResult
    ctx: ConvertedReceipt
    iaf_result: Any


def _run_b5_pipeline(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str, *, seed: bool = True
) -> B5Pipeline:
    """B1 raw intake -> attachment -> OCR -> proposal -> confirm -> B4.1 ->
    IAF fact set, all through public boundaries (COLD STORAGE, SGD 12.34)."""
    content = _receipt_jpeg(suffix)
    content_hash = _sha256(content)

    with conn:
        intake = create_raw_intake_record(
            conn,
            f"telegram photo message: cold storage receipt {suffix}",
            source_type="telegram_image",
            source_channel="telegram",
            source_metadata={"chat_id": -100123, "message_id": 4242, "sender": "owner"},
            received_at="2026-07-20T12:00:00+00:00",
            public_id=f"raw_b5_{suffix}",
        )
    raw_intake_id = int(intake["id"])

    storage_root = _private_storage(tmp_path, f"attachments_{suffix}")
    transport = _make_transport(content)
    acquisition = acquire_and_persist_telegram_attachment(
        conn,
        transport=transport,
        storage_root=storage_root,
        public_id=f"tgae_b5_{suffix}",
        raw_intake_id=raw_intake_id,
        telegram_file_id=f"file_b5_{suffix}",
        telegram_file_unique_id=f"unique_b5_{suffix}",
        original_filename=f"receipt_{suffix}.jpg",
        declared_mime_type="image/jpeg",
    )
    attachment_id = int(acquisition.persistence_result["attachment_id"])

    engine = FakeEngine(
        result=ReceiptOcrEngineResult(
            status=ReceiptOcrExtractionStatus.SUCCEEDED,
            blocks=_sgd_blocks(),
            outcome_code="ok",
        )
    )
    extraction_public_id = f"rocr_b5_{suffix}"
    extract_and_persist_receipt_ocr_evidence(
        conn,
        public_id=extraction_public_id,
        attachment_id=attachment_id,
        engine=engine,
        limits=ReceiptOcrLimits(),
    )

    ingestion = ingest_receipt_ocr_evidence_as_total_expense_proposal(
        conn,
        extraction_public_id=extraction_public_id,
        proposal_public_id=f"prop_b5_{suffix}",
        link_public_id=f"ropl_b5_{suffix}",
    )

    confirm_proposal(
        conn,
        ingestion.parser_output_id,
        actor="owner",
        confirmation_public_id=f"pca_b5_{suffix}",
    )

    if seed:
        seed_people(conn)
    proposal_content_hash = compute_effective_proposal_content_hash(
        conn, {"id": ingestion.parser_output_id}
    )
    conversion = convert_confirmed_receipt_proposal_to_facts(
        conn, _conversion_command(suffix, ingestion, proposal_content_hash)
    )

    ctx = ConvertedReceipt(
        receipt_public_id=conversion.receipt_public_id,
        receipt_id=conversion.receipt_id,
        conversion_command_public_id=f"rpfc_b5_{suffix}",
        conversion_result_hash=conversion.conversion_result_hash,
        proposal_content_hash=conversion.proposal_content_hash,
        attachment_content_hash=content_hash,
    )
    iaf_result = persist_receipt_item_allocation_facts(conn, iaf_command(suffix, ctx))
    conn.commit()

    return B5Pipeline(
        suffix=suffix,
        content=content,
        content_hash=content_hash,
        storage_root=storage_root,
        transport=transport,
        raw_intake_id=raw_intake_id,
        raw_intake_public_id=str(intake["public_id"]),
        acquisition=acquisition,
        attachment_id=attachment_id,
        extraction_public_id=extraction_public_id,
        ingestion=ingestion,
        proposal_content_hash=proposal_content_hash,
        conversion=conversion,
        ctx=ctx,
        iaf_result=iaf_result,
    )


def _conversion_command(
    suffix: str,
    ingestion: ReceiptTotalProposalIngestionResult,
    expected_content_hash: str,
    **overrides: Any,
) -> ReceiptFactsConversionCommand:
    fields: dict[str, Any] = {
        "command_public_id": f"rpfc_b5_{suffix}",
        "proposal_public_id": ingestion.proposal_public_id,
        "expected_content_hash": expected_content_hash,
        "payer_participant_public_id": "person_owner",
        "participants": entries(("person_owner", 1), ("person_alice", 1), ("person_bob", 0)),
        "authenticated_actor_id": "owner",
        "channel": "cli",
        "actor_type": "human",
        "reason": None,
    }
    fields.update(overrides)
    return ReceiptFactsConversionCommand(**fields)


# ---------------------------------------------------------------------------
# Section 9.2 / 9.3 — canonical happy path
# ---------------------------------------------------------------------------


def test_canonical_happy_path_end_to_end(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    p = _run_b5_pipeline(conn, tmp_path, "happy")

    # -- attachment file hash/size/path agree between disk and DB evidence.
    durable = Path(p.acquisition.attachment_path)
    assert durable.is_file()
    assert durable.read_bytes() == p.content
    assert p.storage_root in durable.parents
    assert p.acquisition.content_hash == p.content_hash
    assert p.acquisition.observed_file_size == len(p.content)
    att = dict(
        conn.execute("SELECT * FROM attachments WHERE id = ?", (p.attachment_id,)).fetchone()
    )
    assert att["file_hash"] == p.content_hash
    assert att["file_path"] == str(durable)
    assert att["mime_type"] == "image/jpeg"
    tg_source = dict(
        conn.execute(
            "SELECT * FROM telegram_attachment_source WHERE attachment_id = ?",
            (p.attachment_id,),
        ).fetchone()
    )
    assert tg_source["content_hash"] == p.content_hash
    assert tg_source["observed_file_size"] == len(p.content)
    assert tg_source["raw_intake_record_id"] == p.raw_intake_id

    # -- OCR extraction agrees with attachment identity, engine identity,
    #    limits handling, and block persistence.
    ext = dict(
        conn.execute(
            "SELECT * FROM receipt_ocr_extractions WHERE public_id = ?",
            (p.extraction_public_id,),
        ).fetchone()
    )
    assert ext["attachment_id"] == p.attachment_id
    assert ext["source_attachment_hash"] == p.content_hash
    assert ext["source_attachment_size"] == len(p.content)
    assert ext["engine_name"] == "fake_ocr"
    assert ext["engine_version"] == "1.0"
    assert ext["extraction_status"] == "succeeded"
    assert ext["block_count"] == len(_sgd_blocks())
    assert len(ext["normalized_result_hash"]) == 64
    block_rows = conn.execute(
        "SELECT COUNT(*) FROM receipt_ocr_blocks WHERE extraction_id = ?", (ext["id"],)
    ).fetchone()[0]
    assert block_rows == len(_sgd_blocks())

    # -- proposal was pending confirmation with complete OCR lineage.
    assert p.ingestion.parse_status == "parsed_pending_confirmation"
    assert p.ingestion.ambiguity_flags == ()
    payload = json.loads(
        conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE id = ?",
            (p.ingestion.parser_output_id,),
        ).fetchone()["parsed_payload"]
    )
    assert payload["amount"] == "12.34"
    assert payload["currency"] == "SGD"
    assert payload["merchant"] == "COLD STORAGE"
    assert payload["transaction_date"] == "2026-07-20"
    assert payload["ocr_evidence"]["extraction_public_id"] == p.extraction_public_id
    assert payload["ocr_evidence"]["normalized_result_hash"] == ext["normalized_result_hash"]

    # -- human confirmation is bound to the exact proposal content hash.
    proposal_auth = dict(
        conn.execute(
            "SELECT * FROM parser_proposal_authorizations WHERE parser_output_id = ?",
            (p.ingestion.parser_output_id,),
        ).fetchone()
    )
    assert proposal_auth["proposal_content_hash"] == p.proposal_content_hash
    assert proposal_auth["actor_type"] == "human"

    # -- B4.1 conversion created one total-level confirmed receipt with
    #    raw/attachment/OCR/proposal/confirmation lineage preserved.
    receipt = dict(
        conn.execute(
            "SELECT * FROM receipts WHERE public_id = ?", (p.conversion.receipt_public_id,)
        ).fetchone()
    )
    assert receipt["status"] == "confirmed"
    assert receipt["currency"] == "SGD"
    assert receipt["net_paid_amount_canonical_text"] == "12.34"
    assert receipt["parser_output_id"] == p.ingestion.parser_output_id
    assert receipt["attachment_id"] == p.attachment_id
    assert receipt["raw_input"] is not None
    membership = conn.execute(
        "SELECT COUNT(*) FROM receipt_participants WHERE receipt_id = ?",
        (p.conversion.receipt_id,),
    ).fetchone()[0]
    assert membership == 3

    # -- fact set was created by a human-authored command and reconciles.
    fact_set = dict(
        conn.execute(
            "SELECT * FROM receipt_item_allocation_fact_sets WHERE fact_set_public_id = ?",
            (p.iaf_result.fact_set_public_id,),
        ).fetchone()
    )
    assert fact_set["version"] == 1
    assert fact_set["superseded_by_fact_set_public_id"] is None

    # -- readiness is positive with the correct binding fields.
    readiness = report_receipt_calculator_readiness(conn, p.conversion.receipt_public_id)
    assert isinstance(readiness, ReceiptCalculatorReadinessReport)
    assert readiness.is_calculator_ready is True
    assert readiness.not_ready_reasons == ()
    assert readiness.active_fact_set_public_id == p.iaf_result.fact_set_public_id
    assert readiness.active_fact_set_version == 1
    assert readiness.fact_set_result_hash == p.iaf_result.fact_set_result_hash
    assert readiness.currency == "SGD"
    assert readiness.net_paid_amount_canonical_text == "12.34"

    # -- projection binding agrees with readiness and the DB active set.
    projection = project_receipt_calculator_input(conn, p.conversion.receipt_public_id)
    assert isinstance(projection, ReceiptCalculatorInputProjection)
    assert projection.fact_set_public_id == readiness.active_fact_set_public_id
    assert projection.fact_set_version == readiness.active_fact_set_version
    assert projection.fact_set_result_hash == readiness.fact_set_result_hash
    assert projection.fact_set_input_hash == fact_set["fact_set_input_hash"]
    assert projection.net_paid_amount_canonical_text == "12.34"

    # -- deterministic calculator output reconciles shares against the total.
    prepared = prepare_receipt_calculation(conn, p.conversion.receipt_public_id)
    calc = prepared.calculation_result
    assert calc["currency"] == "SGD"
    assert calc["payer"] == "person_owner"
    share_total = sum(Decimal(str(share)) for share in dict(calc["participant_shares"]).values())
    assert share_total == Decimal("12.34")
    assert prepared.active_fact_set_binding.fact_set_public_id == (p.iaf_result.fact_set_public_id)
    assert prepared.idempotent_replay is False
    prepared_again = prepare_receipt_calculation(conn, p.conversion.receipt_public_id)
    assert prepared_again.idempotent_replay is True
    assert prepared_again == prepared

    # -- authoritative snapshot hashes are recomputable and verified.
    snapshot = AuthoritativeSnapshotRepository(conn).fetch(prepared.calculation_snapshot_id)
    assert snapshot is not None
    snapshot.verify()
    assert snapshot.combined_snapshot_hash == prepared.calculation_snapshot_hash
    assert snapshot.authorization_reference == prepared.authorization_id

    # -- finalization confirmation/authorization are human, content-bound,
    #    and active-fact-set-bound.
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    fin_conf = dict(
        conn.execute(
            "SELECT * FROM receipt_finalization_confirmations WHERE confirmation_id = ?",
            (prepared.confirmation_id,),
        ).fetchone()
    )
    fin_auth = dict(
        conn.execute(
            "SELECT * FROM receipt_finalization_authorizations WHERE authorization_id = ?",
            (prepared.authorization_id,),
        ).fetchone()
    )
    assert fin_conf["actor_type"] == "human"
    assert fin_auth["actor_type"] == "human"
    assert fin_auth["actor_id"] == "owner"
    assert fin_conf["content_hash"] == authorization.content_hash
    assert fin_auth["content_hash"] == authorization.content_hash
    evidence = fin_auth["source_evidence_refs_json"]
    assert p.iaf_result.fact_set_public_id in evidence
    assert p.iaf_result.fact_set_result_hash in evidence
    assert p.content_hash in evidence

    # -- finalization produces exactly one canonical transaction, one
    #    calculation run, two shares, and one settlement obligation.
    output = finalize_prepared_receipt(conn, authorization)
    assert output.status == "finalized"
    assert _counts(conn, CANONICAL_FACT_TABLES) == {
        "transactions": 1,
        "calculation_runs": 1,
        "calculation_participant_shares": 2,
        "settlement_obligations": 1,
        "receipt_groups": 1,
        "receipt_group_receipts": 1,
        "receipt_finalization_audit": 1,
    }
    txn = dict(conn.execute("SELECT * FROM transactions").fetchone())
    assert txn["public_id"] == output.transaction_public_id
    assert txn["status"] == "active"
    assert txn["currency"] == "SGD"
    assert Decimal(str(txn["amount"])) == Decimal("12.34")
    # FIX-07: canonical transaction metadata matches the confirmed receipt.
    assert txn["merchant"] == "COLD STORAGE"
    assert txn["source_channel"] == "telegram"
    assert str(txn["transaction_date"])[:10] == "2026-07-20"
    obligation = dict(
        conn.execute(
            "SELECT so.amount, so.currency, debtor.public_id AS debtor_public_id, "
            "creditor.public_id AS creditor_public_id "
            "FROM settlement_obligations so "
            "JOIN participants debtor ON debtor.id = so.debtor_id "
            "JOIN participants creditor ON creditor.id = so.creditor_id"
        ).fetchone()
    )
    assert obligation["debtor_public_id"] == "person_alice"
    assert obligation["creditor_public_id"] == "person_owner"
    assert obligation["currency"] == "SGD"
    assert Decimal(str(obligation["amount"])) == Decimal("6.17")

    # -- receipt/group/finalization status contracts hold.
    receipt_after = dict(
        conn.execute(
            "SELECT status FROM receipts WHERE public_id = ?",
            (p.conversion.receipt_public_id,),
        ).fetchone()
    )
    assert receipt_after["status"] == "confirmed"
    group = dict(conn.execute("SELECT * FROM receipt_groups").fetchone())
    assert group["public_id"] == f"rgrp_{p.conversion.receipt_public_id}"
    assert group["status"] == "settled"
    fin_audit = dict(conn.execute("SELECT * FROM receipt_finalization_audit").fetchone())
    assert fin_audit["status"] == "finalized"
    # FIX-07: audit monetary totals match the calculator and settlement.
    assert Decimal(str(fin_audit["total_paid"])) == Decimal("12.34")
    assert Decimal(str(fin_audit["total_to_collect"])) == Decimal("6.17")
    assert fin_audit["payer_participant_public_id"] == "person_owner"

    # FIX-07: all four binding evidence rows carry the exact same four-tuple.
    evidence_rows = conn.execute(
        "SELECT bound_record_type, receipt_public_id, fact_set_public_id, "
        "fact_set_version, fact_set_input_hash, fact_set_result_hash "
        "FROM receipt_fact_set_binding_evidence ORDER BY bound_record_type"
    ).fetchall()
    assert len(evidence_rows) == 4
    four_tuples = {
        (
            str(row["fact_set_public_id"]),
            int(row["fact_set_version"]),
            str(row["fact_set_input_hash"]),
            str(row["fact_set_result_hash"]),
        )
        for row in evidence_rows
    }
    assert len(four_tuples) == 1, f"Expected one unique four-tuple, got {four_tuples}"
    bound_types = sorted(str(row["bound_record_type"]) for row in evidence_rows)
    assert bound_types == [
        "calculation_run",
        "calculation_snapshot",
        "finalization_audit",
        "finalization_authorization",
    ]
    # Evidence receipt matches the finalized receipt.
    assert all(
        str(row["receipt_public_id"]) == p.conversion.receipt_public_id for row in evidence_rows
    )
    # Source evidence lineage preserved in authorization and audit.
    assert f"iaf.attachment_content_hash={p.content_hash}" in fin_auth["source_evidence_refs_json"]
    assert f"iaf.attachment_content_hash={p.content_hash}" in fin_audit["evidence_refs_json"]

    # -- the authorization was consumed only by the successful transaction.
    consumed = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (prepared.authorization_id,),
    ).fetchone()["authorization_state"]
    assert consumed == "consumed"

    # -- every touched audit chain verifies, and the finalization audit's
    #    source references trace back to the attachment content hash.
    for aggregate_type, aggregate_public_id in (
        ("parser_proposal", p.ingestion.proposal_public_id),
        ("receipt", p.conversion.receipt_public_id),
        ("receipt_group", f"rgrp_{p.conversion.receipt_public_id}"),
        ("calculation_snapshot", prepared.calculation_snapshot_id),
    ):
        chain = verify_financial_audit_chain(
            conn, aggregate_type=aggregate_type, aggregate_public_id=aggregate_public_id
        )
        assert chain.valid is True, (aggregate_type, chain.reason)
        assert chain.event_count >= 1
    assert f"iaf.attachment_content_hash={p.content_hash}" in fin_audit["evidence_refs_json"]
    assert att["file_hash"] == p.content_hash  # evidence -> attachments row

    # -- repeated exact replay of every stage creates no duplicate facts.
    counts_before_replay = _counts(
        conn,
        CANONICAL_FACT_TABLES
        + (
            "raw_intake_records",
            "attachments",
            "receipt_ocr_extractions",
            "parser_outputs",
            "receipts",
            "receipt_item_allocation_fact_sets",
            "authoritative_calculation_snapshots",
            "calc_audit_runs",
            "receipt_finalization_confirmations",
            "receipt_finalization_authorizations",
            "receipt_fact_set_binding_evidence",
        ),
    )
    replay_acq = acquire_and_persist_telegram_attachment(
        conn,
        transport=_make_transport(p.content),
        storage_root=p.storage_root,
        public_id=f"tgae_b5_{p.suffix}",
        raw_intake_id=p.raw_intake_id,
        telegram_file_id=f"file_b5_{p.suffix}",
        telegram_file_unique_id=f"unique_b5_{p.suffix}",
        original_filename=f"receipt_{p.suffix}.jpg",
        declared_mime_type="image/jpeg",
    )
    assert replay_acq.durable_file_reused is True
    assert replay_acq.network_download_occurred is False
    assert replay_acq.persistence_result["idempotent"] is True
    replay_ext = extract_and_persist_receipt_ocr_evidence(
        conn,
        public_id=p.extraction_public_id,
        attachment_id=p.attachment_id,
        engine=FakeEngine(
            result=ReceiptOcrEngineResult(
                status=ReceiptOcrExtractionStatus.SUCCEEDED,
                blocks=_sgd_blocks(),
                outcome_code="ok",
            )
        ),
        limits=ReceiptOcrLimits(),
    )
    assert replay_ext.persistence_idempotent is True
    replay_conf = confirm_proposal(
        conn,
        p.ingestion.parser_output_id,
        actor="owner",
        confirmation_public_id=f"pca_b5_{p.suffix}",
    )
    assert replay_conf["idempotent"] is True
    # After the receipt's audit chain advanced (IAF persist + finalization),
    # the exact conversion replay fails closed instead of minting facts.
    with pytest.raises(ConversionPersistenceError):
        convert_confirmed_receipt_proposal_to_facts(
            conn, _conversion_command(p.suffix, p.ingestion, p.proposal_content_hash)
        )
    replay_iaf = persist_receipt_item_allocation_facts(conn, iaf_command(p.suffix, p.ctx))
    conn.commit()
    assert replay_iaf.fact_set_public_id == p.iaf_result.fact_set_public_id
    replay_prepared = prepare_receipt_calculation(conn, p.conversion.receipt_public_id)
    replay_authz = authorize_receipt_finalization(conn, replay_prepared, actor_id="owner")
    replay_output = finalize_prepared_receipt(conn, replay_authz)
    assert replay_output.status == "already_finalized"
    assert replay_output.transaction_public_id == output.transaction_public_id
    # FIX-07: replay returns the same IDs as the first finalization.
    assert replay_output.finalization_public_id == output.finalization_public_id
    assert sorted(replay_output.settlement_public_ids) == sorted(output.settlement_public_ids)
    assert (
        _counts(
            conn,
            CANONICAL_FACT_TABLES
            + (
                "raw_intake_records",
                "attachments",
                "receipt_ocr_extractions",
                "parser_outputs",
                "receipts",
                "receipt_item_allocation_fact_sets",
                "authoritative_calculation_snapshots",
                "calc_audit_runs",
                "receipt_finalization_confirmations",
                "receipt_finalization_authorizations",
                "receipt_fact_set_binding_evidence",
            ),
        )
        == counts_before_replay
    )

    # -- no reconciliation or reporting side effects, no leaked temp files,
    #    no real network beyond the injected fake transport.
    recon_tables = _reconciliation_tables(conn)
    assert recon_tables  # schema exists in a fully migrated staging DB
    assert all(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in recon_tables
    )
    leftovers = [
        path for path in p.storage_root.rglob("*") if path.name.startswith(".telegram-acquisition-")
    ]
    assert leftovers == []
    assert p.transport.metadata_calls == 1
    assert p.transport.download_calls == 1
    assert not conn.in_transaction


def test_exact_ingestion_replay_before_confirmation_is_idempotent(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The exact ingest command replays idempotently while the proposal is
    still pending (after confirmation the same replay fails closed)."""
    conn = migrated_temp_db_connection
    content = _receipt_jpeg("ingidem")
    with conn:
        intake = create_raw_intake_record(
            conn,
            "telegram photo message: replay",
            source_type="telegram_image",
            source_channel="telegram",
            public_id="raw_b5_ingidem",
        )
    acquisition = acquire_and_persist_telegram_attachment(
        conn,
        transport=_make_transport(content),
        storage_root=_private_storage(tmp_path, "attachments_ingidem"),
        public_id="tgae_b5_ingidem",
        raw_intake_id=int(intake["id"]),
        telegram_file_id="file_b5_ingidem",
        telegram_file_unique_id="unique_b5_ingidem",
        original_filename="receipt_ingidem.jpg",
        declared_mime_type="image/jpeg",
    )
    extract_and_persist_receipt_ocr_evidence(
        conn,
        public_id="rocr_b5_ingidem",
        attachment_id=int(acquisition.persistence_result["attachment_id"]),
        engine=FakeEngine(
            result=ReceiptOcrEngineResult(
                status=ReceiptOcrExtractionStatus.SUCCEEDED,
                blocks=_sgd_blocks(),
                outcome_code="ok",
            )
        ),
        limits=ReceiptOcrLimits(),
    )
    first = ingest_receipt_ocr_evidence_as_total_expense_proposal(
        conn,
        extraction_public_id="rocr_b5_ingidem",
        proposal_public_id="prop_b5_ingidem",
        link_public_id="ropl_b5_ingidem",
    )
    replay = ingest_receipt_ocr_evidence_as_total_expense_proposal(
        conn,
        extraction_public_id="rocr_b5_ingidem",
        proposal_public_id="prop_b5_ingidem",
        link_public_id="ropl_b5_ingidem",
    )
    assert first.idempotent is False
    assert replay.idempotent is True
    assert replay.parser_output_id == first.parser_output_id
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM parser_outputs WHERE public_id = 'prop_b5_ingidem'"
        ).fetchone()[0]
        == 1
    )


# ---------------------------------------------------------------------------
# Section 9.4 — canonical stale path
# ---------------------------------------------------------------------------


def test_canonical_stale_path_v1_refused_then_v2_finalizes(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    p = _run_b5_pipeline(conn, tmp_path, "stale")

    # v1 readiness/projection/calculation/snapshot/authorization.
    prepared_v1 = prepare_receipt_calculation(conn, p.conversion.receipt_public_id)
    authorization_v1 = authorize_receipt_finalization(conn, prepared_v1, actor_id="owner")

    # Supersede to fact set v2 through the public IAF boundary.
    supersede_receipt_item_allocation_facts(conn, correction_command(p.suffix, p.ctx, p.iaf_result))
    conn.commit()

    # The v1 authorization must fail closed with zero final facts.
    counts_before = _counts(conn, CANONICAL_FACT_TABLES)
    with pytest.raises(FinalizationAuthorizationError) as excinfo:
        finalize_prepared_receipt(conn, authorization_v1)
    assert excinfo.value.reason == FinalizationBlockReason.STALE_ACTIVE_FACT_SET
    assert _counts(conn, CANONICAL_FACT_TABLES) == counts_before
    assert counts_before["transactions"] == 0
    v1_state = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (prepared_v1.authorization_id,),
    ).fetchone()["authorization_state"]
    assert v1_state == "authorized"  # never consumed by the refused attempt
    assert not conn.in_transaction

    # v2 readiness/projection/calculation/snapshot/authorization succeed.
    readiness_v2 = report_receipt_calculator_readiness(conn, p.conversion.receipt_public_id)
    assert readiness_v2.is_calculator_ready is True
    assert readiness_v2.active_fact_set_version == 2
    prepared_v2 = prepare_receipt_calculation(conn, p.conversion.receipt_public_id)
    assert prepared_v2.active_fact_set_binding != prepared_v1.active_fact_set_binding
    assert prepared_v2.active_fact_set_binding.fact_set_version == 2
    authorization_v2 = authorize_receipt_finalization(conn, prepared_v2, actor_id="owner")
    assert authorization_v2.content_hash != authorization_v1.content_hash

    output = finalize_prepared_receipt(conn, authorization_v2)
    assert output.status == "finalized"
    after = _counts(conn, CANONICAL_FACT_TABLES)
    assert after["transactions"] == 1
    assert after["calculation_runs"] == 1
    assert after["receipt_groups"] == 1
    # The finalized monetary facts come from the v2 fact set (single
    # corrected line 12.34; manual shares owner 4.34 / alice 8.00).
    txn = dict(conn.execute("SELECT amount, currency, status FROM transactions").fetchone())
    assert txn["status"] == "active"
    assert txn["currency"] == "SGD"
    assert Decimal(str(txn["amount"])) == Decimal("12.34")
    obligation = dict(
        conn.execute(
            "SELECT so.amount, so.currency, debtor.public_id AS debtor_public_id, "
            "creditor.public_id AS creditor_public_id "
            "FROM settlement_obligations so "
            "JOIN participants debtor ON debtor.id = so.debtor_id "
            "JOIN participants creditor ON creditor.id = so.creditor_id"
        ).fetchone()
    )
    assert obligation["debtor_public_id"] == "person_alice"
    assert obligation["creditor_public_id"] == "person_owner"
    assert obligation["currency"] == "SGD"
    assert Decimal(str(obligation["amount"])) == Decimal("8.00")
    v2_state = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (prepared_v2.authorization_id,),
    ).fetchone()["authorization_state"]
    assert v2_state == "consumed"


# ---------------------------------------------------------------------------
# Section 9.5 — failure and resource boundaries at E2E level
# ---------------------------------------------------------------------------


def test_bad_attachment_content_signature_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A declared JPEG whose bytes carry a PDF signature is rejected with
    zero persisted attachment evidence."""
    conn = migrated_temp_db_connection
    with conn:
        intake = create_raw_intake_record(
            conn,
            "telegram photo message: forged bytes",
            source_type="telegram_image",
            source_channel="telegram",
            public_id="raw_b5_badsig",
        )
    forged = b"%PDF-1.7\nnot-actually-a-jpeg"
    with pytest.raises(ContentSignatureMismatchError):
        acquire_and_persist_telegram_attachment(
            conn,
            transport=_make_transport(forged, mime="application/octet-stream"),
            storage_root=_private_storage(tmp_path, "attachments_badsig"),
            public_id="tgae_b5_badsig",
            raw_intake_id=int(intake["id"]),
            telegram_file_id="file_b5_badsig",
            telegram_file_unique_id="unique_b5_badsig",
            original_filename="receipt_badsig.jpg",
            declared_mime_type="image/jpeg",
        )
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM telegram_attachment_source").fetchone()[0] == 0


def test_engine_failed_ocr_cannot_reach_financial_facts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An engine-failed OCR run yields a flagged incomplete proposal whose
    conversion fails closed: unusable OCR can never become financial facts."""
    conn = migrated_temp_db_connection
    content = _receipt_jpeg("ocrfail")
    with conn:
        intake = create_raw_intake_record(
            conn,
            "telegram photo message: unreadable receipt",
            source_type="telegram_image",
            source_channel="telegram",
            public_id="raw_b5_ocrfail",
        )
    acquisition = acquire_and_persist_telegram_attachment(
        conn,
        transport=_make_transport(content),
        storage_root=_private_storage(tmp_path, "attachments_ocrfail"),
        public_id="tgae_b5_ocrfail",
        raw_intake_id=int(intake["id"]),
        telegram_file_id="file_b5_ocrfail",
        telegram_file_unique_id="unique_b5_ocrfail",
        original_filename="receipt_ocrfail.jpg",
        declared_mime_type="image/jpeg",
    )
    extract_and_persist_receipt_ocr_evidence(
        conn,
        public_id="rocr_b5_ocrfail",
        attachment_id=int(acquisition.persistence_result["attachment_id"]),
        engine=FakeEngine(
            result=ReceiptOcrEngineResult(
                status=ReceiptOcrExtractionStatus.ENGINE_FAILED,
                blocks=(),
                outcome_code="engine_failed",
            )
        ),
        limits=ReceiptOcrLimits(),
    )
    ingestion = ingest_receipt_ocr_evidence_as_total_expense_proposal(
        conn,
        extraction_public_id="rocr_b5_ocrfail",
        proposal_public_id="prop_b5_ocrfail",
        link_public_id="ropl_b5_ocrfail",
    )
    assert "ocr_engine_failed" in ingestion.ambiguity_flags
    payload = json.loads(
        conn.execute(
            "SELECT parsed_payload FROM parser_outputs WHERE id = ?",
            (ingestion.parser_output_id,),
        ).fetchone()["parsed_payload"]
    )
    assert payload["amount"] is None

    confirm_proposal(
        conn,
        ingestion.parser_output_id,
        actor="owner",
        confirmation_public_id="pca_b5_ocrfail",
    )
    seed_people(conn)
    expected = compute_effective_proposal_content_hash(conn, {"id": ingestion.parser_output_id})
    with pytest.raises(IncompleteReceiptInputsError):
        convert_confirmed_receipt_proposal_to_facts(
            conn, _conversion_command("ocrfail", ingestion, expected)
        )
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_stale_proposal_confirmation_hash_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    p = _run_b5_pipeline(conn, tmp_path, "stalehash")
    stale_hash = _sha256(b"some-other-proposal-content")
    with pytest.raises(StaleConfirmationHashError):
        convert_confirmed_receipt_proposal_to_facts(
            conn,
            _conversion_command(
                "stalehash2",
                p.ingestion,
                stale_hash,
                command_public_id="rpfc_b5_stalehash2",
            ),
        )
    # Only the original conversion's receipt exists; no second receipt.
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_conversion_replay_conflict_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The same conversion command id with different membership material is
    a typed conflict, never a silent second receipt."""
    conn = migrated_temp_db_connection
    p = _run_b5_pipeline(conn, tmp_path, "convconf")
    with pytest.raises(ConversionIdempotencyConflictError):
        convert_confirmed_receipt_proposal_to_facts(
            conn,
            _conversion_command(
                p.suffix,
                p.ingestion,
                p.proposal_content_hash,
                participants=entries(("person_owner", 1), ("person_alice", 1), ("person_bob", 1)),
            ),
        )
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_receipt_without_fact_set_is_not_calculator_ready(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A converted receipt without persisted IAF facts is reported not
    ready and the bridge prepare fails closed with zero persisted effects."""
    conn = migrated_temp_db_connection
    content = _receipt_jpeg("nofacts")
    with conn:
        intake = create_raw_intake_record(
            conn,
            "telegram photo message: no facts yet",
            source_type="telegram_image",
            source_channel="telegram",
            public_id="raw_b5_nofacts",
        )
    acquisition = acquire_and_persist_telegram_attachment(
        conn,
        transport=_make_transport(content),
        storage_root=_private_storage(tmp_path, "attachments_nofacts"),
        public_id="tgae_b5_nofacts",
        raw_intake_id=int(intake["id"]),
        telegram_file_id="file_b5_nofacts",
        telegram_file_unique_id="unique_b5_nofacts",
        original_filename="receipt_nofacts.jpg",
        declared_mime_type="image/jpeg",
    )
    extract_and_persist_receipt_ocr_evidence(
        conn,
        public_id="rocr_b5_nofacts",
        attachment_id=int(acquisition.persistence_result["attachment_id"]),
        engine=FakeEngine(
            result=ReceiptOcrEngineResult(
                status=ReceiptOcrExtractionStatus.SUCCEEDED,
                blocks=_sgd_blocks(),
                outcome_code="ok",
            )
        ),
        limits=ReceiptOcrLimits(),
    )
    ingestion = ingest_receipt_ocr_evidence_as_total_expense_proposal(
        conn,
        extraction_public_id="rocr_b5_nofacts",
        proposal_public_id="prop_b5_nofacts",
        link_public_id="ropl_b5_nofacts",
    )
    confirm_proposal(
        conn,
        ingestion.parser_output_id,
        actor="owner",
        confirmation_public_id="pca_b5_nofacts",
    )
    seed_people(conn)
    expected = compute_effective_proposal_content_hash(conn, {"id": ingestion.parser_output_id})
    conversion = convert_confirmed_receipt_proposal_to_facts(
        conn, _conversion_command("nofacts", ingestion, expected)
    )
    # Before any downstream facts exist the exact conversion command
    # replays idempotently without a second receipt.
    replay = convert_confirmed_receipt_proposal_to_facts(
        conn, _conversion_command("nofacts", ingestion, expected)
    )
    assert replay.idempotent is True
    assert replay.receipt_public_id == conversion.receipt_public_id
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1

    readiness = report_receipt_calculator_readiness(conn, conversion.receipt_public_id)
    assert readiness.is_calculator_ready is False
    assert "no_authoritative_item_facts" in readiness.not_ready_reasons
    with pytest.raises(ReceiptNotCalculatorReadyError):
        prepare_receipt_calculation(conn, conversion.receipt_public_id)
    assert (
        conn.execute("SELECT COUNT(*) FROM authoritative_calculation_snapshots").fetchone()[0] == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM calc_audit_runs").fetchone()[0] == 0


def test_finalizer_failure_injection_rolls_back_e2e(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash injected at a finalizer seam after the full public pipeline
    leaves zero canonical facts and an unconsumed authorization."""
    conn = migrated_temp_db_connection
    p = _run_b5_pipeline(conn, tmp_path, "inject")
    prepared = prepare_receipt_calculation(conn, p.conversion.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected finalizer seam failure")

    monkeypatch.setattr(finalizer, "_insert_settlement_obligations", _boom)
    with pytest.raises(RuntimeError, match="injected finalizer seam failure"):
        finalize_prepared_receipt(conn, authorization)

    counts = _counts(conn, CANONICAL_FACT_TABLES)
    assert counts["transactions"] == 0
    assert counts["settlement_obligations"] == 0
    assert counts["calculation_runs"] == 0
    assert counts["receipt_finalization_audit"] == 0
    state = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (prepared.authorization_id,),
    ).fetchone()["authorization_state"]
    assert state == "authorized"
    assert not conn.in_transaction

    # The pipeline recovers on a clean retry.
    monkeypatch.undo()
    output = finalize_prepared_receipt(conn, authorization)
    assert output.status == "finalized"


def test_staging_guard_rejects_untrusted_database(tmp_path: Path) -> None:
    """Both the acquisition boundary and the bridge fail closed on a plain
    (non-staging) database before any side effect."""
    plain = sqlite3.connect(str(tmp_path / "plain.db"))
    transport = _make_transport(_receipt_jpeg("guard"))
    try:
        with pytest.raises(StagingDatabaseRejectedError):
            acquire_and_persist_telegram_attachment(
                plain,
                transport=transport,
                storage_root=_private_storage(tmp_path, "attachments_guard"),
                public_id="tgae_b5_guard",
                raw_intake_id=1,
                telegram_file_id="file_b5_guard",
                telegram_file_unique_id="unique_b5_guard",
                original_filename="receipt_guard.jpg",
                declared_mime_type="image/jpeg",
            )
        with pytest.raises(StagingDatabaseError):
            prepare_receipt_calculation(plain, "rcpt_anything")
    finally:
        plain.close()
    assert transport.metadata_calls == 0
    assert transport.download_calls == 0
