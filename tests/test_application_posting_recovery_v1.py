"""Independent posting recovery against signed, durable synthetic evidence."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import sqlite3
import subprocess
import sys
import threading
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from finance_core.application import admission
from finance_core.application.posting_contract import (
    POSTING_DECISION_SCHEMA,
    posting_review_sha256,
)
from finance_core.intake.raw_text_repository import create_raw_intake_record, save_parser_proposal
from finance_core.parsers.text_expense_parser import parse_text_expense
from tests.conftest import connect_temp_db

NOW = 2_000
BINDING = admission.TrustedBinding(
    "synthetic-instance",
    "synthetic-human",
    "synthetic-submitter",
    "synthetic-source-domain",
    "synthetic-source-key",
    "synthetic-posting-decision-domain",
    "synthetic-posting-decision-key",
)
SOURCE_KEY = b"synthetic source signing root for posting tests"
DECISION_KEY = b"synthetic decision signing root for posting tests"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _signature(table: str, value: object, key: bytes) -> str:
    payload = (table + "\x00" + _canonical(value)).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _persist(
    connection: sqlite3.Connection,
    table: str,
    record_id: str,
    material: dict[str, object],
    key: bytes,
) -> None:
    connection.execute(
        f"INSERT INTO {table} (id, material, signature) VALUES (?, ?, ?)",
        (record_id, _canonical(material), _signature(table, material, key)),
    )


def _replace(
    connection: sqlite3.Connection,
    table: str,
    record_id: str,
    material: dict[str, object],
    key: bytes,
) -> None:
    connection.execute(
        f"UPDATE {table} SET material=?,signature=? WHERE id=?",
        (_canonical(material), _signature(table, material, key), record_id),
    )


def _load(
    connection: sqlite3.Connection,
    table: str,
    record_id: str,
    key: bytes,
) -> dict[str, object]:
    row = connection.execute(
        f"SELECT material, signature FROM {table} WHERE id = ?", (record_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Missing durable synthetic evidence in {table}")
    material = json.loads(row[0])
    if not isinstance(material, dict) or not hmac.compare_digest(
        str(row[1]), _signature(table, material, key)
    ):
        raise ValueError(f"Invalid durable synthetic evidence in {table}")
    return material


class _DurableSourceVerifier:
    """Verify signed source records and the raw content in this SQLite file."""

    def __init__(self, *, workspace: Any = None, manifest: Any = None) -> None:
        self._workspace = workspace
        self._manifest = manifest

    def verify_persisted(self, connection: sqlite3.Connection, intake_public_id: str):
        rows = connection.execute("SELECT id FROM synthetic_sources ORDER BY id").fetchall()
        sources = [_load(connection, "synthetic_sources", str(row[0]), SOURCE_KEY) for row in rows]
        matches = [source for source in sources if source["intake_public_id"] == intake_public_id]
        if len(matches) != 1:
            raise ValueError("Synthetic source is absent or ambiguous")
        source = matches[0]
        if (
            sum(candidate["source_event_id"] == source["source_event_id"] for candidate in sources)
            != 1
        ):
            raise ValueError("Synthetic source event is not unique")
        raw = connection.execute(
            "SELECT raw_input, source_content_hash FROM raw_intake_records WHERE public_id = ?",
            (intake_public_id,),
        ).fetchone()
        if raw is None:
            raise ValueError("Raw source content is missing")
        raw_hash = hashlib.sha256(str(raw[0]).encode("utf-8")).hexdigest()
        if raw_hash != source["source_content_hash"] or raw[1] != f"sha256:{raw_hash}":
            raise ValueError("Raw source content does not match signed evidence")
        if self._workspace is not None:
            from finance_core.receipt_staging_runner.local_intake import (
                require_local_runner_receipt_proposal,
            )

            proposal_row = connection.execute(
                "SELECT parser_outputs.public_id FROM raw_intake_records "
                "JOIN parser_outputs ON parser_outputs.id=raw_intake_records.parser_output_id "
                "WHERE raw_intake_records.public_id=?",
                (intake_public_id,),
            ).fetchone()
            if proposal_row is None:
                raise ValueError("Receipt proposal is missing from the durable source")
            require_local_runner_receipt_proposal(
                connection,
                str(proposal_row[0]),
                workspace=self._workspace,
                manifest=self._manifest,
            )
        unsigned = {key: value for key, value in source.items() if key != "evidence_digest"}
        if _digest(unsigned) != source["evidence_digest"]:
            raise ValueError("Source evidence digest is invalid")
        return admission.VerifiedSource(**source)


class _DurablePostingDecisionAuthority:
    """Read and verify signed posting decision/display evidence on every call."""

    @staticmethod
    def _display_closures(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
        rows = connection.execute(
            "SELECT id FROM synthetic_display_closures ORDER BY id"
        ).fetchall()
        closures: dict[str, dict[str, object]] = {}
        for row in rows:
            closure_id = str(row[0])
            material = _load(connection, "synthetic_display_closures", closure_id, DECISION_KEY)
            display_id = str(material["display_id"])
            if display_id in closures:
                raise ValueError("Synthetic display has contradictory closure evidence")
            closures[display_id] = material
        return closures

    @staticmethod
    def _current_displays(
        connection: sqlite3.Connection, review_id: str, closures: dict[str, dict[str, object]]
    ) -> list[dict[str, object]]:
        rows = connection.execute("SELECT id FROM synthetic_displays ORDER BY id").fetchall()
        current = []
        for row in rows:
            display_id = str(row[0])
            display = _load(connection, "synthetic_displays", display_id, DECISION_KEY)
            if display["review_id"] == review_id and display_id not in closures:
                current.append(display)
        return current

    def verify_persisted(self, connection, decision_record_id, expected):
        from finance_core.application.posting import VerifiedPostingDecision

        decision = _load(connection, "synthetic_decisions", decision_record_id, DECISION_KEY)
        display = _load(connection, "synthetic_displays", str(decision["display_id"]), DECISION_KEY)
        reply = _load(connection, "synthetic_replies", str(decision["reply_id"]), DECISION_KEY)
        display_id = str(decision["display_id"])
        closures = self._display_closures(connection)
        closure = closures.get(display_id)
        current_displays = self._current_displays(connection, expected.review_id, closures)
        if expected.accepted_attempt_id is None:
            if (
                closure is not None
                or len(current_displays) != 1
                or current_displays[0] != display
                or any(
                    item["projection"] != expected.review_projection
                    or item["human_principal_id"] != expected.binding.human_principal_id
                    or item["instance_id"] != expected.binding.instance_id
                    for item in current_displays
                )
            ):
                raise ValueError("Fresh decision is not bound to the exact current display")
        else:
            accepted = connection.execute(
                "SELECT decision_id,decision_digest,accepted_at FROM "
                "application_posting_decisions WHERE attempt_id=?",
                (expected.accepted_attempt_id,),
            ).fetchone()
            if (
                accepted is None
                or accepted["decision_id"] != decision_record_id
                or accepted["decision_digest"] != decision["decision_digest"]
                or accepted["accepted_at"] != expected.checked_at
                or not current_displays
            ):
                raise ValueError("Historical decision is not owned by this accepted attempt")
            if closure is not None and (
                closure["review_id"] != expected.review_id
                or closure["superseded_at"] <= expected.checked_at
                or closure["projection_hash"] != posting_review_sha256(expected.review_projection)
            ):
                raise ValueError("Historical display was not current at acceptance")
            if any(item["projection"] != expected.review_projection for item in current_displays):
                raise ValueError("Current replacement display differs from accepted review")
        unsigned_decision = {
            key: value for key, value in decision.items() if key != "decision_digest"
        }
        if _digest(unsigned_decision) != decision["decision_digest"]:
            raise ValueError("Decision digest is invalid")
        if (
            reply["schema"] != "finance-application-synthetic-human-reply-v1"
            or reply["decision_id"] != decision_record_id
            or reply["review_id"] != expected.review_id
            or reply["display_id"] != display_id
            or reply["display_evidence_digest"] != decision["display_evidence_digest"]
            or reply["projection_hash"] != posting_review_sha256(expected.review_projection)
            or reply["human_principal_id"] != expected.binding.human_principal_id
            or reply["instance_id"] != expected.binding.instance_id
            or reply["action"] != "confirm"
            or reply["origin"] != "human"
            or reply["direct"] is not True
            or reply["private"] is not True
        ):
            raise ValueError("Human reply is not bound to the signed current review card")
        if (
            _digest(display) != decision["display_evidence_digest"]
            or display["state"] != "delivered"
            or display["direct"] is not True
            or display["origin"] != "human"
            or display["private"] is not True
            or display["current"] is not True
            or display["human_principal_id"] != expected.binding.human_principal_id
            or display["instance_id"] != expected.binding.instance_id
            or display["source_evidence_id"] != expected.source.evidence_id
            or display["source_evidence_digest"] != expected.source.evidence_digest
            or display["review_id"] != expected.review_id
            or display["projection"] != expected.review_projection
        ):
            raise ValueError("Display is not the exact current signed posting review")
        if (
            decision["schema"] != POSTING_DECISION_SCHEMA
            or decision["namespace"] != expected.binding.decision_namespace
            or decision["key_id"] != expected.binding.decision_key_id
            or decision["instance_id"] != expected.binding.instance_id
            or decision["human_principal_id"] != expected.binding.human_principal_id
            or decision["source_evidence_id"] != expected.source.evidence_id
            or decision["source_evidence_digest"] != expected.source.evidence_digest
            or decision["proposal_public_id"] != expected.proposal_public_id
            or decision["proposal_version"] != expected.proposal_version
            or decision["proposal_content_hash"] != expected.proposal_content_hash
            or decision["review_id"] != expected.review_id
            or decision["review_projection_hash"] != expected.review_projection_hash
            or decision["review_projection_hash"]
            != posting_review_sha256(expected.review_projection)
            or decision["action"] != "confirm"
            or decision["consumed"] is not False
        ):
            raise ValueError("Decision is not bound to the expected posting review")
        checked_at = expected.checked_at
        if not (decision["issued_at"] <= checked_at < decision["expires_at"]):
            raise ValueError("Decision is outside its signed validity interval")
        return VerifiedPostingDecision(
            **{key: value for key, value in decision.items() if key != "reply_id"}
        )


def _decision_material(review, proposal_public_id: str, decision_id: str) -> tuple[dict, dict]:
    projection = dict(review.projection)
    source_evidence_id = str(projection["source_evidence_id"])
    source_evidence_digest = str(projection["source_evidence_digest"])
    display_id = f"synthetic-display-{review.review_id}"
    display = {
        "state": "delivered",
        "direct": True,
        "origin": "human",
        "private": True,
        "current": True,
        "human_principal_id": BINDING.human_principal_id,
        "instance_id": BINDING.instance_id,
        "source_evidence_id": source_evidence_id,
        "source_evidence_digest": source_evidence_digest,
        "review_id": review.review_id,
        "projection": projection,
    }
    decision = {
        "schema": POSTING_DECISION_SCHEMA,
        "namespace": BINDING.decision_namespace,
        "key_id": BINDING.decision_key_id,
        "instance_id": BINDING.instance_id,
        "human_principal_id": BINDING.human_principal_id,
        "decision_id": decision_id,
        "source_evidence_id": source_evidence_id,
        "source_evidence_digest": source_evidence_digest,
        "proposal_public_id": proposal_public_id,
        "proposal_version": projection["proposal_version"],
        "proposal_content_hash": projection["effective_content_hash"],
        "review_id": review.review_id,
        "reply_id": f"synthetic-reply-{decision_id}",
        "review_projection_hash": posting_review_sha256(projection),
        "display_id": display_id,
        "display_evidence_digest": _digest(display),
        "action": "confirm",
        "issued_at": NOW - 1,
        "expires_at": review.expires_at,
        "consumed": False,
    }
    decision["decision_digest"] = _digest(decision)
    return display, decision


def _reply_material(decision: dict) -> dict:
    return {
        "schema": "finance-application-synthetic-human-reply-v1",
        "decision_id": decision["decision_id"],
        "review_id": decision["review_id"],
        "display_id": decision["display_id"],
        "display_evidence_digest": decision["display_evidence_digest"],
        "projection_hash": decision["review_projection_hash"],
        "human_principal_id": BINDING.human_principal_id,
        "instance_id": BINDING.instance_id,
        "action": "confirm",
        "origin": "human",
        "direct": True,
        "private": True,
        "created_at": NOW,
    }


def _supersede_current_display(
    connection: sqlite3.Connection,
    review,
    decision_id: str,
    display_id: str,
) -> str:
    """Append signed closure/replacement evidence without rewriting old display proof."""
    old_display = _load(connection, "synthetic_displays", display_id, DECISION_KEY)
    closure_id = f"synthetic-closure-{decision_id}"
    closure = {
        "display_id": display_id,
        "review_id": review.review_id,
        "superseded_at": NOW + 1,
        "projection_hash": posting_review_sha256(review.projection),
    }
    _persist(connection, "synthetic_display_closures", closure_id, closure, DECISION_KEY)
    replacement_id = f"synthetic-display-refresh-{decision_id}"
    replacement = {
        **old_display,
        "projection": dict(review.projection),
    }
    _persist(connection, "synthetic_displays", replacement_id, replacement, DECISION_KEY)
    connection.commit()
    return replacement_id


def _prepare_text_subject(
    connection: sqlite3.Connection,
    suffix: str = "one",
    *,
    source_event_id: str | None = None,
    raw_text: str = "Lunch SGD 12.50 at Example Cafe",
    merchant: str = "Example Cafe",
    description: str = "Lunch",
    category: str = "food",
):
    from finance_core.application.posting import PostingService

    for table in (
        "synthetic_sources",
        "synthetic_displays",
        "synthetic_decisions",
        "synthetic_replies",
        "synthetic_display_closures",
    ):
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
        )
    intake = create_raw_intake_record(
        connection,
        raw_text,
        source_type="manual_entry",
        source_channel="manual",
        public_id=f"synthetic-intake-{suffix}",
        received_at="2026-10-08T00:00:00Z",
    )
    parsed = parse_text_expense(
        raw_text,
        raw_input_reference=intake["public_id"],
        source_type="manual_entry",
    )
    parsed.update(
        {
            "amount": "12.50",
            "currency": "SGD",
            "transaction_date": "2026-10-08",
            "merchant": merchant,
            "description": description,
            "category": category,
            "intent": "personal_expense_log",
            "transaction_type": "personal_expense",
        }
    )
    proposal = save_parser_proposal(connection, intake["id"], parsed)
    source = {
        "schema": admission.SOURCE_SCHEMA,
        "namespace": BINDING.source_namespace,
        "key_id": BINDING.source_key_id,
        "instance_id": BINDING.instance_id,
        "submission_client_id": BINDING.submission_client_id,
        "evidence_id": f"synthetic-source-{suffix}",
        "intake_public_id": intake["public_id"],
        "source_event_id": source_event_id or f"synthetic-event-{suffix}",
        "source_content_hash": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "source_occurred_at": NOW - 2,
        "received_at": NOW - 1,
    }
    source["evidence_digest"] = _digest(source)
    _persist(connection, "synthetic_sources", source["evidence_id"], source, SOURCE_KEY)
    connection.commit()

    service = PostingService(
        connection=connection,
        source_verifier=_DurableSourceVerifier(),
        human_decision_authority=_DurablePostingDecisionAuthority(),
        binding=BINDING,
        clock=lambda: NOW,
    )
    review = service.prepare(proposal["public_id"])
    decision_id = f"synthetic-posting-decision-{suffix}"
    display, decision = _decision_material(review, proposal["public_id"], decision_id)
    _persist(
        connection,
        "synthetic_displays",
        display_id := str(decision["display_id"]),
        display,
        DECISION_KEY,
    )
    _persist(
        connection,
        "synthetic_replies",
        str(decision["reply_id"]),
        _reply_material(decision),
        DECISION_KEY,
    )
    _persist(connection, "synthetic_decisions", decision_id, decision, DECISION_KEY)
    connection.commit()
    return service, review, proposal, intake, decision_id, display_id


def _prepare_receipt_subject(
    tmp_path: Path,
    suffix: str = "receipt",
    *,
    merchant_text: str = "EXAMPLE CAFE",
):
    """Prepare a real local-file receipt custody chain with signed synthetic authority."""
    from finance_core.application.posting import PostingService
    from finance_core.intake.receipt_ocr_evidence import (
        ReceiptOcrBlock,
        ReceiptOcrEngineIdentity,
        ReceiptOcrEngineResult,
        ReceiptOcrExtractionStatus,
        ReceiptOcrLimits,
        ReceiptOcrSource,
    )
    from finance_core.receipt_staging_runner.local_intake import run_local_receipt_intake
    from finance_core.receipt_staging_runner.models import parse_runner_manifest
    from finance_core.receipt_staging_runner.participants import bootstrap_participants
    from finance_core.receipt_staging_runner.workspace import create_runner_workspace
    from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
    from finance_core.staging_guard import create_staging_database

    manifest_bytes = json.dumps(
        {
            "schema_version": "v1",
            "workspace_identity": f"posting_{suffix}",
            "operator_actor_id": BINDING.human_principal_id,
            "participants": [
                {"public_id": "ptcp_posting_self", "display_name": "Self", "is_self": True}
            ],
        },
        separators=(",", ":"),
    ).encode()
    manifest = parse_runner_manifest(manifest_bytes)
    workspace = create_runner_workspace(str(tmp_path / f"workspace-{suffix}"), manifest)
    connection = create_staging_database(
        workspace.database_path, migration_paths=TEMP_DB_MIGRATION_PATHS
    )
    bootstrap_participants(connection, manifest)

    class _SyntheticReceiptEngine:
        @property
        def identity(self):
            return ReceiptOcrEngineIdentity(
                name="synthetic_local_test_ocr",
                version="1",
                binary_sha256="a" * 64,
                configuration_hash="b" * 64,
            )

        def extract(self, _source: ReceiptOcrSource, *, limits: ReceiptOcrLimits, deadline: float):
            del limits, deadline

            def block(index: int, text: str, line: int, left: int) -> ReceiptOcrBlock:
                return ReceiptOcrBlock(
                    sequence_index=index,
                    page_index=0,
                    engine_block_index=0,
                    engine_paragraph_index=0,
                    engine_line_index=line,
                    engine_word_index=index,
                    text=text,
                    left=left,
                    top=20 + line * 40,
                    width=50,
                    height=10,
                    page_width=800,
                    page_height=600,
                    confidence_scaled=9500,
                )

            return ReceiptOcrEngineResult(
                status=ReceiptOcrExtractionStatus.SUCCEEDED,
                blocks=(
                    block(0, merchant_text, 0, 10),
                    block(1, "2026-10-08", 1, 10),
                    block(2, "TOTAL", 2, 10),
                    block(3, "SGD", 2, 80),
                    block(4, "12.50", 2, 140),
                ),
                outcome_code="synthetic_success",
            )

    source_path = tmp_path / f"external-{suffix}" / "receipt.jpg"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"\xff\xd8\xff\xe0synthetic-local-receipt-" + suffix.encode())
    ingestion = run_local_receipt_intake(
        connection,
        workspace=workspace,
        manifest=manifest,
        source_image_path=str(source_path),
        engine=_SyntheticReceiptEngine(),
        # Local intake caps this prefix at 32 characters; include a stable,
        # collision-resistant fixture suffix rather than the verbose case name.
        public_id_prefix=f"post_{hashlib.sha256(suffix.encode()).hexdigest()[:16]}",
    )
    proposal = ingestion.ingestion.proposal_public_id
    raw_row = connection.execute(
        "SELECT public_id,raw_input,source_content_hash FROM raw_intake_records "
        "WHERE parser_output_id=(SELECT id FROM parser_outputs WHERE public_id=?)",
        (proposal,),
    ).fetchone()
    if raw_row is None:
        raise AssertionError("Local receipt intake did not persist its raw source")
    intake_public_id = str(raw_row["public_id"])
    source = {
        "schema": admission.SOURCE_SCHEMA,
        "namespace": BINDING.source_namespace,
        "key_id": BINDING.source_key_id,
        "instance_id": BINDING.instance_id,
        "submission_client_id": BINDING.submission_client_id,
        "evidence_id": f"synthetic-source-{suffix}",
        "intake_public_id": intake_public_id,
        "source_event_id": f"synthetic-event-{suffix}",
        "source_content_hash": hashlib.sha256(str(raw_row["raw_input"]).encode()).hexdigest(),
        "source_occurred_at": NOW - 2,
        "received_at": NOW - 1,
    }
    source["evidence_digest"] = _digest(source)
    for table in (
        "synthetic_sources",
        "synthetic_displays",
        "synthetic_decisions",
        "synthetic_replies",
        "synthetic_display_closures",
    ):
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
        )
    _persist(connection, "synthetic_sources", source["evidence_id"], source, SOURCE_KEY)
    connection.commit()
    source_verifier = _DurableSourceVerifier(workspace=workspace, manifest=manifest)
    decision_authority = _DurablePostingDecisionAuthority()
    service = PostingService(
        connection=connection,
        source_verifier=source_verifier,
        human_decision_authority=decision_authority,
        binding=BINDING,
        clock=lambda: NOW,
    )
    review = service.prepare(proposal)
    decision_id = f"synthetic-posting-decision-{suffix}"
    display, decision = _decision_material(review, proposal, decision_id)
    _persist(connection, "synthetic_displays", str(decision["display_id"]), display, DECISION_KEY)
    _persist(
        connection,
        "synthetic_replies",
        str(decision["reply_id"]),
        _reply_material(decision),
        DECISION_KEY,
    )
    _persist(connection, "synthetic_decisions", decision_id, decision, DECISION_KEY)
    connection.commit()
    return (
        connection,
        workspace,
        manifest,
        service,
        review,
        proposal,
        intake_public_id,
        decision_id,
        str(decision["display_id"]),
        source_verifier,
        decision_authority,
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _assert_counts(connection: sqlite3.Connection, expected: dict[str, int]) -> None:
    for table, count in expected.items():
        assert _count(connection, table) == count, table


def _temporarily_drop_triggers(
    connection: sqlite3.Connection,
    trigger_names: tuple[str, ...],
    mutate,
) -> None:
    definitions = []
    for name in trigger_names:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        assert row is not None, name
        definitions.append(str(row[0]))
    for name in trigger_names:
        connection.execute(f'DROP TRIGGER "{name}"')
    try:
        mutate()
    finally:
        for definition in definitions:
            connection.execute(definition)
    connection.commit()
    for name in trigger_names:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
            ).fetchone()
            is not None
        )


def _delete_durable_proof(
    connection: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: tuple[object, ...],
    *,
    trigger_names: tuple[str, ...] = (),
) -> int:
    deleted = 0

    def delete() -> None:
        nonlocal deleted
        cursor = connection.execute(f"DELETE FROM {table} WHERE {where_sql}", params)
        deleted = cursor.rowcount
        assert deleted > 0, (table, where_sql, params)

    _temporarily_drop_triggers(connection, trigger_names, delete)
    return deleted


def _assert_read_refusal_is_write_free(
    service,
    connection: sqlite3.Connection,
    attempt_id: str,
    expected_counts: dict[str, int],
) -> None:
    from finance_core.application.posting import PostingError

    for operation in (service.get_status, service.resume_post):
        before_changes = connection.total_changes
        with pytest.raises(PostingError):
            operation(attempt_id)
        assert connection.total_changes == before_changes
        _assert_counts(connection, expected_counts)


def test_text_posting_persists_once_and_reopens_to_same_canonical_result(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    service, review, proposal, _intake, decision_id, _display_id = _prepare_text_subject(connection)

    assert _count(connection, "application_posting_reviews") == 1
    assert _count(connection, "application_posting_decisions") == 0
    assert _count(connection, "application_posting_attempts") == 0
    assert _count(connection, "application_posting_events") == 0
    assert _count(connection, "parser_proposal_authorizations") == 0
    assert _count(connection, "transactions") == 0
    accepted = service.submit_post(review.review_id, decision_id)
    assert accepted.state == "finalized"
    assert accepted.transaction_public_id
    assert accepted.attempt_id
    assert _count(connection, "application_posting_reviews") == 1
    assert _count(connection, "application_posting_decisions") == 1
    assert _count(connection, "application_posting_attempts") == 1
    assert _count(connection, "application_posting_events") == 2
    assert _count(connection, "application_posting_receipt_evidence") == 0
    assert _count(connection, "parser_proposal_authorizations") == 1
    assert _count(connection, "transactions") == 1
    assert _count(connection, "receipt_item_allocation_fact_sets") == 0
    assert _count(connection, "receipt_finalization_authorizations") == 0
    assert _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is False

    transaction = connection.execute(
        "SELECT amount, currency, transaction_date, merchant FROM transactions WHERE public_id = ?",
        (accepted.transaction_public_id,),
    ).fetchone()
    assert transaction is not None
    assert (
        f"{Decimal(str(transaction[0])):.2f}",
        str(transaction[1]),
        str(transaction[2]),
        str(transaction[3]),
    ) == (
        "12.50",
        "SGD",
        "2026-10-08",
        "Example Cafe",
    )

    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    connection.close()
    reopened = connect_temp_db(database_path)
    try:
        reopened_service = type(service)(
            connection=reopened,
            source_verifier=_DurableSourceVerifier(),
            human_decision_authority=_DurablePostingDecisionAuthority(),
            binding=BINDING,
            clock=lambda: NOW + 10_000,
        )
        recovered = reopened_service.resume_post(accepted.attempt_id)
        observed = reopened_service.get_status(accepted.attempt_id)
        assert recovered.state == observed.state == "finalized"
        assert recovered.transaction_public_id == observed.transaction_public_id
        assert recovered.transaction_public_id == accepted.transaction_public_id
        assert _count(reopened, "application_posting_reviews") == 1
        assert _count(reopened, "application_posting_decisions") == 1
        assert _count(reopened, "application_posting_attempts") == 1
        assert _count(reopened, "application_posting_events") == 2
        assert _count(reopened, "transactions") == 1
        assert _count(reopened, "parser_proposal_authorizations") == 1
        assert _count(reopened, "parser_outputs") == 1
    finally:
        reopened.close()


def test_text_normalizes_displayed_unicode_but_preserves_exact_raw_source(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    merchant_nfd = unicodedata.normalize("NFD", "Café Market")
    description_nfd = unicodedata.normalize("NFD", "Déjeuner")
    category_nfd = unicodedata.normalize("NFD", "Café")
    raw_text = f"{description_nfd} SGD 12.50 at {merchant_nfd}"
    service, review, proposal, intake, decision_id, _display_id = _prepare_text_subject(
        connection,
        suffix="unicode-source",
        raw_text=raw_text,
        merchant=merchant_nfd,
        description=description_nfd,
        category=category_nfd,
    )
    approved = review.projection["financial_projection"]
    assert approved["merchant"] == unicodedata.normalize("NFC", merchant_nfd)
    assert approved["description"] == unicodedata.normalize("NFC", description_nfd)
    assert approved["category"] == unicodedata.normalize("NFC", category_nfd)
    persisted_raw = connection.execute(
        "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
        (intake["public_id"],),
    ).fetchone()
    assert persisted_raw["raw_input"] == raw_text
    source = _load(
        connection,
        "synthetic_sources",
        "synthetic-source-unicode-source",
        SOURCE_KEY,
    )
    raw_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    assert persisted_raw["source_content_hash"] == f"sha256:{raw_digest}"
    assert source["source_content_hash"] == raw_digest

    posted = service.submit_post(review.review_id, decision_id)
    assert posted.state == "finalized"
    actual = connection.execute(
        "SELECT merchant,category FROM transactions WHERE public_id=?",
        (posted.transaction_public_id,),
    ).fetchone()
    assert actual[0] == unicodedata.normalize("NFC", merchant_nfd)
    assert actual[1] == unicodedata.normalize("NFC", category_nfd)
    assert proposal["public_id"] == review.projection["proposal_public_id"]


def test_finalized_text_missing_conversion_audit_event_refuses_status_and_resume(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    from finance_core.application.posting import PostingError

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix="missing-conversion-audit-event"
    )
    posted = service.submit_post(review.review_id, decision_id)
    assert posted.state == "finalized"
    conversion = connection.execute(
        "SELECT event_public_id FROM financial_audit_events "
        "WHERE aggregate_type='parser_proposal' AND aggregate_public_id=? "
        "AND event_type='parser_proposal_converted'",
        (review.projection["proposal_public_id"],),
    ).fetchone()
    assert conversion is not None

    _delete_durable_proof(
        connection,
        "financial_audit_events",
        "event_public_id=?",
        (conversion["event_public_id"],),
        trigger_names=("trg_financial_audit_events_no_delete",),
    )
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_financial_audit_events_no_delete'"
        ).fetchone()
        is not None
    )
    expected_counts = {
        "application_posting_decisions": 1,
        "application_posting_attempts": 1,
        "application_posting_events": 2,
        "application_posting_receipt_evidence": 0,
        "parser_proposal_authorizations": 1,
        "parser_proposal_conversion_audit": 1,
        "financial_audit_events": _count(connection, "financial_audit_events"),
        "transactions": 1,
        "receipt_proposal_conversions": 0,
        "receipt_item_allocation_fact_sets": 0,
    }
    before_changes = connection.total_changes
    with pytest.raises(PostingError):
        service.get_status(posted.attempt_id)
    assert connection.total_changes == before_changes
    _assert_counts(connection, expected_counts)
    with pytest.raises(PostingError):
        service.resume_post(posted.attempt_id)
    assert connection.total_changes == before_changes
    _assert_counts(connection, expected_counts)


def test_canonical_text_transaction_tamper_is_not_reported_as_finalized(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix="canonical-tamper"
    )
    posted = service.submit_post(review.review_id, decision_id)
    assert posted.state == "finalized"
    trigger = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name='trg_correction_transactions_no_update'"
    ).fetchone()
    assert trigger is not None
    # Bypass the ordinary immutability guard only to model stored-result
    # corruption in this disposable acceptance database.
    connection.execute("DROP TRIGGER trg_correction_transactions_no_update")
    connection.execute(
        "UPDATE transactions SET amount=amount+1 WHERE public_id=?",
        (posted.transaction_public_id,),
    )
    connection.execute(str(trigger[0]))
    connection.commit()
    before_status_changes = connection.total_changes
    with pytest.raises(ValueError, match="Canonical parser transaction drifted"):
        service.get_status(posted.attempt_id)
    assert connection.total_changes == before_status_changes
    assert _count(connection, "application_posting_decisions") == 1
    assert _count(connection, "application_posting_attempts") == 1
    assert _count(connection, "transactions") == 1


@pytest.mark.parametrize("refusal", ["caller_transaction", "foreign_keys_off", "schema_drift"])
def test_posting_refuses_unowned_or_untrusted_database_state_without_writes(
    migrated_temp_db_connection: sqlite3.Connection,
    refusal: str,
) -> None:
    from finance_core.application.posting import PostingError
    from finance_core.application.posting_contract import PostingContractError
    from finance_core.sqlite_connection import ForeignKeysDisabledError

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix=f"refusal-{refusal}"
    )
    if refusal == "caller_transaction":
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TEMP TABLE caller_owned_sentinel(value TEXT)")
        connection.execute("INSERT INTO caller_owned_sentinel VALUES ('keep')")
        expected_error = PostingError
    elif refusal == "foreign_keys_off":
        connection.execute("PRAGMA foreign_keys=OFF")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        expected_error = ForeignKeysDisabledError
    else:
        connection.execute("CREATE TABLE application_posting_contract_probe(value TEXT NOT NULL)")
        connection.commit()
        expected_error = PostingContractError

    before_changes = connection.total_changes
    with pytest.raises(expected_error):
        service.submit_post(review.review_id, decision_id)
    assert connection.total_changes == before_changes
    _assert_counts(
        connection,
        {
            "application_posting_decisions": 0,
            "application_posting_attempts": 0,
            "application_posting_events": 0,
            "application_posting_receipt_evidence": 0,
            "parser_proposal_authorizations": 0,
            "receipt_proposal_conversions": 0,
            "receipts": 0,
            "receipt_item_allocation_fact_sets": 0,
            "authoritative_calculation_snapshots": 0,
            "receipt_finalization_authorizations": 0,
            "application_conditional_authorization_proofs": 0,
            "transactions": 0,
        },
    )
    assert _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is False
    if refusal == "caller_transaction":
        assert connection.in_transaction
        assert connection.execute("SELECT value FROM caller_owned_sentinel").fetchone()[0] == "keep"
        connection.rollback()
    else:
        assert not connection.in_transaction


def test_committed_acceptance_recovers_after_return_is_lost(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.application import posting
    from finance_core.application.posting import PostingService

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix="acceptance-crash"
    )

    def lose_return(stage: str) -> None:
        if stage == "after_acceptance_commit":
            raise RuntimeError("injected acceptance return loss")

    monkeypatch.setattr(posting, "_failure_injection_hook", lose_return)
    with pytest.raises(RuntimeError, match="injected acceptance return loss"):
        service.submit_post(review.review_id, decision_id)

    assert _count(connection, "application_posting_reviews") == 1
    assert _count(connection, "application_posting_decisions") == 1
    assert _count(connection, "application_posting_attempts") == 1
    assert _count(connection, "application_posting_events") == 1
    assert _count(connection, "parser_proposal_authorizations") == 1
    assert _count(connection, "transactions") == 0
    attempt_id = str(
        connection.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
    )
    assert _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is False
    _supersede_current_display(connection, review, decision_id, _display_id)

    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    connection.close()
    reopened = connect_temp_db(database_path)
    try:
        monkeypatch.setattr(posting, "_failure_injection_hook", None)
        recovery_service = PostingService(
            connection=reopened,
            source_verifier=_DurableSourceVerifier(),
            human_decision_authority=_DurablePostingDecisionAuthority(),
            binding=BINDING,
            clock=lambda: NOW + 10_000,
        )
        recovered = recovery_service.resume_post(attempt_id)
        assert recovered.state == "finalized"
        assert recovered.transaction_public_id
        assert _count(reopened, "application_posting_decisions") == 1
        assert _count(reopened, "application_posting_attempts") == 1
        assert _count(reopened, "application_posting_events") == 2
        assert _count(reopened, "parser_proposal_authorizations") == 1
        assert _count(reopened, "transactions") == 1
        assert (
            _load(reopened, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is False
        )
    finally:
        reopened.close()


def test_same_value_but_distinct_source_events_create_two_canonical_results(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    service_a, review_a, _proposal_a, _raw_a, decision_a, _display_a = _prepare_text_subject(
        connection, suffix="same-value-a"
    )
    service_b, review_b, _proposal_b, _raw_b, decision_b, _display_b = _prepare_text_subject(
        connection, suffix="same-value-b"
    )

    result_a = service_a.submit_post(review_a.review_id, decision_a)
    result_b = service_b.submit_post(review_b.review_id, decision_b)

    assert result_a.state == result_b.state == "finalized"
    assert result_a.transaction_public_id != result_b.transaction_public_id
    assert _count(connection, "application_posting_reviews") == 2
    assert _count(connection, "application_posting_decisions") == 2
    assert _count(connection, "application_posting_attempts") == 2
    assert _count(connection, "application_posting_events") == 4
    assert _count(connection, "transactions") == 2
    assert _count(connection, "parser_proposal_authorizations") == 2


def test_duplicate_source_event_cannot_create_a_second_posting_attempt(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    connection = migrated_temp_db_connection
    same_event = "synthetic-event-once"
    service, review, _proposal, _raw, decision_id, _display = _prepare_text_subject(
        connection, suffix="first-source-event", source_event_id=same_event
    )
    first = service.submit_post(review.review_id, decision_id)
    assert first.state == "finalized"

    with pytest.raises(ValueError):
        _prepare_text_subject(
            connection, suffix="duplicate-source-event", source_event_id=same_event
        )

    assert _count(connection, "application_posting_reviews") == 1
    assert _count(connection, "application_posting_decisions") == 1
    assert _count(connection, "application_posting_attempts") == 1
    assert _count(connection, "transactions") == 1


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_human",
        "wrong_schema",
        "wrong_proposal_version",
        "wrong_content_hash",
        "expired",
        "already_consumed",
        "wrong_display_projection",
        "closed_current_display",
        "missing_source",
        "ambiguous_source",
        "bad_source_signature",
        "missing_decision",
        "bad_decision_signature",
        "missing_display",
        "missing_reply",
        "bad_display_signature",
        "bad_reply_signature",
    ],
)
def test_invalid_durable_posting_evidence_writes_nothing(
    migrated_temp_db_connection: sqlite3.Connection,
    fault: str,
) -> None:
    from finance_core.application.posting import PostingError

    connection = migrated_temp_db_connection
    service, review, proposal, _intake, decision_id, display_id = _prepare_text_subject(
        connection, suffix=f"refusal-{fault}"
    )
    if fault == "closed_current_display":
        _supersede_current_display(connection, review, decision_id, display_id)
    elif fault == "missing_source":
        connection.execute(
            "DELETE FROM synthetic_sources WHERE id=?",
            (f"synthetic-source-refusal-{fault}",),
        )
        connection.commit()
    elif fault == "ambiguous_source":
        source_id = "synthetic-source-refusal-ambiguous-second"
        source = _load(
            connection, "synthetic_sources", "synthetic-source-refusal-ambiguous_source", SOURCE_KEY
        )
        source["evidence_id"] = source_id
        source["source_event_id"] = "synthetic-event-refusal-ambiguous-second"
        unsigned = {key: value for key, value in source.items() if key != "evidence_digest"}
        source["evidence_digest"] = _digest(unsigned)
        _persist(connection, "synthetic_sources", source_id, source, SOURCE_KEY)
        connection.commit()
    elif fault == "bad_source_signature":
        connection.execute(
            "UPDATE synthetic_sources SET signature=? WHERE id=?",
            ("0" * 64, f"synthetic-source-refusal-{fault}"),
        )
        connection.commit()
    elif fault == "missing_decision":
        connection.execute("DELETE FROM synthetic_decisions WHERE id=?", (decision_id,))
        connection.commit()
    elif fault == "bad_decision_signature":
        connection.execute(
            "UPDATE synthetic_decisions SET signature=? WHERE id=?",
            ("0" * 64, decision_id),
        )
        connection.commit()
    elif fault == "missing_display":
        connection.execute("DELETE FROM synthetic_displays WHERE id=?", (display_id,))
        connection.commit()
    elif fault == "missing_reply":
        connection.execute(
            "DELETE FROM synthetic_replies WHERE id=?",
            (f"synthetic-reply-{decision_id}",),
        )
        connection.commit()
    elif fault == "bad_display_signature":
        connection.execute(
            "UPDATE synthetic_displays SET signature=? WHERE id=?",
            ("0" * 64, display_id),
        )
        connection.commit()
    elif fault == "bad_reply_signature":
        connection.execute(
            "UPDATE synthetic_replies SET signature=? WHERE id=?",
            ("0" * 64, f"synthetic-reply-{decision_id}"),
        )
        connection.commit()
    else:
        decision = _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)
        if fault == "wrong_human":
            decision["human_principal_id"] = "another-principal"
        elif fault == "wrong_schema":
            decision["schema"] = "finance-application-human-decision-v1"
        elif fault == "wrong_proposal_version":
            decision["proposal_version"] = int(decision["proposal_version"]) + 1
        elif fault == "wrong_content_hash":
            decision["proposal_content_hash"] = "0" * 64
        elif fault == "expired":
            decision["expires_at"] = NOW
        elif fault == "already_consumed":
            decision["consumed"] = True
        elif fault == "wrong_display_projection":
            display = _load(connection, "synthetic_displays", display_id, DECISION_KEY)
            projection = dict(display["projection"])
            proposal_projection = dict(projection["proposal_review"])
            proposal_projection["amount"] = "99.00"
            projection["proposal_review"] = proposal_projection
            display["projection"] = projection
            _replace(connection, "synthetic_displays", display_id, display, DECISION_KEY)
            decision["display_evidence_digest"] = _digest(display)
            reply = _load(connection, "synthetic_replies", str(decision["reply_id"]), DECISION_KEY)
            reply["display_evidence_digest"] = decision["display_evidence_digest"]
            _replace(
                connection, "synthetic_replies", str(decision["reply_id"]), reply, DECISION_KEY
            )
        unsigned_decision = {
            key: value for key, value in decision.items() if key != "decision_digest"
        }
        decision["decision_digest"] = _digest(unsigned_decision)
        _replace(connection, "synthetic_decisions", decision_id, decision, DECISION_KEY)
        connection.commit()

    decision_snapshot = None
    if fault == "bad_decision_signature":
        decision_snapshot = tuple(
            connection.execute(
                "SELECT material,signature FROM synthetic_decisions WHERE id=?", (decision_id,)
            ).fetchone()
        )
    before_changes = connection.total_changes
    with pytest.raises((PostingError, ValueError, sqlite3.IntegrityError)):
        service.submit_post(review.review_id, decision_id)
    assert connection.total_changes == before_changes
    assert _count(connection, "application_posting_reviews") == 1
    assert _count(connection, "application_posting_decisions") == 0
    assert _count(connection, "application_posting_attempts") == 0
    assert _count(connection, "application_posting_events") == 0
    assert _count(connection, "application_posting_receipt_evidence") == 0
    for table in (
        "parser_proposal_authorizations",
        "transactions",
        "receipt_proposal_conversions",
        "receipt_item_allocation_fact_sets",
        "receipt_finalization_authorizations",
        "application_conditional_authorization_proofs",
    ):
        assert _count(connection, table) == 0
    if fault == "missing_decision":
        assert (
            connection.execute(
                "SELECT 1 FROM synthetic_decisions WHERE id=?", (decision_id,)
            ).fetchone()
            is None
        )
    elif fault == "bad_decision_signature":
        row = connection.execute(
            "SELECT material,signature FROM synthetic_decisions WHERE id=?", (decision_id,)
        ).fetchone()
        assert row is not None
        assert tuple(row) == decision_snapshot
        assert row[1] == "0" * 64
        assert json.loads(row[0])["consumed"] is False
    else:
        assert _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is (
            fault == "already_consumed"
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema", "finance-application-other-source-v1"),
        ("namespace", "wrong-source-namespace"),
        ("key_id", "wrong-source-key"),
        ("instance_id", "wrong-source-instance"),
        ("evidence_id", ""),
        ("source_content_hash", "bad-digest"),
        ("evidence_digest", "bad-digest"),
        ("received_at", NOW + 1),
    ],
)
def test_core_rejects_mutated_verified_source_without_consumption(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    from finance_core.application.admission import AdmissionError

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix=f"typed-source-{field}"
    )
    original = service._source_port.verify_persisted
    verified_calls = 0

    def mutate_after_real_verification(owner_connection, intake_id):
        nonlocal verified_calls
        verified_calls += 1
        verified = original(owner_connection, intake_id)
        return dataclasses.replace(verified, **{field: replacement})

    monkeypatch.setattr(service._source_port, "verify_persisted", mutate_after_real_verification)
    before_changes = connection.total_changes
    with pytest.raises(AdmissionError):
        service.submit_post(review.review_id, decision_id)
    assert verified_calls >= 1
    assert connection.total_changes == before_changes
    _assert_counts(
        connection,
        {
            "application_posting_reviews": 1,
            "application_posting_decisions": 0,
            "application_posting_attempts": 0,
            "application_posting_events": 0,
            "application_posting_receipt_evidence": 0,
            "parser_proposal_authorizations": 0,
            "parser_proposal_conversion_audit": 0,
            "transactions": 0,
            "receipt_proposal_conversions": 0,
            "receipt_item_allocation_fact_sets": 0,
            "authoritative_calculation_snapshots": 0,
            "receipt_finalization_authorizations": 0,
            "application_conditional_authorization_proofs": 0,
        },
    )


@pytest.mark.parametrize(
    ("fault", "changed_field", "changed_value"),
    [
        ("display_nonhuman", "origin", "system"),
        ("display_not_private", "private", False),
        ("display_not_direct", "direct", False),
        ("reply_nonhuman", "origin", "system"),
        ("reply_not_private", "private", False),
        ("reply_not_direct", "direct", False),
    ],
)
def test_durable_display_and_reply_semantics_are_refused(
    migrated_temp_db_connection: sqlite3.Connection,
    fault: str,
    changed_field: str,
    changed_value: object,
) -> None:
    from finance_core.application.posting import PostingError

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, display_id = _prepare_text_subject(
        connection, suffix=f"semantic-{fault}"
    )
    decision = _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)
    reply_id = str(decision["reply_id"])
    if fault.startswith("display_"):
        display = _load(connection, "synthetic_displays", display_id, DECISION_KEY)
        display[changed_field] = changed_value
        _replace(connection, "synthetic_displays", display_id, display, DECISION_KEY)
        decision["display_evidence_digest"] = _digest(display)
        unsigned = {key: value for key, value in decision.items() if key != "decision_digest"}
        decision["decision_digest"] = _digest(unsigned)
        _replace(connection, "synthetic_decisions", decision_id, decision, DECISION_KEY)
        reply = _load(connection, "synthetic_replies", reply_id, DECISION_KEY)
        reply["display_evidence_digest"] = decision["display_evidence_digest"]
        _replace(connection, "synthetic_replies", reply_id, reply, DECISION_KEY)
    else:
        reply = _load(connection, "synthetic_replies", reply_id, DECISION_KEY)
        reply[changed_field] = changed_value
        _replace(connection, "synthetic_replies", reply_id, reply, DECISION_KEY)
    connection.commit()

    before_changes = connection.total_changes
    with pytest.raises((PostingError, ValueError)):
        service.submit_post(review.review_id, decision_id)
    assert connection.total_changes == before_changes
    _assert_counts(
        connection,
        {
            "application_posting_reviews": 1,
            "application_posting_decisions": 0,
            "application_posting_attempts": 0,
            "application_posting_events": 0,
            "application_posting_receipt_evidence": 0,
            "parser_proposal_authorizations": 0,
            "parser_proposal_conversion_audit": 0,
            "transactions": 0,
            "receipt_proposal_conversions": 0,
            "receipt_item_allocation_fact_sets": 0,
            "authoritative_calculation_snapshots": 0,
            "receipt_finalization_authorizations": 0,
            "application_conditional_authorization_proofs": 0,
        },
    )


@pytest.mark.parametrize(
    ("fault", "replacement"),
    [
        ("schema", "finance-application-other-human-decision-v1"),
        ("namespace", "wrong-decision-namespace"),
        ("key_id", "wrong-decision-key"),
        ("instance_id", "wrong-service-instance"),
        ("human_principal_id", "wrong-human"),
        ("action", "edit"),
        ("issued_at", NOW + 1),
        ("source_evidence_id", ""),
        ("source_evidence_digest", "bad-digest"),
        ("display_id", ""),
        ("display_evidence_digest", "bad-digest"),
        ("decision_digest", ""),
        ("consumed", True),
        ("review_id", "another-review"),
    ],
)
def test_core_rejects_mutated_verified_posting_decision_without_consumption(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    replacement: object,
) -> None:
    from finance_core.application.posting import PostingError

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix=f"typed-decision-{fault}"
    )
    original = service._decision_port.verify_persisted
    verified_calls = 0

    def mutate_after_real_verification(owner_connection, record_id, expected):
        nonlocal verified_calls
        verified_calls += 1
        verified = original(owner_connection, record_id, expected)
        return dataclasses.replace(verified, **{fault: replacement})

    monkeypatch.setattr(service._decision_port, "verify_persisted", mutate_after_real_verification)
    before_changes = connection.total_changes
    with pytest.raises(PostingError):
        service.submit_post(review.review_id, decision_id)
    assert verified_calls == 1
    assert connection.total_changes == before_changes
    _assert_counts(
        connection,
        {
            "application_posting_reviews": 1,
            "application_posting_decisions": 0,
            "application_posting_attempts": 0,
            "application_posting_events": 0,
            "application_posting_receipt_evidence": 0,
            "parser_proposal_authorizations": 0,
            "parser_proposal_conversion_audit": 0,
            "transactions": 0,
            "receipt_proposal_conversions": 0,
            "receipt_item_allocation_fact_sets": 0,
            "authoritative_calculation_snapshots": 0,
            "receipt_finalization_authorizations": 0,
            "application_conditional_authorization_proofs": 0,
        },
    )


def test_core_rejects_untyped_forged_approval_after_durable_verification(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from finance_core.application.posting import PostingError

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix="untyped-forged-decision"
    )
    original = service._decision_port.verify_persisted
    verified_calls = 0

    def forge_after_real_verification(owner_connection, record_id, expected):
        nonlocal verified_calls
        verified_calls += 1
        original(owner_connection, record_id, expected)
        return SimpleNamespace(approved=True, decision_id=record_id)

    monkeypatch.setattr(service._decision_port, "verify_persisted", forge_after_real_verification)
    before_changes = connection.total_changes
    with pytest.raises(PostingError):
        service.submit_post(review.review_id, decision_id)
    assert verified_calls == 1
    assert connection.total_changes == before_changes
    _assert_counts(
        connection,
        {
            "application_posting_reviews": 1,
            "application_posting_decisions": 0,
            "application_posting_attempts": 0,
            "application_posting_events": 0,
            "application_posting_receipt_evidence": 0,
            "parser_proposal_authorizations": 0,
            "transactions": 0,
        },
    )


def test_completed_status_refuses_historical_typed_return_change_without_writes(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.application.posting import PostingError

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix="historical-typed-change"
    )
    posted = service.submit_post(review.review_id, decision_id)
    assert posted.state == "finalized"
    original = service._decision_port.verify_persisted
    verified_calls = 0

    def change_historical_result(owner_connection, record_id, expected):
        nonlocal verified_calls
        verified_calls += 1
        verified = original(owner_connection, record_id, expected)
        return dataclasses.replace(verified, decision_digest="f" * 64)

    monkeypatch.setattr(service._decision_port, "verify_persisted", change_historical_result)
    expected_counts = {
        "application_posting_decisions": 1,
        "application_posting_attempts": 1,
        "application_posting_events": 2,
        "application_posting_receipt_evidence": 0,
        "parser_proposal_authorizations": 1,
        "transactions": 1,
        "receipt_proposal_conversions": 0,
        "receipt_item_allocation_fact_sets": 0,
    }
    for operation in (service.get_status, service.resume_post):
        before_changes = connection.total_changes
        with pytest.raises(PostingError):
            operation(posted.attempt_id)
        assert connection.total_changes == before_changes
        _assert_counts(connection, expected_counts)
    assert verified_calls == 2


@pytest.mark.parametrize("phase", ["fresh", "historical"])
def test_recomposed_service_with_changed_fixed_binding_refuses_read_or_submit(
    migrated_temp_db_connection: sqlite3.Connection,
    phase: str,
) -> None:
    from finance_core.application.posting import PostingError, PostingService

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix=f"binding-change-{phase}"
    )
    if phase == "historical":
        posted = service.submit_post(review.review_id, decision_id)
        assert posted.state == "finalized"
        attempt_id = posted.attempt_id
    changed_binding = dataclasses.replace(
        BINDING, decision_key_id="synthetic-rotated-posting-decision-key"
    )
    changed_service = PostingService(
        connection=connection,
        source_verifier=_DurableSourceVerifier(),
        human_decision_authority=_DurablePostingDecisionAuthority(),
        binding=changed_binding,
        clock=lambda: NOW + (10_000 if phase == "historical" else 0),
    )
    if phase == "fresh":
        before_changes = connection.total_changes
        with pytest.raises(PostingError):
            changed_service.submit_post(review.review_id, decision_id)
        assert connection.total_changes == before_changes
        _assert_counts(
            connection,
            {
                "application_posting_reviews": 1,
                "application_posting_decisions": 0,
                "application_posting_attempts": 0,
                "application_posting_events": 0,
                "parser_proposal_authorizations": 0,
                "transactions": 0,
            },
        )
    else:
        expected_counts = {
            "application_posting_reviews": 1,
            "application_posting_decisions": 1,
            "application_posting_attempts": 1,
            "application_posting_events": 2,
            "parser_proposal_authorizations": 1,
            "transactions": 1,
        }
        for operation in (changed_service.get_status, changed_service.resume_post):
            before_changes = connection.total_changes
            with pytest.raises(PostingError):
                operation(attempt_id)
            assert connection.total_changes == before_changes
            _assert_counts(connection, expected_counts)


def test_two_signed_competing_approvals_create_one_posting_and_result(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    from queue import Queue

    from finance_core.application.posting import PostingService

    connection = migrated_temp_db_connection
    service, review, proposal, _intake, decision_a, _display_a = _prepare_text_subject(
        connection, suffix="competing-approval"
    )
    _display_b, signed_decision_b = _decision_material(
        review, proposal["public_id"], "synthetic-decision-competing-b"
    )
    decision_b = str(signed_decision_b["decision_id"])
    _persist(
        connection,
        "synthetic_replies",
        str(signed_decision_b["reply_id"]),
        _reply_material(signed_decision_b),
        DECISION_KEY,
    )
    _persist(connection, "synthetic_decisions", decision_b, signed_decision_b, DECISION_KEY)
    connection.commit()
    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    barrier = threading.Barrier(2)
    outcomes: Queue[tuple[str, object]] = Queue()

    def submit(decision_id: str) -> None:
        worker_connection = connect_temp_db(database_path)
        try:
            worker_service = PostingService(
                connection=worker_connection,
                source_verifier=_DurableSourceVerifier(),
                human_decision_authority=_DurablePostingDecisionAuthority(),
                binding=BINDING,
                clock=lambda: NOW,
            )
            barrier.wait(timeout=10)
            try:
                outcomes.put(
                    ("accepted", worker_service.submit_post(review.review_id, decision_id))
                )
            except Exception as exc:  # The competing valid decision must lose cleanly.
                outcomes.put(("refused", exc))
        finally:
            worker_connection.close()

    workers = [
        threading.Thread(target=submit, args=(decision_id,), daemon=True)
        for decision_id in (decision_a, decision_b)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    assert all(not worker.is_alive() for worker in workers)
    observed = [outcomes.get_nowait() for _ in workers]
    accepted = [value for state, value in observed if state == "accepted"]
    refused = [value for state, value in observed if state == "refused"]
    assert len(accepted) == len(refused) == 1
    assert getattr(accepted[0], "state") == "finalized"
    assert _count(connection, "application_posting_decisions") == 1
    assert _count(connection, "application_posting_attempts") == 1
    assert _count(connection, "application_posting_events") == 2
    assert _count(connection, "parser_proposal_authorizations") == 1
    assert _count(connection, "transactions") == 1
    assert _count(connection, "settlement_obligations") == 0
    consumed_id = str(
        connection.execute("SELECT decision_id FROM application_posting_decisions").fetchone()[0]
    )
    assert consumed_id in {decision_a, decision_b}


def test_local_receipt_posts_exact_reviewed_projection_without_facts_before_confirmation(
    tmp_path: Path,
) -> None:
    from finance_core.receipt_finalization.application_conditional import (
        require_application_conditional_authority,
    )

    (
        connection,
        _workspace,
        _manifest,
        service,
        review,
        _proposal,
        _intake,
        decision_id,
        _display_id,
        _source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(tmp_path, "exact-total")
    try:
        approved = dict(review.projection["financial_projection"])
        assert approved == {
            "amount": "12.50",
            "currency": "SGD",
            "transaction_date": "2026-10-08",
            "merchant": "EXAMPLE CAFE",
            "account": "unspecified",
            "receipt_total": "12.50",
            "personal_share": "12.50",
            "calculation": {
                "total_paid": "12.50",
                "total_to_collect": "0.00",
                "settlement_obligations": [],
            },
        }
        assert _count(connection, "application_posting_reviews") == 1
        assert _count(connection, "application_posting_decisions") == 0
        assert _count(connection, "application_posting_attempts") == 0
        assert _count(connection, "application_posting_events") == 0
        assert _count(connection, "application_posting_receipt_evidence") == 0
        for table in (
            "parser_proposal_authorizations",
            "receipt_proposal_conversions",
            "receipts",
            "receipt_item_allocation_fact_sets",
            "authoritative_calculation_snapshots",
            "receipt_finalization_authorizations",
            "application_conditional_authorization_proofs",
            "transactions",
            "settlement_obligations",
        ):
            assert _count(connection, table) == 0

        posted = service.submit_post(review.review_id, decision_id)
        assert posted.state == "finalized"
        assert posted.transaction_public_id
        assert _count(connection, "application_posting_decisions") == 1
        assert _count(connection, "application_posting_attempts") == 1
        assert _count(connection, "application_posting_events") == 6
        assert _count(connection, "application_posting_receipt_evidence") == 4
        assert _count(connection, "parser_proposal_authorizations") == 1
        assert _count(connection, "receipt_proposal_conversions") == 1
        assert _count(connection, "receipts") == 1
        assert _count(connection, "receipt_item_allocation_fact_sets") == 1
        assert _count(connection, "authoritative_calculation_snapshots") == 1
        assert _count(connection, "receipt_finalization_authorizations") == 1
        assert _count(connection, "application_conditional_authorization_proofs") == 1
        assert _count(connection, "transactions") == 1
        assert _count(connection, "settlement_obligations") == 0

        transaction = connection.execute(
            "SELECT amount,currency,transaction_date,merchant FROM transactions WHERE public_id=?",
            (posted.transaction_public_id,),
        ).fetchone()
        assert transaction is not None
        assert (
            f"{Decimal(str(transaction[0])):.2f}",
            str(transaction[1]),
            str(transaction[2]),
            str(transaction[3]),
        ) == (
            approved["amount"],
            approved["currency"],
            approved["transaction_date"],
            approved["merchant"],
        )

        proof = connection.execute(
            "SELECT authorization_id,calculation_snapshot_id FROM "
            "application_conditional_authorization_proofs"
        ).fetchone()
        require_application_conditional_authority(
            connection,
            {
                "authorization_id": proof["authorization_id"],
                "calculation_snapshot_id": proof["calculation_snapshot_id"],
                "actor_id": BINDING.human_principal_id,
            },
        )
        assert (
            _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is False
        )
    finally:
        connection.close()


def test_nfd_receipt_identity_posts_nfc_and_preserves_raw_evidence_hash(
    tmp_path: Path,
) -> None:
    from finance_core.calculation.authoritative_snapshot import canonical_json_value

    merchant_nfd = unicodedata.normalize("NFD", "CAFÉ MARKET")
    merchant_nfc = unicodedata.normalize("NFC", merchant_nfd)
    (
        connection,
        _workspace,
        _manifest,
        service,
        review,
        _proposal,
        intake_public_id,
        decision_id,
        _display_id,
        _source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(
        tmp_path,
        "unicode-receipt",
        merchant_text=merchant_nfd,
    )
    try:
        assert review.projection["financial_projection"]["merchant"] == merchant_nfc
        raw = connection.execute(
            "SELECT raw_input,source_content_hash,attachment_hash FROM raw_intake_records "
            "WHERE public_id=?",
            (intake_public_id,),
        ).fetchone()
        assert raw is not None
        assert raw["raw_input"] == "local receipt image: receipt.jpg"
        raw_digest = hashlib.sha256(raw["raw_input"].encode("utf-8")).hexdigest()
        assert raw["source_content_hash"] == f"sha256:{raw_digest}"
        original_image = tmp_path / "external-unicode-receipt" / "receipt.jpg"
        image_digest = hashlib.sha256(original_image.read_bytes()).hexdigest()
        assert raw["attachment_hash"] == image_digest
        local_source = connection.execute(
            "SELECT source.content_hash FROM local_attachment_source AS source "
            "JOIN raw_intake_records AS intake ON intake.attachment_id=source.attachment_id "
            "WHERE intake.public_id=?",
            (intake_public_id,),
        ).fetchone()
        assert local_source is not None
        assert local_source["content_hash"] == image_digest
        source = _load(
            connection,
            "synthetic_sources",
            "synthetic-source-unicode-receipt",
            SOURCE_KEY,
        )
        assert source["source_content_hash"] == raw_digest

        posted = service.submit_post(review.review_id, decision_id)
        assert posted.state == "finalized"
        transaction = connection.execute(
            "SELECT merchant FROM transactions WHERE public_id=?",
            (posted.transaction_public_id,),
        ).fetchone()
        assert transaction is not None
        assert transaction[0] == merchant_nfc

        snapshot_id = connection.execute(
            "SELECT evidence_public_id FROM application_posting_receipt_evidence "
            "WHERE evidence_type='snapshot'"
        ).fetchone()[0]
        snapshot = connection.execute(
            "SELECT input_payload_json FROM authoritative_calculation_snapshots "
            "WHERE snapshot_public_id=?",
            (snapshot_id,),
        ).fetchone()
        assert snapshot is not None
        snapshot_input = canonical_json_value(snapshot["input_payload_json"], label="input")
        assert snapshot_input["confirmed_receipt_identity"]["merchant"] == merchant_nfc
    finally:
        connection.close()


def test_local_receipt_source_drift_inside_conversion_owner_rolls_back_receipt_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.application import posting

    (
        connection,
        _workspace,
        _manifest,
        service,
        review,
        _proposal,
        _intake,
        decision_id,
        _display_id,
        _source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(tmp_path, "source-race")
    original_convert = posting.convert_confirmed_receipt_proposal_to_facts
    drift_reached_owner = False

    def race_with_signed_source_drift(
        owner_connection,
        command,
        *,
        clock=None,
        metadata_authority=None,
        persistence_effect=None,
    ):
        def replace_source_before_application_effect(inner_connection, result):
            nonlocal drift_reached_owner
            source_id = "synthetic-source-source-race"
            source = _load(inner_connection, "synthetic_sources", source_id, SOURCE_KEY)
            source["source_event_id"] = "synthetic-event-changed-at-owner-boundary"
            unsigned = {key: value for key, value in source.items() if key != "evidence_digest"}
            source["evidence_digest"] = _digest(unsigned)
            _replace(inner_connection, "synthetic_sources", source_id, source, SOURCE_KEY)
            drift_reached_owner = True
            assert persistence_effect is not None
            persistence_effect(inner_connection, result)

        return original_convert(
            owner_connection,
            command,
            clock=clock,
            metadata_authority=metadata_authority,
            persistence_effect=replace_source_before_application_effect,
        )

    monkeypatch.setattr(
        posting,
        "convert_confirmed_receipt_proposal_to_facts",
        race_with_signed_source_drift,
    )
    try:
        with pytest.raises(posting.PostingError, match="source evidence changed"):
            service.submit_post(review.review_id, decision_id)
        assert drift_reached_owner is True
        # The source mutation and every receipt fact were in the real owner
        # transaction; the rejected persistence callback rolled them all back.
        source = _load(connection, "synthetic_sources", "synthetic-source-source-race", SOURCE_KEY)
        assert source["source_event_id"] == "synthetic-event-source-race"
        assert _count(connection, "application_posting_decisions") == 1
        assert _count(connection, "application_posting_attempts") == 1
        assert _count(connection, "application_posting_events") == 1
        assert _count(connection, "parser_proposal_authorizations") == 1
        assert _count(connection, "application_posting_receipt_evidence") == 0
        for table in (
            "receipt_proposal_conversions",
            "receipts",
            "receipt_item_allocation_fact_sets",
            "authoritative_calculation_snapshots",
            "receipt_finalization_authorizations",
            "application_conditional_authorization_proofs",
            "transactions",
            "settlement_obligations",
        ):
            assert _count(connection, table) == 0
        assert (
            _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is False
        )
    finally:
        connection.close()


@pytest.mark.parametrize("drift", ["signed_source", "signed_decision", "payer"])
def test_snapshot_owner_lock_revalidates_signed_authority_and_payer_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    from finance_core.application.posting import PostingError
    from finance_core.receipt_finalization import fact_set_bridge

    (
        connection,
        _workspace,
        _manifest,
        service,
        review,
        _proposal,
        _intake,
        decision_id,
        _display_id,
        _source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(tmp_path, f"snapshot-lock-{drift}")
    original_prepare = fact_set_bridge.prepare_receipt_calculation
    mutation_reached_owner = False

    def run_with_owner_lock_mutation(
        owner_connection,
        receipt_public_id,
        *,
        actor_type="system",
        actor_id=None,
        clock=None,
        persistence_effect=None,
    ):
        def mutate_before_revalidation(inner_connection, snapshot):
            nonlocal mutation_reached_owner
            assert inner_connection is connection
            assert inner_connection.in_transaction
            mutation_reached_owner = True
            if drift == "signed_source":
                source_id = f"synthetic-source-snapshot-lock-{drift}"
                source = _load(inner_connection, "synthetic_sources", source_id, SOURCE_KEY)
                source["source_event_id"] = "synthetic-event-drifted-under-snapshot-lock"
                unsigned = {key: value for key, value in source.items() if key != "evidence_digest"}
                source["evidence_digest"] = _digest(unsigned)
                _replace(inner_connection, "synthetic_sources", source_id, source, SOURCE_KEY)
            elif drift == "signed_decision":
                decision = _load(inner_connection, "synthetic_decisions", decision_id, DECISION_KEY)
                decision["issued_at"] = NOW - 3
                unsigned = {
                    key: value for key, value in decision.items() if key != "decision_digest"
                }
                decision["decision_digest"] = _digest(unsigned)
                _replace(
                    inner_connection,
                    "synthetic_decisions",
                    decision_id,
                    decision,
                    DECISION_KEY,
                )
            else:
                inner_connection.execute(
                    "UPDATE participants SET is_active=0 WHERE public_id='ptcp_posting_self'"
                )
            assert persistence_effect is not None
            persistence_effect(inner_connection, snapshot)

        return original_prepare(
            owner_connection,
            receipt_public_id,
            actor_type=actor_type,
            actor_id=actor_id,
            clock=clock,
            persistence_effect=mutate_before_revalidation,
        )

    monkeypatch.setattr(
        fact_set_bridge, "prepare_receipt_calculation", run_with_owner_lock_mutation
    )
    try:
        with pytest.raises((PostingError, ValueError)):
            service.submit_post(review.review_id, decision_id)
        assert mutation_reached_owner
        assert not connection.in_transaction
        receipt_id = str(connection.execute("SELECT public_id FROM receipts").fetchone()[0])
        assert (
            connection.execute(
                "SELECT is_active FROM participants WHERE public_id='ptcp_posting_self'"
            ).fetchone()[0]
            == 1
        )
        source = _load(
            connection,
            "synthetic_sources",
            f"synthetic-source-snapshot-lock-{drift}",
            SOURCE_KEY,
        )
        assert source["source_event_id"] == f"synthetic-event-snapshot-lock-{drift}"
        decision = _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)
        assert decision["issued_at"] == NOW - 1
        _assert_counts(
            connection,
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 3,
                "application_posting_receipt_evidence": 2,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 1,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
                "settlement_obligations": 0,
            },
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM calc_audit_runs WHERE entity_type='receipt' AND entity_id=?",
                (receipt_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM receipt_fact_set_binding_evidence "
                "WHERE receipt_public_id=? AND bound_record_type IN "
                "('calculation_run','calculation_snapshot')",
                (receipt_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("fault_stage", "attempt_stage", "pre_counts"),
    [
        (
            "before_conversion_commit",
            "accepted",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 1,
                "application_posting_receipt_evidence": 0,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 0,
                "receipts": 0,
                "receipt_item_allocation_fact_sets": 0,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
            },
        ),
        (
            "before_fact_set_commit",
            "conversion_persisted",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 2,
                "application_posting_receipt_evidence": 1,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 0,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
            },
        ),
        (
            "before_snapshot_commit",
            "fact_set_persisted",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 3,
                "application_posting_receipt_evidence": 2,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 1,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
            },
        ),
        (
            "before_conditional_authorization_commit",
            "snapshot_persisted",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 4,
                "application_posting_receipt_evidence": 3,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 1,
                "authoritative_calculation_snapshots": 1,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
            },
        ),
        (
            "before_receipt_finalization_commit",
            "conditional_authorization_persisted",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 5,
                "application_posting_receipt_evidence": 4,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 1,
                "authoritative_calculation_snapshots": 1,
                "receipt_finalization_authorizations": 1,
                "application_conditional_authorization_proofs": 1,
                "transactions": 0,
            },
        ),
    ],
)
def test_receipt_before_owner_commits_rollback_and_recover_same_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
    attempt_stage: str,
    pre_counts: dict[str, int],
) -> None:
    from finance_core.application import posting
    from finance_core.application.posting import PostingService

    (
        connection,
        workspace,
        manifest,
        service,
        review,
        _proposal,
        _intake,
        decision_id,
        _display_id,
        source_verifier,
        decision_authority,
    ) = _prepare_receipt_subject(tmp_path, f"before-owner-{fault_stage}")

    def fail_before_owner_commit(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"injected {fault_stage}")

    monkeypatch.setattr(posting, "_failure_injection_hook", fail_before_owner_commit)
    try:
        with pytest.raises(RuntimeError, match=fault_stage):
            service.submit_post(review.review_id, decision_id)
        assert not connection.in_transaction
        _assert_counts(connection, pre_counts)
        attempt_id = str(
            connection.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
        )
        assert (
            connection.execute(
                "SELECT stage FROM application_posting_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()[0]
            == attempt_stage
        )
        assert (
            _load(connection, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is False
        )

        if fault_stage == "before_snapshot_commit":
            receipt_id = str(connection.execute("SELECT public_id FROM receipts").fetchone()[0])
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM calc_audit_runs "
                    "WHERE entity_type='receipt' AND entity_id=?",
                    (receipt_id,),
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM receipt_fact_set_binding_evidence "
                    "WHERE receipt_public_id=? AND bound_record_type IN "
                    "('calculation_run','calculation_snapshot')",
                    (receipt_id,),
                ).fetchone()[0]
                == 0
            )

        database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
        connection.close()
        reopened = connect_temp_db(database_path)
        try:
            monkeypatch.setattr(posting, "_failure_injection_hook", None)
            recovery_service = PostingService(
                connection=reopened,
                source_verifier=type(source_verifier)(workspace=workspace, manifest=manifest),
                human_decision_authority=type(decision_authority)(),
                binding=BINDING,
                clock=lambda: NOW + 10_000,
            )
            recovered = recovery_service.resume_post(attempt_id)
            repeated = recovery_service.resume_post(attempt_id)
            status = recovery_service.get_status(attempt_id)
            assert recovered.state == repeated.state == status.state == "finalized"
            assert recovered.transaction_public_id
            assert repeated.transaction_public_id == status.transaction_public_id
            assert recovered.transaction_public_id == status.transaction_public_id
            assert (
                _load(reopened, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"]
                is False
            )
            _assert_counts(
                reopened,
                {
                    "application_posting_decisions": 1,
                    "application_posting_attempts": 1,
                    "application_posting_events": 6,
                    "application_posting_receipt_evidence": 4,
                    "parser_proposal_authorizations": 1,
                    "receipt_proposal_conversions": 1,
                    "receipts": 1,
                    "receipt_item_allocation_fact_sets": 1,
                    "authoritative_calculation_snapshots": 1,
                    "receipt_finalization_authorizations": 1,
                    "application_conditional_authorization_proofs": 1,
                    "transactions": 1,
                    "settlement_obligations": 0,
                },
            )
            assert (
                reopened.execute(
                    "SELECT stage FROM application_posting_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()[0]
                == "finalized"
            )
        finally:
            reopened.close()
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass


@pytest.mark.parametrize("missing_proof", ["conditional_authorization", "finalization_idempotency"])
def test_finalized_receipt_missing_proof_refuses_status_and_resume(
    tmp_path: Path,
    missing_proof: str,
) -> None:

    (
        connection,
        _workspace,
        _manifest,
        service,
        review,
        _proposal,
        _intake,
        decision_id,
        _display_id,
        _source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(tmp_path, f"missing-final-proof-{missing_proof}")
    try:
        posted = service.submit_post(review.review_id, decision_id)
        assert posted.state == "finalized"
        if missing_proof == "conditional_authorization":
            _delete_durable_proof(
                connection,
                "application_conditional_authorization_proofs",
                "attempt_id=?",
                (posted.attempt_id,),
                trigger_names=("application_conditional_proofs_no_delete",),
            )
        else:
            _delete_durable_proof(
                connection,
                "receipt_finalization_idempotency",
                "1=1",
                (),
                trigger_names=("trg_receipt_finalization_idempotency_no_delete",),
            )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='application_conditional_proofs_no_delete'"
            ).fetchone()
            is not None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_receipt_finalization_idempotency_no_delete'"
            ).fetchone()
            is not None
        )
        expected_counts = {
            "application_posting_decisions": 1,
            "application_posting_attempts": 1,
            "application_posting_events": 6,
            "application_posting_receipt_evidence": 4,
            "parser_proposal_authorizations": 1,
            "receipt_proposal_conversions": 1,
            "receipts": 1,
            "receipt_item_allocation_fact_sets": 1,
            "authoritative_calculation_snapshots": 1,
            "receipt_finalization_authorizations": 1,
            "application_conditional_authorization_proofs": (
                0 if missing_proof == "conditional_authorization" else 1
            ),
            "receipt_finalization_audit": 1,
            "receipt_finalization_idempotency": (
                0 if missing_proof == "finalization_idempotency" else 1
            ),
            "transactions": 1,
            "settlement_obligations": 0,
        }
        _assert_read_refusal_is_write_free(service, connection, posted.attempt_id, expected_counts)
        assert tuple(
            connection.execute(
                "SELECT stage,transaction_public_id FROM application_posting_attempts "
                "WHERE attempt_id=?",
                (posted.attempt_id,),
            ).fetchone()
        ) == ("finalized", posted.transaction_public_id)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("missing_owner", "fault_stage", "attempt_stage"),
    [
        ("conversion", "before_fact_set_commit", "conversion_persisted"),
        ("snapshot", "before_conditional_authorization_commit", "snapshot_persisted"),
        (
            "conditional_authorization",
            "before_receipt_finalization_commit",
            "conditional_authorization_persisted",
        ),
    ],
)
def test_pending_receipt_stage_missing_owner_proof_refuses_read_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_owner: str,
    fault_stage: str,
    attempt_stage: str,
) -> None:
    from finance_core.application import posting

    (
        connection,
        _workspace,
        _manifest,
        service,
        review,
        _proposal,
        _intake,
        decision_id,
        _display_id,
        _source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(tmp_path, f"pending-missing-{missing_owner}")

    def fail_at_owner_boundary(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"injected {fault_stage}")

    monkeypatch.setattr(posting, "_failure_injection_hook", fail_at_owner_boundary)
    try:
        with pytest.raises(RuntimeError, match=fault_stage):
            service.submit_post(review.review_id, decision_id)
        assert not connection.in_transaction
        attempt_id = str(
            connection.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
        )
        assert (
            connection.execute(
                "SELECT stage FROM application_posting_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()[0]
            == attempt_stage
        )

        if missing_owner == "conversion":
            command_id = str(
                connection.execute(
                    "SELECT command_public_id FROM receipt_proposal_conversions"
                ).fetchone()[0]
            )
            _delete_durable_proof(
                connection,
                "receipt_proposal_conversions",
                "command_public_id=?",
                (command_id,),
                trigger_names=("trg_receipt_proposal_conversions_no_delete",),
            )
        elif missing_owner == "snapshot":
            receipt_id = str(connection.execute("SELECT public_id FROM receipts").fetchone()[0])
            run = connection.execute(
                "SELECT run_id FROM calc_audit_runs WHERE entity_type='receipt' AND entity_id=?",
                (receipt_id,),
            ).fetchone()
            snapshot_id = str(
                connection.execute(
                    "SELECT snapshot_public_id FROM authoritative_calculation_snapshots"
                ).fetchone()[0]
            )
            assert run is not None
            _delete_durable_proof(
                connection,
                "receipt_fact_set_binding_evidence",
                "receipt_public_id=? AND bound_record_type IN "
                "('calculation_run','calculation_snapshot')",
                (receipt_id,),
                trigger_names=("trg_fact_set_binding_evidence_no_delete",),
            )
            connection.execute("DELETE FROM calc_audit_runs WHERE run_id=?", (run["run_id"],))
            connection.commit()
            _delete_durable_proof(
                connection,
                "authoritative_calculation_snapshots",
                "snapshot_public_id=?",
                (snapshot_id,),
                trigger_names=("trg_authoritative_snapshots_no_delete",),
            )
        else:
            _delete_durable_proof(
                connection,
                "application_conditional_authorization_proofs",
                "attempt_id=?",
                (attempt_id,),
                trigger_names=("application_conditional_proofs_no_delete",),
            )

        assert (
            connection.execute(
                "SELECT stage FROM application_posting_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()[0]
            == attempt_stage
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_authoritative_snapshots_no_delete'"
            ).fetchone()
            is not None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_fact_set_binding_evidence_no_delete'"
            ).fetchone()
            is not None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='application_conditional_proofs_no_delete'"
            ).fetchone()
            is not None
        )
        expected_counts = {
            "application_posting_decisions": 1,
            "application_posting_attempts": 1,
            "application_posting_events": {
                "conversion": 2,
                "snapshot": 4,
                "conditional_authorization": 5,
            }[missing_owner],
            "application_posting_receipt_evidence": {
                "conversion": 1,
                "snapshot": 3,
                "conditional_authorization": 4,
            }[missing_owner],
            "parser_proposal_authorizations": 1,
            "receipt_proposal_conversions": 0 if missing_owner == "conversion" else 1,
            "receipts": 1,
            "receipt_item_allocation_fact_sets": 0 if missing_owner == "conversion" else 1,
            "authoritative_calculation_snapshots": (
                1 if missing_owner == "conditional_authorization" else 0
            ),
            "receipt_finalization_authorizations": 0
            if missing_owner != "conditional_authorization"
            else 1,
            "application_conditional_authorization_proofs": 0
            if missing_owner == "conditional_authorization"
            else 0,
            "transactions": 0,
            "settlement_obligations": 0,
        }
        _assert_read_refusal_is_write_free(service, connection, attempt_id, expected_counts)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("fault_stage", "pre_counts", "pre_attempt_stage"),
    [
        (
            "before_acceptance_commit",
            {
                "application_posting_decisions": 0,
                "application_posting_attempts": 0,
                "application_posting_events": 0,
                "application_posting_receipt_evidence": 0,
                "parser_proposal_authorizations": 0,
                "receipt_proposal_conversions": 0,
                "receipts": 0,
                "receipt_item_allocation_fact_sets": 0,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
            },
            None,
        ),
        (
            "after_acceptance_commit",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 1,
                "application_posting_receipt_evidence": 0,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 0,
                "receipts": 0,
                "receipt_item_allocation_fact_sets": 0,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
            },
            "accepted",
        ),
        (
            "after_conversion_commit",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 1,
                "application_posting_receipt_evidence": 1,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 0,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
            },
            "accepted",
        ),
        (
            "after_fact_set_commit",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 2,
                "application_posting_receipt_evidence": 2,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 1,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
            },
            "conversion_persisted",
        ),
        (
            "after_snapshot_commit",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 3,
                "application_posting_receipt_evidence": 3,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 1,
                "authoritative_calculation_snapshots": 1,
                "receipt_finalization_authorizations": 0,
                "application_conditional_authorization_proofs": 0,
                "transactions": 0,
            },
            "fact_set_persisted",
        ),
        (
            "after_conditional_authorization_commit",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 4,
                "application_posting_receipt_evidence": 4,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 1,
                "authoritative_calculation_snapshots": 1,
                "receipt_finalization_authorizations": 1,
                "application_conditional_authorization_proofs": 1,
                "transactions": 0,
            },
            "snapshot_persisted",
        ),
        (
            "after_receipt_finalization_commit",
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 5,
                "application_posting_receipt_evidence": 4,
                "parser_proposal_authorizations": 1,
                "receipt_proposal_conversions": 1,
                "receipts": 1,
                "receipt_item_allocation_fact_sets": 1,
                "authoritative_calculation_snapshots": 1,
                "receipt_finalization_authorizations": 1,
                "application_conditional_authorization_proofs": 1,
                "transactions": 1,
            },
            "conditional_authorization_persisted",
        ),
    ],
)
def test_receipt_commit_boundaries_recover_same_durable_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
    pre_counts: dict[str, int],
    pre_attempt_stage: str | None,
) -> None:
    from finance_core.application import posting
    from finance_core.application.posting import PostingService

    (
        connection,
        workspace,
        manifest,
        service,
        review,
        _proposal,
        _intake,
        decision_id,
        _display_id,
        _source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(tmp_path, f"fault-{fault_stage}")

    def fail_after_commit_boundary(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"injected {fault_stage}")

    monkeypatch.setattr(posting, "_failure_injection_hook", fail_after_commit_boundary)
    try:
        with pytest.raises(RuntimeError, match=fault_stage):
            service.submit_post(review.review_id, decision_id)
        assert not connection.in_transaction
        _assert_counts(connection, pre_counts)
        if pre_attempt_stage is not None:
            assert (
                connection.execute("SELECT stage FROM application_posting_attempts").fetchone()[0]
                == pre_attempt_stage
            )

        database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
        connection.close()
        reopened = connect_temp_db(database_path)
        try:
            monkeypatch.setattr(posting, "_failure_injection_hook", None)
            recovery_service = PostingService(
                connection=reopened,
                source_verifier=type(_source_verifier)(workspace=workspace, manifest=manifest),
                human_decision_authority=type(_decision_authority)(),
                binding=BINDING,
                # The pre-acceptance decision is still fresh; every accepted
                # attempt recovers against immutable accepted_at after expiry.
                clock=lambda: NOW if pre_attempt_stage is None else NOW + 10_000,
            )
            if pre_attempt_stage is None:
                recovered = recovery_service.submit_post(review.review_id, decision_id)
                attempt_id = recovered.attempt_id
            else:
                attempt_id = str(
                    reopened.execute(
                        "SELECT attempt_id FROM application_posting_attempts"
                    ).fetchone()[0]
                )
                recovered = recovery_service.resume_post(attempt_id)
            assert recovered.state == "finalized"
            assert recovered.transaction_public_id
            repeated = recovery_service.resume_post(attempt_id)
            status = recovery_service.get_status(attempt_id)
            assert repeated.state == status.state == "finalized"
            assert repeated.transaction_public_id == status.transaction_public_id
            assert repeated.transaction_public_id == recovered.transaction_public_id
            _assert_counts(
                reopened,
                {
                    "application_posting_decisions": 1,
                    "application_posting_attempts": 1,
                    "application_posting_events": 6,
                    "application_posting_receipt_evidence": 4,
                    "parser_proposal_authorizations": 1,
                    "receipt_proposal_conversions": 1,
                    "receipts": 1,
                    "receipt_item_allocation_fact_sets": 1,
                    "authoritative_calculation_snapshots": 1,
                    "receipt_finalization_authorizations": 1,
                    "application_conditional_authorization_proofs": 1,
                    "transactions": 1,
                    "settlement_obligations": 0,
                },
            )
            finalized_attempt = reopened.execute(
                "SELECT stage,transaction_public_id FROM application_posting_attempts "
                "WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            assert tuple(finalized_attempt) == ("finalized", recovered.transaction_public_id)
            assert (
                _load(reopened, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"]
                is False
            )
        finally:
            reopened.close()
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass


def test_receipt_final_commit_return_loss_recovers_after_self_authority_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.application import posting
    from finance_core.application.posting import PostingService

    (
        connection,
        workspace,
        manifest,
        service,
        review,
        _proposal,
        _intake,
        decision_id,
        _display_id,
        _source_verifier,
        _decision_authority,
    ) = _prepare_receipt_subject(tmp_path, "receipt-final-lost")

    def lose_finalized_return(stage: str) -> None:
        if stage == "after_receipt_finalization_commit":
            raise RuntimeError("injected receipt finalized return loss")

    monkeypatch.setattr(posting, "_failure_injection_hook", lose_finalized_return)
    try:
        with pytest.raises(RuntimeError, match="receipt finalized return loss"):
            service.submit_post(review.review_id, decision_id)
        assert _count(connection, "transactions") == 1
        assert _count(connection, "application_posting_decisions") == 1
        assert _count(connection, "application_posting_receipt_evidence") == 4
        attempt_id = str(
            connection.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
        )
        transaction_id = str(connection.execute("SELECT public_id FROM transactions").fetchone()[0])
        connection.execute(
            "UPDATE participants SET is_active=0 WHERE public_id='ptcp_posting_self'"
        )
        connection.commit()
        database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
        connection.close()

        reopened = connect_temp_db(database_path)
        try:
            monkeypatch.setattr(posting, "_failure_injection_hook", None)
            recovery_service = PostingService(
                connection=reopened,
                source_verifier=type(_source_verifier)(workspace=workspace, manifest=manifest),
                human_decision_authority=type(_decision_authority)(),
                binding=BINDING,
                clock=lambda: NOW + 10_000,
            )
            recovered = recovery_service.resume_post(attempt_id)
            assert recovered.state == "finalized"
            assert recovered.transaction_public_id == transaction_id
            assert _count(reopened, "transactions") == 1
            assert _count(reopened, "application_posting_decisions") == 1
            assert _count(reopened, "application_posting_attempts") == 1
            assert _count(reopened, "application_posting_events") == 6
            assert _count(reopened, "application_posting_receipt_evidence") == 4
            assert _count(reopened, "receipt_proposal_conversions") == 1
            assert _count(reopened, "receipt_item_allocation_fact_sets") == 1
            assert _count(reopened, "authoritative_calculation_snapshots") == 1
            assert _count(reopened, "receipt_finalization_authorizations") == 1
            assert _count(reopened, "application_conditional_authorization_proofs") == 1
            assert (
                _load(reopened, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"]
                is False
            )
        finally:
            reopened.close()
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def test_text_before_conversion_owner_commit_rolls_back_then_recovers_same_attempt(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.application import posting
    from finance_core.application.posting import PostingService

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix="text-before-conversion-commit"
    )

    def fail_before_conversion_commit(stage: str) -> None:
        if stage == "before_text_conversion_commit":
            raise RuntimeError("injected before_text_conversion_commit")

    monkeypatch.setattr(posting, "_failure_injection_hook", fail_before_conversion_commit)
    with pytest.raises(RuntimeError, match="before_text_conversion_commit"):
        service.submit_post(review.review_id, decision_id)
    _assert_counts(
        connection,
        {
            "application_posting_decisions": 1,
            "application_posting_attempts": 1,
            "application_posting_events": 1,
            "application_posting_receipt_evidence": 0,
            "parser_proposal_authorizations": 1,
            "parser_proposal_conversion_audit": 0,
            "transactions": 0,
            "receipt_proposal_conversions": 0,
            "receipt_item_allocation_fact_sets": 0,
            "authoritative_calculation_snapshots": 0,
            "receipt_finalization_authorizations": 0,
        },
    )
    attempt_id = str(
        connection.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
    )
    assert (
        connection.execute(
            "SELECT stage FROM application_posting_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
        == "accepted"
    )
    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    connection.close()

    reopened = connect_temp_db(database_path)
    try:
        monkeypatch.setattr(posting, "_failure_injection_hook", None)
        recovered_service = PostingService(
            connection=reopened,
            source_verifier=_DurableSourceVerifier(),
            human_decision_authority=_DurablePostingDecisionAuthority(),
            binding=BINDING,
            clock=lambda: NOW + 10_000,
        )
        recovered = recovered_service.resume_post(attempt_id)
        assert recovered.state == "finalized"
        repeated = recovered_service.resume_post(attempt_id)
        status = recovered_service.get_status(attempt_id)
        assert repeated.state == status.state == "finalized"
        assert recovered.transaction_public_id == repeated.transaction_public_id
        assert repeated.transaction_public_id == status.transaction_public_id
        assert (
            _load(reopened, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is False
        )
        _assert_counts(
            reopened,
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 2,
                "application_posting_receipt_evidence": 0,
                "parser_proposal_authorizations": 1,
                "parser_proposal_conversion_audit": 1,
                "transactions": 1,
                "receipt_proposal_conversions": 0,
                "receipt_item_allocation_fact_sets": 0,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
            },
        )
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "fault_stage", ["after_text_conversion_commit", "before_status_catchup_commit"]
)
def test_text_final_commit_and_status_catchup_loss_recover_same_result(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    from finance_core.application import posting
    from finance_core.application.posting import PostingService

    connection = migrated_temp_db_connection
    service, review, _proposal, _intake, decision_id, _display_id = _prepare_text_subject(
        connection, suffix=f"text-final-{fault_stage}"
    )

    def fail_after_final_commit(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"injected {fault_stage}")

    monkeypatch.setattr(posting, "_failure_injection_hook", fail_after_final_commit)
    with pytest.raises(RuntimeError, match=fault_stage):
        service.submit_post(review.review_id, decision_id)
    _assert_counts(
        connection,
        {
            "application_posting_decisions": 1,
            "application_posting_attempts": 1,
            "application_posting_events": 1,
            "parser_proposal_authorizations": 1,
            "transactions": 1,
            "receipt_proposal_conversions": 0,
            "receipt_item_allocation_fact_sets": 0,
            "authoritative_calculation_snapshots": 0,
            "receipt_finalization_authorizations": 0,
        },
    )
    attempt_id = str(
        connection.execute("SELECT attempt_id FROM application_posting_attempts").fetchone()[0]
    )
    durable_transaction = str(
        connection.execute("SELECT public_id FROM transactions").fetchone()[0]
    )
    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    connection.close()
    reopened = connect_temp_db(database_path)
    try:
        monkeypatch.setattr(posting, "_failure_injection_hook", None)
        recovery_service = PostingService(
            connection=reopened,
            source_verifier=_DurableSourceVerifier(),
            human_decision_authority=_DurablePostingDecisionAuthority(),
            binding=BINDING,
            clock=lambda: NOW + 10_000,
        )
        recovered = recovery_service.resume_post(attempt_id)
        repeated = recovery_service.resume_post(attempt_id)
        status = recovery_service.get_status(attempt_id)
        assert recovered.state == repeated.state == status.state == "finalized"
        assert recovered.transaction_public_id == durable_transaction
        assert repeated.transaction_public_id == status.transaction_public_id == durable_transaction
        _assert_counts(
            reopened,
            {
                "application_posting_decisions": 1,
                "application_posting_attempts": 1,
                "application_posting_events": 2,
                "parser_proposal_authorizations": 1,
                "transactions": 1,
                "receipt_proposal_conversions": 0,
                "receipt_item_allocation_fact_sets": 0,
                "authoritative_calculation_snapshots": 0,
                "receipt_finalization_authorizations": 0,
            },
        )
        assert (
            _load(reopened, "synthetic_decisions", decision_id, DECISION_KEY)["consumed"] is False
        )
    finally:
        reopened.close()


def test_neutral_posting_import_is_cold_and_platform_imports_are_blocked(
    tmp_path: Path,
) -> None:
    connection, *_ = _prepare_receipt_subject(tmp_path, "cold-posting-import")
    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    connection.close()
    script = """
import importlib
import importlib.abc
import sqlite3
import sys
from scripts.check_application_dependencies import is_platform

class BlockPlatform(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if is_platform(fullname):
            raise ImportError('Platform import blocked: ' + fullname)

sys.meta_path.insert(0, BlockPlatform())
connection = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
before = connection.serialize()
posting = importlib.import_module('finance_core.application.posting')
assert posting.PostingService
assert connection.total_changes == 0 and connection.serialize() == before
assert not any(is_platform(name) for name in sys.modules)
connection.close()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(database_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
