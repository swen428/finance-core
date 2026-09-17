"""D1 human-draft migration and repository tests on temporary databases only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
    migration_ledger_rows,
)

TABLES_047 = {
    "parser_human_drafts",
    "parser_human_draft_reply_evidence",
    "parser_human_draft_operations",
    "parser_human_draft_cards",
    "parser_human_draft_card_delivery_attempts",
    "parser_human_draft_card_delivery_outcomes",
    "parser_human_draft_publications",
    "parser_human_draft_action_bindings",
}


def _assert_insert_or_replace_refused(conn: sqlite3.Connection, tables: tuple[str, ...]) -> None:
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    for table in tables:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0
        columns = [
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_xinfo('{table}')")
            if row["hidden"] == 0
        ]
        projection = ", ".join(f'"{column}"' for column in columns)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f'INSERT OR REPLACE INTO "{table}" ({projection}) '
                f'SELECT {projection} FROM "{table}"'
            )
        conn.rollback()


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_migration_047_contract() -> None:
    """Migration 047 is additive, strict, replayable, and preserves immutable evidence."""
    fresh = _connection()
    upgraded = _connection()
    try:
        apply_migration_paths(fresh, TEMP_DB_MIGRATION_PATHS[:47])
        assert migration_ledger_rows(fresh)[-1]["migration_id"] == "047"
        objects = {
            row["name"]
            for row in fresh.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert TABLES_047 <= objects
        insert_collision_guards = {
            "trg_parser_human_drafts_no_insert_collision",
            "trg_parser_human_draft_reply_evidence_no_insert_collision",
            "trg_parser_human_draft_operations_no_insert_collision",
            "trg_parser_human_draft_cards_no_insert_collision",
            "trg_parser_human_draft_delivery_attempts_no_insert_collision",
            "trg_parser_human_draft_delivery_outcomes_no_insert_collision",
            "trg_parser_human_draft_publications_no_insert_collision",
            "trg_parser_human_draft_action_bindings_no_insert_collision",
        }
        assert insert_collision_guards <= {
            row["name"]
            for row in fresh.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        for table in TABLES_047:
            sql = fresh.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()["sql"]
            assert sql.rstrip().endswith("STRICT")

        apply_migration_paths(upgraded, TEMP_DB_MIGRATION_PATHS[:46])
        before = [tuple(row) for row in migration_ledger_rows(upgraded)]
        apply_migration_paths(upgraded, TEMP_DB_MIGRATION_PATHS[:47])
        assert [tuple(row) for row in migration_ledger_rows(upgraded)[:46]] == before
        rows_047 = [tuple(row) for row in migration_ledger_rows(upgraded)]
        apply_migration_paths(upgraded, TEMP_DB_MIGRATION_PATHS[:47])
        assert [tuple(row) for row in migration_ledger_rows(upgraded)] == rows_047
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []

        # A real reply-evidence row must retain exact bytes and cannot be rewritten.
        upgraded.execute(
            "INSERT INTO parser_outputs (public_id, source_type, parsed_payload) "
            "VALUES ('prop_m47', 'text', '{}')"
        )
        parser_output_id = upgraded.execute(
            "SELECT id FROM parser_outputs WHERE public_id = 'prop_m47'"
        ).fetchone()[0]
        upgraded.execute(
            """
            INSERT INTO openclaw_human_action_references (
                reference_public_id, reference_sha256, issuance_idempotency_key,
                parser_output_id, action, proposal_version, proposal_content_hash,
                authenticated_actor_id, channel, channel_account_id,
                channel_conversation_id, conversation_binding_id, ttl_seconds,
                expires_at, issued_at
            ) VALUES (?, ?, ?, ?, 'edit', 0, ?, '111', 'telegram', 'acct', '111',
                      'binding', 600, 2000000000, '2026-09-13T00:00:00+00:00')
            """,
            (
                "haref_0123456789abcdef0123456789abcdef",
                "1" * 64,
                "bridge-human-action-issue:" + "2" * 32,
                parser_output_id,
                "3" * 64,
            ),
        )
        reference_id = upgraded.execute(
            "SELECT id FROM openclaw_human_action_references"
        ).fetchone()[0]
        upgraded.execute(
            """
            INSERT INTO parser_human_drafts (
                draft_public_id, source_parser_output_id, source_edit_reference_id,
                source_reference_public_id, start_redemption_public_id,
                start_redemption_material_hash, current_draft_version,
                current_draft_content_hash, current_payload_json, field_values_json,
                reason_policy_version, reason_contributors_json,
                reason_contributors_hash, unresolved_flags_json,
                unresolved_flags_hash, decision_target_parser_output_id,
                decision_target_proposal_version, decision_target_proposal_content_hash,
                current_card_generation_public_id, authenticated_actor_id,
                telegram_account_id, telegram_conversation_id, conversation_binding_id,
                state, expires_at, last_claimed_message_id, last_claimed_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, '{}', '{}', ?, '[]', ?, '[]', ?,
                      ?, 0, ?, ?, '111', 'acct', '111', 'binding', 'active',
                      2000000000, 10, 100, 100, 100)
            """,
            (
                "d1draft_" + "4" * 32,
                parser_output_id,
                reference_id,
                "haref_0123456789abcdef0123456789abcdef",
                "d1start_" + "5" * 32,
                "6" * 64,
                "7" * 64,
                "d1-reason-policy-v1",
                hashlib.sha256(b"[]").hexdigest(),
                hashlib.sha256(b"[]").hexdigest(),
                parser_output_id,
                "3" * 64,
                "d1card_" + "8" * 32,
            ),
        )
        draft_id = upgraded.execute("SELECT id FROM parser_human_drafts").fetchone()[0]
        raw = "金额：15.50\r\n币种: MYR".encode()
        upgraded.execute(
            """
            INSERT INTO parser_human_draft_reply_evidence (
                evidence_public_id, draft_id, raw_utf8, encoding, format_version,
                byte_length, sha256, authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id,
                telegram_message_id, received_at
            ) VALUES (?, ?, ?, 'UTF-8', 'd1-human-reply-v1', ?, ?, '111', 'acct',
                      '111', 'binding', 11, 101)
            """,
            ("d1evidence_" + "9" * 32, draft_id, raw, len(raw), hashlib.sha256(raw).hexdigest()),
        )
        assert (
            upgraded.execute("SELECT raw_utf8 FROM parser_human_draft_reply_evidence").fetchone()[0]
            == raw
        )
        upgraded.commit()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            upgraded.execute("UPDATE parser_human_draft_reply_evidence SET received_at = 102")
        upgraded.rollback()
        _assert_insert_or_replace_refused(
            upgraded,
            ("parser_human_drafts", "parser_human_draft_reply_evidence"),
        )
    finally:
        fresh.close()
        upgraded.close()


def _file_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_start_material(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, object] | None = None,
    source_type: str = "text",
) -> tuple[sqlite3.Row, bytes, str]:
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash

    if payload is None:
        payload = {
            "intent": "personal_expense",
            "amount": "12.50",
            "currency": "MYR",
            "transaction_date": "2026-09-13",
            "merchant": "Kopitiam",
            "description": "Lunch",
            "category": "food",
        }
    conn.execute(
        """
        INSERT INTO parser_outputs (
            public_id, source_type, source_public_id, parser_name, parser_version,
            raw_text, parsed_payload, parse_status
        ) VALUES ('prop_d1_source', ?, 'intake_d1', 'test', '1', 'lunch', ?,
                  'parsed_pending_confirmation')
        """,
        (source_type, json.dumps(payload)),
    )
    parser_output_id = conn.execute(
        "SELECT id FROM parser_outputs WHERE public_id = 'prop_d1_source'"
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO raw_intake_records (
            public_id, source_type, raw_input, received_at, status, parser_output_id
        ) VALUES ('intake_d1', 'telegram_text', 'lunch', '2026-09-13T00:00:00Z',
                  'parsed_pending_confirmation', ?)
        """,
        (parser_output_id,),
    )
    content_hash = compute_effective_proposal_content_hash(conn, {"id": parser_output_id})
    reference_material = b"d1-test-reference-material"
    conn.execute(
        """
        INSERT INTO openclaw_human_action_references (
            reference_public_id, reference_sha256, issuance_idempotency_key,
            parser_output_id, action, proposal_version, proposal_content_hash,
            authenticated_actor_id, channel, channel_account_id,
            channel_conversation_id, conversation_binding_id, ttl_seconds,
            expires_at, issued_at
        ) VALUES (?, ?, ?, ?, 'edit', 0, ?, '111', 'telegram', 'acct', '111',
                  'binding', 600, 2000, '1970-01-01T00:16:40+00:00')
        """,
        (
            "haref_abcdefabcdefabcdefabcdefabcdefab",
            hashlib.sha256(reference_material).hexdigest(),
            "bridge-human-action-issue:" + "a" * 32,
            parser_output_id,
            content_hash,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE action = 'edit'"
    ).fetchone()
    assert row is not None
    return row, reference_material, hashlib.sha256(b"callback-a").hexdigest()


def _start(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, object] | None = None,
    source_type: str = "text",
):
    from finance_core.parser_proposals.human_drafts import begin_human_draft_in_transaction

    row, reference_material, redemption_hash = _seed_start_material(
        conn, payload=payload, source_type=source_type
    )
    conn.execute("BEGIN IMMEDIATE")
    locked = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE id = ?", (row["id"],)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO openclaw_human_action_redemptions (
            reference_id, callback_id_sha256, callback_message_id, redeemed_at
        ) VALUES (?, ?, 100, '1970-01-01T00:16:40+00:00')
        """,
        (row["id"], redemption_hash),
    )
    result = begin_human_draft_in_transaction(
        conn,
        locked_edit_reference_row=locked,
        source_edit_reference_id=row["id"],
        reference_public_id=row["reference_public_id"],
        reference_integrity_material=reference_material,
        callback_message_id=100,
        redemption_public_id="d1start_callback_a",
        redemption_material_hash=redemption_hash,
        now_epoch=1000,
    )
    conn.commit()
    return result


def _card_text(card_ref: str, *, merchant: str = "Cafe") -> tuple[str, dict[str, str]]:
    fields = {
        "amount": "12.50",
        "currency": "MYR",
        "transaction_date": "2026-09-13",
        "merchant": merchant,
        "description": "Lunch: set",
        "category": "food",
    }
    text = (
        f"资料卡编号：{card_ref}\r\n金额: {fields['amount']}\r\n币种：{fields['currency']}\r\n"
        f"日期: {fields['transaction_date']}\r\n商户: {merchant}\r\n"
        f"描述: {fields['description']}\r\n分类: {fields['category']}"
    )
    return text, fields


def test_start_and_apply_preserve_exact_reply_and_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)

    def validate(current_payload, supplied, **kwargs):
        assert supplied == fields
        payload = dict(current_payload)
        payload.update(supplied)
        return SimpleNamespace(
            canonical_payload=payload,
            changed_fields=("merchant",),
            completeness="incomplete",
            reason_contributors=(),
            unresolved_flags=("missing_source",),
            explicit_clears={},
        )

    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", validate)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    command = HumanDraftCommand(
        card_generation_public_id=started.card_generation_public_id,
        telegram_message_id=101,
        operation_public_id="d1op_message_101",
        authenticated_actor_id="111",
        telegram_account_id="acct",
        telegram_conversation_id="111",
        conversation_binding_id="binding",
        raw_card_text=text,
        field_values=fields,
    )
    result = apply_human_draft_card(
        conn,
        command,
        publish=lambda *_: (_ for _ in ()).throw(AssertionError("incomplete must not publish")),
    )
    assert result.operation_outcome == "accepted"
    assert result.draft_version == 1
    assert result.current_card_generation_public_id == result.card_generation_public_id
    evidence = conn.execute("SELECT * FROM parser_human_draft_reply_evidence").fetchone()
    assert bytes(evidence["raw_utf8"]) == text.encode("utf-8")
    assert evidence["byte_length"] == len(text.encode("utf-8"))
    assert evidence["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()

    replay = apply_human_draft_card(conn, command, publish=lambda *_: None)
    assert replay.card_generation_public_id == result.card_generation_public_id
    assert replay.draft_content_hash == result.draft_content_hash
    assert replay.human_reply_evidence_public_id == result.human_reply_evidence_public_id
    assert replay.idempotent_replay is True
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == 2
    _assert_insert_or_replace_refused(
        conn,
        ("parser_human_draft_operations", "parser_human_draft_cards"),
    )
    with pytest.raises(human_drafts.HumanDraftError, match="operation_conflict"):
        apply_human_draft_card(
            conn,
            HumanDraftCommand(**{**command.__dict__, "raw_card_text": text + " "}),
            publish=lambda *_: None,
        )
    conn.close()


def test_refusal_claims_high_water_but_not_financial_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)

    def refuse(*args, **kwargs):
        raise human_drafts._HumanDraftRefusal("D1_DATE_INVALID")

    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", refuse)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    command = HumanDraftCommand(
        started.card_generation_public_id,
        102,
        "d1op_message_102",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    refused = apply_human_draft_card(conn, command, publish=lambda *_: None)
    assert refused.operation_outcome == "refused"
    assert refused.refusal_code == "D1_DATE_INVALID"
    assert refused.draft_version == 0
    assert refused.card_generation_public_id == started.card_generation_public_id
    head = conn.execute("SELECT * FROM parser_human_drafts").fetchone()
    assert head["last_claimed_message_id"] == 102
    assert head["current_draft_version"] == 0
    with pytest.raises(human_drafts.HumanDraftError, match="stale_message"):
        apply_human_draft_card(
            conn,
            HumanDraftCommand(
                **{
                    **command.__dict__,
                    "telegram_message_id": 101,
                    "operation_public_id": "d1op_message_101",
                }
            ),
            publish=lambda *_: None,
        )
    assert apply_human_draft_card(conn, command, publish=lambda *_: None).idempotent_replay
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 1
    conn.close()


def test_apply_rejects_nested_transaction_without_touching_it() -> None:
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftCommand,
        HumanDraftError,
        apply_human_draft_card,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    conn.execute("BEGIN")
    conn.execute("CREATE TEMP TABLE caller_work(value TEXT)")
    command = HumanDraftCommand(
        "d1card_" + "0" * 32, 1, "op", "111", "acct", "111", "binding", "x", {}
    )
    with pytest.raises(HumanDraftError, match="transaction_conflict"):
        apply_human_draft_card(conn, command, publish=lambda *_: None)
    assert conn.in_transaction
    assert conn.execute("SELECT name FROM sqlite_temp_master WHERE name='caller_work'").fetchone()
    conn.rollback()
    conn.close()


def test_start_requires_matching_redemption_and_shared_uow_rollback() -> None:
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftError,
        begin_human_draft_in_transaction,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    row, reference_material, redemption_hash = _seed_start_material(conn)
    conn.execute("BEGIN IMMEDIATE")
    locked = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE id = ?", (row["id"],)
    ).fetchone()
    with pytest.raises(HumanDraftError, match="redemption_required"):
        begin_human_draft_in_transaction(
            conn,
            locked_edit_reference_row=locked,
            source_edit_reference_id=row["id"],
            reference_public_id=row["reference_public_id"],
            reference_integrity_material=reference_material,
            callback_message_id=100,
            redemption_public_id="d1start_callback_a",
            redemption_material_hash=redemption_hash,
            now_epoch=1000,
        )
    conn.rollback()

    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        INSERT INTO openclaw_human_action_redemptions (
            reference_id, callback_id_sha256, callback_message_id, redeemed_at
        ) VALUES (?, ?, 100, '1970-01-01T00:16:40+00:00')
        """,
        (row["id"], redemption_hash),
    )
    begin_human_draft_in_transaction(
        conn,
        locked_edit_reference_row=locked,
        source_edit_reference_id=row["id"],
        reference_public_id=row["reference_public_id"],
        reference_integrity_material=reference_material,
        callback_message_id=100,
        redemption_public_id="d1start_callback_a",
        redemption_material_hash=redemption_hash,
        now_epoch=1000,
    )
    conn.rollback()  # injected failure in the shared redemption/start UOW
    for table in (
        "openclaw_human_action_redemptions",
        "parser_human_drafts",
        "parser_human_draft_operations",
        "parser_human_draft_cards",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    conn.close()


def _complete_validator(current_payload, supplied, **kwargs):
    payload = dict(current_payload)
    payload.update(supplied)
    return SimpleNamespace(
        canonical_payload=payload,
        changed_fields=("merchant",),
        completeness="publishable",
        reason_contributors=(),
        unresolved_flags=(),
        explicit_clears={},
    )


def _valid_child_publisher(conn, payload, context):
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
    from finance_core.parser_proposals.human_drafts import PublishedDraft

    parent = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (context["source_parser_output_id"],)
    ).fetchone()
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (
            public_id, source_type, source_public_id, statement_batch_id,
            attachment_id, parser_name, parser_version, raw_text, parsed_payload,
            normalized_payload, parse_status, parent_parser_output_id
        ) VALUES ('prop_d1_child', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  'parsed_pending_confirmation', ?)
        """,
        (
            parent["source_type"],
            parent["source_public_id"],
            parent["statement_batch_id"],
            parent["attachment_id"],
            parent["parser_name"],
            parent["parser_version"],
            parent["raw_text"],
            json.dumps(payload),
            json.dumps(payload),
            parent["id"],
        ),
    )
    child_id = int(cursor.lastrowid)
    conn.execute(
        "UPDATE parser_outputs SET parse_status = 'superseded' WHERE id = ?", (parent["id"],)
    )
    raw_intake_id = conn.execute("SELECT source_raw_intake_id FROM parser_human_drafts").fetchone()[
        0
    ]
    conn.execute(
        "UPDATE raw_intake_records SET parser_output_id = ? WHERE id = ?",
        (child_id, raw_intake_id),
    )
    content_hash = compute_effective_proposal_content_hash(conn, {"id": child_id})
    return PublishedDraft(child_id, "prop_d1_child", 0, content_hash)


def test_publication_result_must_match_persisted_current_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftCommand,
        HumanDraftError,
        PublishedDraft,
        apply_human_draft_card,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op_publish_101",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )

    def fabricated_hash(conn, payload, context):
        published = _valid_child_publisher(conn, payload, context)
        return PublishedDraft(
            published.parser_output_id,
            published.proposal_public_id,
            published.proposal_version,
            "f" * 64,
        )

    with pytest.raises(HumanDraftError, match="publication_content_hash"):
        apply_human_draft_card(conn, command, publish=fabricated_hash)
    assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 1
    assert conn.execute("SELECT current_draft_version FROM parser_human_drafts").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("mutate_in_place", [False, True])
def test_publication_payload_substitution_rolls_back_entire_operation(
    monkeypatch: pytest.MonkeyPatch, mutate_in_place: bool
) -> None:
    """A publisher cannot replace validated fields while returning a self-consistent hash."""
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftCommand,
        HumanDraftError,
        apply_human_draft_card,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)

    def substituting_publisher(conn, payload, context):
        substituted = payload if mutate_in_place else dict(payload)
        substituted["merchant"] = "SUBSTITUTED"
        return _valid_child_publisher(conn, substituted, context)

    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op-payload-substitution",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    with pytest.raises(HumanDraftError, match="publication_payload"):
        apply_human_draft_card(conn, command, publish=substituting_publisher)

    assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_publications").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0] == 0
    draft = conn.execute("SELECT * FROM parser_human_drafts").fetchone()
    assert draft["current_draft_version"] == 0
    assert draft["current_card_generation_public_id"] == started.card_generation_public_id
    assert conn.execute("SELECT parser_output_id FROM raw_intake_records").fetchone()[0] == 1
    conn.close()


def test_publisher_context_mutation_cannot_rewrite_operation_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callback-visible nested context is isolated from persisted audit material."""
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn, payload=_validation_payload())
    fields = {**started.field_values, "description": ""}
    text = (
        f"资料卡编号：{started.card_generation_public_id}\r\n"
        f"金额: {fields['amount']}\r\n币种：{fields['currency']}\r\n"
        f"日期: {fields['transaction_date']}\r\n商户: {fields['merchant']}\r\n"
        f"描述: {fields['description']}\r\n分类: {fields['category']}"
    )
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)

    def context_mutating_publisher(conn, payload, context):
        context["explicit_clears"]["description"] = ("tampered", "tampered")
        context["canonical_supplied_fields"]["description"] = "tampered"
        return _valid_child_publisher(conn, payload, context)

    result = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op-context-mutation",
            "111",
            "acct",
            "111",
            "binding",
            text,
            fields,
        ),
        publish=context_mutating_publisher,
    )
    assert result.operation_outcome == "accepted"
    operation = conn.execute(
        "SELECT canonical_supplied_fields_json, explicit_clears_json "
        "FROM parser_human_draft_operations "
        "WHERE operation_public_id = 'd1op-context-mutation'"
    ).fetchone()
    assert json.loads(operation["canonical_supplied_fields_json"])["description"] == ""
    assert json.loads(operation["explicit_clears_json"]) == {"description": ["Lunch", ""]}
    conn.close()


def test_head_rejects_incoherent_direct_sql_updates() -> None:
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    with pytest.raises(sqlite3.IntegrityError, match="mutation shape"):
        conn.execute("UPDATE parser_human_drafts SET current_draft_content_hash = ?", ("f" * 64,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="mutation shape"):
        conn.execute(
            "UPDATE parser_human_drafts SET current_card_generation_public_id = ?",
            ("d1card_" + "f" * 32,),
        )
    conn.rollback()
    conn.close()


def test_operation_evidence_and_action_binding_have_exact_relational_closure() -> None:
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    draft = conn.execute("SELECT * FROM parser_human_drafts").fetchone()
    card = conn.execute("SELECT * FROM parser_human_draft_cards").fetchone()
    start = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_type = 'start'"
    ).fetchone()
    assert draft is not None and card is not None and start is not None

    raw = b"cross-context"
    conn.execute(
        """
        INSERT INTO parser_human_draft_reply_evidence (
            evidence_public_id, draft_id, raw_utf8, encoding, format_version,
            byte_length, sha256, authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            telegram_message_id, received_at
        ) VALUES (?, ?, ?, 'UTF-8', 'd1-human-reply-v1', ?, ?,
                  '111', 'acct', '111', 'binding', 101, 1001)
        """,
        (
            "d1evidence_" + "c" * 32,
            draft["id"],
            raw,
            len(raw),
            hashlib.sha256(raw).hexdigest(),
        ),
    )
    evidence_id = conn.execute(
        "SELECT id FROM parser_human_draft_reply_evidence WHERE telegram_message_id = 101"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            """
            INSERT INTO parser_human_draft_operations (
                operation_public_id, draft_id, operation_type, operation_outcome,
                result_completeness, telegram_message_id, request_material_hash,
                human_reply_evidence_id, before_draft_version,
                before_draft_content_hash, after_draft_version,
                after_draft_content_hash, canonical_supplied_fields_json,
                material_changes_json, explicit_clears_json, reason_policy_version,
                reason_contributors_before_json, reason_contributors_before_hash,
                reason_contributors_after_json, reason_contributors_after_hash,
                unresolved_flags_json, result_card_generation_public_id,
                authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id,
                correction_channel, created_at
            ) VALUES ('tampered-operation', ?, 'accepted', 'accepted', 'complete',
                      101, ?, ?, 0, ?, 0, ?, '{}', '{}', '[]',
                      'd1-reason-policy-v1', '[]', ?, '[]', ?, '[]', ?,
                      '111', 'wrong-account', '111', 'binding', 'telegram', 1001)
            """,
            (
                draft["id"],
                "d" * 64,
                evidence_id,
                draft["current_draft_content_hash"],
                draft["current_draft_content_hash"],
                start["reason_contributors_before_hash"],
                start["reason_contributors_after_hash"],
                card["card_generation_public_id"],
            ),
        )
    conn.rollback()

    def add_reference(public_suffix: str, account_id: str) -> int:
        conn.execute(
            """
            INSERT INTO openclaw_human_action_references (
                reference_public_id, reference_sha256, issuance_idempotency_key,
                parser_output_id, action, proposal_version, proposal_content_hash,
                authenticated_actor_id, channel, channel_account_id,
                channel_conversation_id, conversation_binding_id, ttl_seconds,
                expires_at, issued_at
            ) VALUES (?, ?, ?, ?, 'confirm', ?, ?, '111', 'telegram', ?, '111',
                      'binding', 600, 2000, '1970-01-01T00:16:40+00:00')
            """,
            (
                "haref_" + public_suffix * 32,
                public_suffix * 64,
                "bridge-human-action-issue:" + public_suffix * 32,
                card["decision_target_parser_output_id"],
                card["decision_target_proposal_version"],
                card["decision_target_proposal_content_hash"],
                account_id,
            ),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    bad_reference_id = add_reference("d", "wrong-account")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            """
            INSERT INTO parser_human_draft_action_bindings (
                reference_id, card_generation_public_id, draft_id,
                parser_output_id, proposal_version, proposal_content_hash,
                authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, '111', 'acct', '111', 'binding', 1001)
            """,
            (
                bad_reference_id,
                card["card_generation_public_id"],
                draft["id"],
                card["decision_target_parser_output_id"],
                card["decision_target_proposal_version"],
                card["decision_target_proposal_content_hash"],
            ),
        )
    conn.rollback()
    good_reference_id = add_reference("e", "acct")
    conn.execute(
        """
        INSERT INTO parser_human_draft_action_bindings (
            reference_id, card_generation_public_id, draft_id,
            parser_output_id, proposal_version, proposal_content_hash,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, '111', 'acct', '111', 'binding', 1001)
        """,
        (
            good_reference_id,
            card["card_generation_public_id"],
            draft["id"],
            card["decision_target_parser_output_id"],
            card["decision_target_proposal_version"],
            card["decision_target_proposal_content_hash"],
        ),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE parser_human_draft_action_bindings SET created_at = 1002")
    conn.rollback()
    _assert_insert_or_replace_refused(conn, ("parser_human_draft_action_bindings",))
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


@pytest.mark.parametrize("invalid_kind", ["terminal", "unrelated", "stale"])
def test_publication_terminal_unrelated_or_stale_rolls_back(
    monkeypatch: pytest.MonkeyPatch, invalid_kind: str
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)

    def invalid_publisher(conn, payload, context):
        published = _valid_child_publisher(conn, payload, context)
        if invalid_kind == "terminal":
            conn.execute(
                "UPDATE parser_outputs SET parse_status = 'confirmed' WHERE id = ?",
                (published.parser_output_id,),
            )
        elif invalid_kind == "unrelated":
            return type(published)(
                context["source_parser_output_id"],
                "prop_d1_source",
                0,
                published.proposal_content_hash,
            )
        else:
            conn.execute(
                """
                INSERT INTO parser_outputs (
                    public_id, source_type, source_public_id, parser_name,
                    parser_version, raw_text, parsed_payload, normalized_payload,
                    parse_status, parent_parser_output_id
                ) SELECT 'prop_d1_grandchild', source_type, source_public_id,
                         parser_name, parser_version, raw_text, parsed_payload,
                         normalized_payload, 'parsed_pending_confirmation', id
                  FROM parser_outputs WHERE id = ?
                """,
                (published.parser_output_id,),
            )
        return published

    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op-invalid-" + invalid_kind,
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    with pytest.raises(human_drafts.HumanDraftError, match="publication_"):
        apply_human_draft_card(conn, command, publish=invalid_publisher)
    assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0] == 0
    conn.close()


def test_publisher_cannot_commit_outside_owned_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)

    def escaping_publisher(conn, payload, context):
        published = _valid_child_publisher(conn, payload, context)
        conn.commit()
        return published

    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op-escaping-publisher",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        apply_human_draft_card(conn, command, publish=escaping_publisher)
    assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0] == 0
    conn.close()


def test_start_exact_replay_survives_connection_restart(tmp_path: Path) -> None:
    from finance_core.parser_proposals.human_drafts import begin_human_draft_in_transaction

    path = tmp_path / "draft-restart.db"
    conn = _file_connection(path)
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    row, reference_material, redemption_hash = _seed_start_material(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        INSERT INTO openclaw_human_action_redemptions (
            reference_id, callback_id_sha256, callback_message_id, redeemed_at
        ) VALUES (?, ?, 100, '1970-01-01T00:16:40+00:00')
        """,
        (row["id"], redemption_hash),
    )
    locked = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE id = ?", (row["id"],)
    ).fetchone()
    first = begin_human_draft_in_transaction(
        conn,
        locked_edit_reference_row=locked,
        source_edit_reference_id=row["id"],
        reference_public_id=row["reference_public_id"],
        reference_integrity_material=reference_material,
        callback_message_id=100,
        redemption_public_id="d1start_callback_a",
        redemption_material_hash=redemption_hash,
        now_epoch=1000,
    )
    conn.commit()
    conn.close()

    restarted = _file_connection(path)
    restarted.execute("BEGIN IMMEDIATE")
    locked = restarted.execute(
        "SELECT * FROM openclaw_human_action_references WHERE id = ?", (row["id"],)
    ).fetchone()
    replay = begin_human_draft_in_transaction(
        restarted,
        locked_edit_reference_row=locked,
        source_edit_reference_id=row["id"],
        reference_public_id=row["reference_public_id"],
        reference_integrity_material=reference_material,
        callback_message_id=100,
        redemption_public_id="d1start_callback_a",
        redemption_material_hash=redemption_hash,
        now_epoch=1000,
    )
    restarted.commit()
    assert replay.idempotent_replay
    assert replay.card_generation_public_id == first.card_generation_public_id
    assert restarted.execute("SELECT COUNT(*) FROM parser_human_drafts").fetchone()[0] == 1
    restarted.close()


def test_two_connections_cannot_both_advance_the_same_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftCommand,
        HumanDraftError,
        apply_human_draft_card,
    )

    path = tmp_path / "draft-race.db"
    first_conn = _file_connection(path)
    apply_migration_paths(first_conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(first_conn)
    second_conn = _file_connection(path)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    first_command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op-race-first",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    second_command = HumanDraftCommand(
        started.card_generation_public_id,
        102,
        "d1op-race-second",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    first = apply_human_draft_card(first_conn, first_command, publish=_valid_child_publisher)
    with pytest.raises(HumanDraftError, match="stale_card"):
        apply_human_draft_card(second_conn, second_command, publish=_valid_child_publisher)
    assert first.draft_version == 1
    operation_count = first_conn.execute(
        "SELECT COUNT(*) FROM parser_human_draft_operations"
    ).fetchone()[0]
    evidence_count = first_conn.execute(
        "SELECT COUNT(*) FROM parser_human_draft_reply_evidence"
    ).fetchone()[0]
    assert operation_count == 2
    assert evidence_count == 1
    first_conn.close()
    second_conn.close()


def test_publisher_cannot_commit_outside_repository_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)

    def escaping_publisher(conn, payload, context):
        published = _valid_child_publisher(conn, payload, context)
        conn.commit()
        return published

    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op-publisher-commit",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        apply_human_draft_card(conn, command, publish=escaping_publisher)
    assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    "target_mutation",
    [
        "confirmed",
        "rejected",
        "expired",
        "superseded",
        "authorized",
        "text_converted",
        "receipt_converted",
        "effective_version_hash",
        "competing_child",
    ],
)
def test_apply_proves_current_unconverted_decision_target_before_any_d1_write(
    monkeypatch: pytest.MonkeyPatch,
    target_mutation: str,
) -> None:
    """Removing the pre-publication target proof must call the publisher too late."""
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftCommand,
        HumanDraftError,
        apply_human_draft_card,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    target_id = conn.execute(
        "SELECT decision_target_parser_output_id FROM parser_human_drafts"
    ).fetchone()[0]
    target = conn.execute("SELECT * FROM parser_outputs WHERE id = ?", (target_id,)).fetchone()
    assert target is not None

    if target_mutation in {"confirmed", "rejected", "expired", "superseded"}:
        conn.execute(
            "UPDATE parser_outputs SET parse_status = ? WHERE id = ?",
            (target_mutation, target_id),
        )
    elif target_mutation in {"authorized", "text_converted", "receipt_converted"}:
        target_hash = compute_effective_proposal_content_hash(conn, target)
        conn.execute(
            """
            INSERT INTO parser_proposal_authorizations (
                confirmation_public_id, parser_output_id, proposal_content_hash,
                actor_type, authenticated_actor_id, confirmation_state,
                confirmation_channel, decided_at
            ) VALUES ('decision-preexisting', ?, ?, 'human', '111', 'confirmed',
                      'cli', '2026-09-13T00:00:00Z')
            """,
            (target_id, target_hash),
        )
        if target_mutation == "text_converted":
            transaction_id = conn.execute(
                """
                INSERT INTO transactions (
                    public_id, intent, intent_type, transaction_date
                ) VALUES ('txn-preexisting-conversion', 'expense', 'Manual', '2026-09-13')
                """
            ).lastrowid
            conn.execute(
                """
                INSERT INTO parser_proposal_conversion_audit (
                    parser_output_id, transaction_id, confirmation_public_id,
                    proposal_content_hash, authenticated_actor_id
                ) VALUES (?, ?, 'decision-preexisting', ?, '111')
                """,
                (target_id, transaction_id, target_hash),
            )
        elif target_mutation == "receipt_converted":
            payer_id = conn.execute(
                """
                INSERT INTO participants (public_id, display_name)
                VALUES ('person-preexisting-conversion', 'Conversion Fixture')
                """
            ).lastrowid
            receipt_id = conn.execute(
                """
                INSERT INTO receipts (
                    public_id, merchant, net_paid_amount,
                    net_paid_amount_canonical_text, currency, payer_participant_id
                ) VALUES (
                    'receipt-preexisting-conversion', 'Conversion Fixture',
                    '9.99', '9.99', 'SGD', ?
                )
                """,
                (payer_id,),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO receipt_proposal_conversions (
                    command_public_id, parser_output_id,
                    supersession_root_parser_output_id, receipt_id,
                    confirmation_public_id, proposal_content_hash,
                    command_material_hash, conversion_result_hash, actor_type,
                    authenticated_actor_id, conversion_channel
                ) VALUES (
                    'rpfc_preexisting', ?, ?, ?, 'decision-preexisting', ?,
                    ?, ?, 'human', '111', 'cli'
                )
                """,
                (
                    target_id,
                    target_id,
                    receipt_id,
                    target_hash,
                    hashlib.sha256(b"preexisting-command").hexdigest(),
                    hashlib.sha256(b"preexisting-result").hexdigest(),
                ),
            )
    elif target_mutation == "effective_version_hash":
        changed_payload = json.loads(target["parsed_payload"])
        changed_payload["merchant"] = "Changed elsewhere"
        changed_hash = hashlib.sha256(b"persisted-completion-marker").hexdigest()
        conn.execute(
            """
            INSERT INTO parser_proposal_completions (
                completion_public_id, parser_output_id, version_number,
                base_content_hash, completed_content_hash, completed_payload_json,
                field_updates_json, actor_type, authenticated_actor_id,
                completion_channel
            ) VALUES ('completion-preexisting', ?, 1, ?, ?, ?, '{"merchant":"Changed elsewhere"}',
                      'human', '111', 'cli')
            """,
            (
                target_id,
                compute_effective_proposal_content_hash(conn, target),
                changed_hash,
                json.dumps(changed_payload),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO parser_outputs (
                public_id, source_type, source_public_id, parser_name,
                parser_version, raw_text, parsed_payload, normalized_payload,
                parse_status, parent_parser_output_id
            ) SELECT 'prop_competing_child', source_type, source_public_id, parser_name,
                     parser_version, raw_text, parsed_payload, normalized_payload,
                     'parsed_pending_confirmation', id
              FROM parser_outputs WHERE id = ?
            """,
            (target_id,),
        )
    conn.commit()

    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    publisher_calls = 0

    def publisher_must_not_run(*_args):
        nonlocal publisher_calls
        publisher_calls += 1
        raise AssertionError("publisher called before decision-target proof")

    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        f"d1op-preproof-{target_mutation}",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    with pytest.raises(HumanDraftError, match="proposal_|publication_"):
        apply_human_draft_card(conn, command, publish=publisher_must_not_run)
    assert publisher_calls == 0
    for table in (
        "parser_human_draft_reply_evidence",
        "parser_human_draft_publications",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 1
    assert conn.execute("SELECT current_draft_version FROM parser_human_drafts").fetchone()[0] == 0
    conn.close()


def test_first_and_consecutive_second_publication_use_the_current_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requiring the original source to remain pending would block the second edit."""
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftCommand,
        PublishedDraft,
        apply_human_draft_card,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", _complete_validator)
    clock = iter((1001, 1002))
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: next(clock))
    published_ids: list[int] = []

    def publish_current_leaf(conn, payload, context):
        parent = conn.execute(
            "SELECT * FROM parser_outputs WHERE id = ?",
            (context["source_parser_output_id"],),
        ).fetchone()
        assert parent is not None
        ordinal = len(published_ids) + 1
        public_id = f"prop_d1_child_{ordinal}"
        cursor = conn.execute(
            """
            INSERT INTO parser_outputs (
                public_id, source_type, source_public_id, statement_batch_id,
                attachment_id, parser_name, parser_version, raw_text,
                parsed_payload, normalized_payload, parse_status,
                parent_parser_output_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsed_pending_confirmation', ?)
            """,
            (
                public_id,
                parent["source_type"],
                parent["source_public_id"],
                parent["statement_batch_id"],
                parent["attachment_id"],
                parent["parser_name"],
                parent["parser_version"],
                parent["raw_text"],
                json.dumps(payload),
                json.dumps(payload),
                parent["id"],
            ),
        )
        child_id = int(cursor.lastrowid)
        published_ids.append(child_id)
        conn.execute(
            "UPDATE parser_outputs SET parse_status = 'superseded' WHERE id = ?",
            (parent["id"],),
        )
        raw_intake_id = conn.execute(
            "SELECT source_raw_intake_id FROM parser_human_drafts"
        ).fetchone()[0]
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = ? WHERE id = ?",
            (child_id, raw_intake_id),
        )
        content_hash = compute_effective_proposal_content_hash(conn, {"id": child_id})
        return PublishedDraft(child_id, public_id, 0, content_hash)

    first_text, first_fields = _card_text(started.card_generation_public_id, merchant="Cafe")
    first = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op-first-publication",
            "111",
            "acct",
            "111",
            "binding",
            first_text,
            first_fields,
        ),
        publish=publish_current_leaf,
    )
    second_text, second_fields = _card_text(first.card_generation_public_id, merchant="Bistro")
    second = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            first.card_generation_public_id,
            102,
            "d1op-second-publication",
            "111",
            "acct",
            "111",
            "binding",
            second_text,
            second_fields,
        ),
        publish=publish_current_leaf,
    )
    assert second.draft_version == 2
    assert second.proposal_public_id == "prop_d1_child_2"
    assert len(published_ids) == 2
    assert published_ids[0] != published_ids[1]
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_publications").fetchone()[0] == 2
    conn.close()


def test_raw_intake_pointer_move_is_refused_before_publisher_or_d1_write() -> None:
    """Removing the landed raw-pointer trigger would detach the active D1 target."""
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    conn.execute(
        """
        INSERT INTO parser_outputs (
            public_id, source_type, source_public_id, parser_name,
            parser_version, raw_text, parsed_payload, parse_status
        ) VALUES ('prop_unrelated_pointer', 'text', 'intake_d1', 'test', '1',
                  'other', '{}', 'parsed_pending_confirmation')
        """
    )
    unrelated_id = conn.execute(
        "SELECT id FROM parser_outputs WHERE public_id = 'prop_unrelated_pointer'"
    ).fetchone()[0]
    d1_before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "parser_human_draft_reply_evidence",
            "parser_human_draft_operations",
            "parser_human_draft_cards",
            "parser_human_draft_publications",
        )
    }
    publisher_calls = 0

    def publisher_must_not_run(*_args):
        nonlocal publisher_calls
        publisher_calls += 1

    with pytest.raises(sqlite3.IntegrityError, match="pointer cannot be detached"):
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = ? WHERE public_id = 'intake_d1'",
            (unrelated_id,),
        )
    conn.rollback()
    assert publisher_calls == 0
    assert {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in d1_before
    } == d1_before
    conn.close()


@pytest.mark.parametrize(
    ("column", "forged_value"),
    [
        ("authenticated_actor_id", "222"),
        ("telegram_account_id", "other-account"),
        ("telegram_conversation_id", "222"),
        ("conversation_binding_id", "other-binding"),
    ],
)
def test_evidence_context_must_match_its_draft(
    column: str,
    forged_value: str,
) -> None:
    """Dropping the evidence-to-head composite FK admits a cross-context row."""
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    draft_id = conn.execute("SELECT id FROM parser_human_drafts").fetchone()[0]
    context = {
        "authenticated_actor_id": "111",
        "telegram_account_id": "acct",
        "telegram_conversation_id": "111",
        "conversation_binding_id": "binding",
    }
    context[column] = forged_value
    raw = b"forged context"
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            """
            INSERT INTO parser_human_draft_reply_evidence (
                evidence_public_id, draft_id, raw_utf8, encoding, format_version,
                byte_length, sha256, authenticated_actor_id, telegram_account_id,
                telegram_conversation_id, conversation_binding_id,
                telegram_message_id, received_at
            ) VALUES (?, ?, ?, 'UTF-8', 'd1-human-reply-v1', ?, ?, ?, ?, ?, ?, 101, 1001)
            """,
            (
                "d1evidence_" + "f" * 32,
                draft_id,
                raw,
                len(raw),
                hashlib.sha256(raw).hexdigest(),
                context["authenticated_actor_id"],
                context["telegram_account_id"],
                context["telegram_conversation_id"],
                context["conversation_binding_id"],
            ),
        )
    conn.rollback()
    conn.close()


def _insert_minimal_noop_operation(
    conn: sqlite3.Connection,
    *,
    draft: sqlite3.Row,
    operation_public_id: str,
    result_card_generation_public_id: str,
) -> int:
    raw = b"cross-result-card"
    conn.execute(
        """
        INSERT INTO parser_human_draft_reply_evidence (
            evidence_public_id, draft_id, raw_utf8, encoding, format_version,
            byte_length, sha256, authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            telegram_message_id, received_at
        ) VALUES (?, ?, ?, 'UTF-8', 'd1-human-reply-v1', ?, ?, ?, ?, ?, ?, 401, 1001)
        """,
        (
            "d1evidence_" + "a" * 32,
            draft["id"],
            raw,
            len(raw),
            hashlib.sha256(raw).hexdigest(),
            draft["authenticated_actor_id"],
            draft["telegram_account_id"],
            draft["telegram_conversation_id"],
            draft["conversation_binding_id"],
        ),
    )
    evidence_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO parser_human_draft_operations (
            operation_public_id, draft_id, operation_type, operation_outcome,
            result_completeness, telegram_message_id, request_material_hash,
            human_reply_evidence_id, before_draft_version, before_draft_content_hash,
            after_draft_version, after_draft_content_hash,
            canonical_supplied_fields_json, material_changes_json,
            explicit_clears_json, reason_policy_version,
            reason_contributors_before_json, reason_contributors_before_hash,
            reason_contributors_after_json, reason_contributors_after_hash,
            unresolved_flags_json, result_card_generation_public_id,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            correction_channel, created_at
        ) VALUES (?, ?, 'noop', 'noop', ?, 401, ?, ?, ?, ?, ?, ?, '{}', '{}',
                  '[]', 'd1-reason-policy-v1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  'telegram', 1001)
        """,
        (
            operation_public_id,
            draft["id"],
            _result_completeness_for_test(draft),
            "a" * 64,
            evidence_id,
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["unresolved_flags_json"],
            result_card_generation_public_id,
            draft["authenticated_actor_id"],
            draft["telegram_account_id"],
            draft["telegram_conversation_id"],
            draft["conversation_binding_id"],
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_minimal_card(
    conn: sqlite3.Connection,
    *,
    draft: sqlite3.Row,
    card_generation_public_id: str,
    original_operation_id: int,
    predecessor_card_id: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO parser_human_draft_cards (
            card_generation_public_id, draft_id, draft_version,
            draft_content_hash, field_values_json,
            decision_target_parser_output_id, decision_target_proposal_version,
            decision_target_proposal_content_hash, language, format_version,
            action_issue_batch_id, predecessor_card_id, original_operation_id,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            expires_at, issued_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'mixed', 'd1-human-card-v1', ?, ?, ?,
                  ?, ?, ?, ?, 1300, 1000)
        """,
        (
            card_generation_public_id,
            draft["id"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["field_values_json"],
            draft["decision_target_parser_output_id"],
            draft["decision_target_proposal_version"],
            draft["decision_target_proposal_content_hash"],
            "b" * 64,
            predecessor_card_id,
            original_operation_id,
            draft["authenticated_actor_id"],
            draft["telegram_account_id"],
            draft["telegram_conversation_id"],
            draft["conversation_binding_id"],
        ),
    )


@pytest.mark.parametrize("cross_relation", ["predecessor", "original_operation", "result_card"])
def test_card_operation_relationships_cannot_cross_draft_context(cross_relation: str) -> None:
    """Dropping any closure FK admits a cross-draft predecessor/operation/card edge."""
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    _start_second_context(conn)
    drafts = conn.execute("SELECT * FROM parser_human_drafts ORDER BY id").fetchall()
    first, second = drafts
    first_card = conn.execute(
        "SELECT * FROM parser_human_draft_cards WHERE draft_id = ?", (first["id"],)
    ).fetchone()
    second_card = conn.execute(
        "SELECT * FROM parser_human_draft_cards WHERE draft_id = ?", (second["id"],)
    ).fetchone()
    second_operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE draft_id = ?", (second["id"],)
    ).fetchone()
    assert first_card is not None and second_card is not None and second_operation is not None
    forged_card_id = "d1card_" + "e" * 32
    conn.execute("BEGIN IMMEDIATE")
    if cross_relation == "predecessor":
        first_operation = conn.execute(
            "SELECT id FROM parser_human_draft_operations WHERE draft_id = ?",
            (first["id"],),
        ).fetchone()
        assert first_operation is not None
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _insert_minimal_card(
                conn,
                draft=first,
                card_generation_public_id=forged_card_id,
                original_operation_id=first_operation["id"],
                predecessor_card_id=second_card["id"],
            )
        conn.rollback()
    elif cross_relation == "original_operation":
        _insert_minimal_card(
            conn,
            draft=first,
            card_generation_public_id=forged_card_id,
            original_operation_id=second_operation["id"],
            predecessor_card_id=None,
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.commit()
        conn.rollback()
    else:
        _insert_minimal_noop_operation(
            conn,
            draft=first,
            operation_public_id="cross-result-card-operation",
            result_card_generation_public_id=second_card["card_generation_public_id"],
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.commit()
        conn.rollback()
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def _insert_terminal_operation(
    conn: sqlite3.Connection,
    *,
    operation_public_id: str,
    operation_type: str,
    decision_public_id: str | None,
    action_reference_id: int | None = None,
) -> None:
    draft = conn.execute("SELECT * FROM parser_human_drafts").fetchone()
    assert draft is not None
    conn.execute(
        """
        INSERT INTO parser_human_draft_operations (
            operation_public_id, draft_id, operation_type, operation_outcome,
            result_completeness, request_material_hash, action_reference_id,
            decision_public_id, before_draft_version, before_draft_content_hash,
            after_draft_version, after_draft_content_hash,
            canonical_supplied_fields_json, material_changes_json,
            explicit_clears_json, reason_policy_version,
            reason_contributors_before_json, reason_contributors_before_hash,
            reason_contributors_after_json, reason_contributors_after_hash,
            unresolved_flags_json, result_card_generation_public_id,
            authenticated_actor_id, telegram_account_id,
            telegram_conversation_id, conversation_binding_id,
            correction_channel, created_at
        ) VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '[]',
                  'd1-reason-policy-v1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'telegram', 1100)
        """,
        (
            operation_public_id,
            draft["id"],
            operation_type,
            _result_completeness_for_test(draft),
            "d" * 64,
            action_reference_id,
            decision_public_id,
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["current_draft_version"],
            draft["current_draft_content_hash"],
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["reason_contributors_json"],
            draft["reason_contributors_hash"],
            draft["unresolved_flags_json"],
            draft["current_card_generation_public_id"],
            draft["authenticated_actor_id"],
            draft["telegram_account_id"],
            draft["telegram_conversation_id"],
            draft["conversation_binding_id"],
        ),
    )


def _result_completeness_for_test(draft: sqlite3.Row) -> str:
    payload = json.loads(draft["current_payload_json"])
    return "complete" if payload.get("amount") and payload.get("currency") else "incomplete"


def _insert_decision(
    conn: sqlite3.Connection,
    *,
    decision_public_id: str,
    state: str,
    actor_id: str = "111",
    parser_output_id: int | None = None,
    proposal_content_hash: str | None = None,
) -> int:
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash

    draft = conn.execute("SELECT * FROM parser_human_drafts").fetchone()
    assert draft is not None
    target_id = int(draft["decision_target_parser_output_id"])
    decision_target_id = target_id if parser_output_id is None else parser_output_id
    proposal = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (decision_target_id,)
    ).fetchone()
    assert proposal is not None
    decision_hash = (
        compute_effective_proposal_content_hash(conn, proposal)
        if proposal_content_hash is None
        else proposal_content_hash
    )
    conn.execute(
        """
        INSERT INTO parser_proposal_authorizations (
            confirmation_public_id, parser_output_id, proposal_content_hash,
            actor_type, authenticated_actor_id, confirmation_state,
            confirmation_channel, decided_at
        ) VALUES (?, ?, ?, 'human', ?, ?, 'cli', '2026-09-13T00:00:00Z')
        """,
        (decision_public_id, decision_target_id, decision_hash, actor_id, state),
    )
    conn.execute(
        "UPDATE parser_outputs SET parse_status = ? WHERE id = ?", (state, decision_target_id)
    )
    return decision_target_id


def _insert_d1_action_binding(
    conn: sqlite3.Connection,
    *,
    action: str,
    card_generation_public_id: str,
    suffix: str,
    redeemed: bool,
) -> str:
    card = conn.execute(
        "SELECT * FROM parser_human_draft_cards WHERE card_generation_public_id = ?",
        (card_generation_public_id,),
    ).fetchone()
    assert card is not None
    reference_public_id = "haref_" + suffix * 32
    conn.execute(
        """
        INSERT INTO openclaw_human_action_references (
            reference_public_id, reference_sha256, issuance_idempotency_key,
            parser_output_id, action, proposal_version, proposal_content_hash,
            authenticated_actor_id, channel, channel_account_id,
            channel_conversation_id, conversation_binding_id, ttl_seconds,
            expires_at, issued_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'telegram', ?, ?, ?, 600, 2000,
                  '1970-01-01T00:16:40+00:00')
        """,
        (
            reference_public_id,
            suffix * 64,
            "bridge-human-action-issue:" + suffix * 32,
            card["decision_target_parser_output_id"],
            action,
            card["decision_target_proposal_version"],
            card["decision_target_proposal_content_hash"],
            card["authenticated_actor_id"],
            card["telegram_account_id"],
            card["telegram_conversation_id"],
            card["conversation_binding_id"],
        ),
    )
    reference_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if redeemed:
        conn.execute(
            """
            INSERT INTO openclaw_human_action_redemptions (
                reference_id, callback_id_sha256, callback_message_id, redeemed_at
            ) VALUES (?, ?, 200, '1970-01-01T00:18:20+00:00')
            """,
            (reference_id, hashlib.sha256(f"callback-{suffix}".encode()).hexdigest()),
        )
    conn.execute(
        """
        INSERT INTO parser_human_draft_action_bindings (
            reference_id, card_generation_public_id, draft_id, parser_output_id,
            proposal_version, proposal_content_hash, authenticated_actor_id,
            telegram_account_id, telegram_conversation_id,
            conversation_binding_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1100)
        """,
        (
            reference_id,
            card_generation_public_id,
            card["draft_id"],
            card["decision_target_parser_output_id"],
            card["decision_target_proposal_version"],
            card["decision_target_proposal_content_hash"],
            card["authenticated_actor_id"],
            card["telegram_account_id"],
            card["telegram_conversation_id"],
            card["conversation_binding_id"],
        ),
    )
    return reference_public_id


def _start_second_context(conn: sqlite3.Connection):
    from finance_core.parser_proposals.human_drafts import begin_human_draft_in_transaction

    source = conn.execute(
        "SELECT * FROM parser_outputs WHERE public_id = 'prop_d1_source'"
    ).fetchone()
    assert source is not None
    content_hash = conn.execute(
        "SELECT decision_target_proposal_content_hash FROM parser_human_drafts LIMIT 1"
    ).fetchone()[0]
    reference_material = b"second-context-edit-reference"
    redemption_hash = hashlib.sha256(b"second-context-callback").hexdigest()
    conn.execute(
        """
        INSERT INTO openclaw_human_action_references (
            reference_public_id, reference_sha256, issuance_idempotency_key,
            parser_output_id, action, proposal_version, proposal_content_hash,
            authenticated_actor_id, channel, channel_account_id,
            channel_conversation_id, conversation_binding_id, ttl_seconds,
            expires_at, issued_at
        ) VALUES (?, ?, ?, ?, 'edit', 0, ?, '111', 'telegram', 'acct-second',
                  '111', 'binding-second', 600, 2000,
                  '1970-01-01T00:16:40+00:00')
        """,
        (
            "haref_" + "9" * 32,
            hashlib.sha256(reference_material).hexdigest(),
            "bridge-human-action-issue:" + "9" * 32,
            source["id"],
            content_hash,
        ),
    )
    conn.commit()
    reference = conn.execute(
        "SELECT * FROM openclaw_human_action_references WHERE reference_public_id = ?",
        ("haref_" + "9" * 32,),
    ).fetchone()
    assert reference is not None
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        INSERT INTO openclaw_human_action_redemptions (
            reference_id, callback_id_sha256, callback_message_id, redeemed_at
        ) VALUES (?, ?, 300, '1970-01-01T00:16:40+00:00')
        """,
        (reference["id"], redemption_hash),
    )
    result = begin_human_draft_in_transaction(
        conn,
        locked_edit_reference_row=reference,
        source_edit_reference_id=reference["id"],
        reference_public_id=reference["reference_public_id"],
        reference_integrity_material=reference_material,
        callback_message_id=300,
        redemption_public_id="d1start_second_context",
        redemption_material_hash=redemption_hash,
        now_epoch=1000,
    )
    conn.commit()
    return result


def test_fully_shaped_forged_terminal_operation_cannot_mutate_head() -> None:
    """Removing terminal decision evidence checks permits a forged terminal head."""
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_terminal_operation(
            conn,
            operation_public_id="forged-terminal-operation",
            operation_type="rejected",
            decision_public_id="forged-decision",
        )
    conn.rollback()
    assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "active"
    conn.close()


def test_confirmed_terminal_operation_requires_redeemed_confirm_reference() -> None:
    """Removing the Confirm redemption guard admits a bare confirmed operation."""
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    _insert_decision(conn, decision_public_id="decision-confirmed", state="confirmed")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_terminal_operation(
            conn,
            operation_public_id="terminal-confirmed",
            operation_type="confirmed",
            decision_public_id="decision-confirmed",
        )
    conn.rollback()
    assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "active"
    conn.close()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", "decision_missing"),
        ("wrong_actor", "decision_actor"),
        ("wrong_hash", "decision_target"),
        ("wrong_state", "decision_state"),
    ],
)
def test_reject_helper_requires_exact_persisted_rejected_decision(
    mutation: str,
    expected: str,
) -> None:
    """Weakening the helper's decision join accepts missing or mismatched authority."""
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftError,
        reject_active_human_draft_in_transaction,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    target_id = conn.execute(
        "SELECT decision_target_parser_output_id FROM parser_human_drafts"
    ).fetchone()[0]
    if mutation != "missing":
        _insert_decision(
            conn,
            decision_public_id="decision-reject",
            state="confirmed" if mutation == "wrong_state" else "rejected",
            actor_id="222" if mutation == "wrong_actor" else "111",
            proposal_content_hash="f" * 64 if mutation == "wrong_hash" else None,
        )
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(HumanDraftError, match=expected):
        reject_active_human_draft_in_transaction(
            conn,
            parser_output_id=target_id,
            authenticated_actor_id="111",
            decision_public_id="missing-decision" if mutation == "missing" else "decision-reject",
            decision_binding=None,
            now_epoch=1100,
        )
    conn.rollback()
    assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "active"
    conn.close()


def test_real_legacy_rejected_decision_is_terminal_and_exact_replay_is_idempotent() -> None:
    """A real legacy Reject stays supported without a D1 action reference."""
    from finance_core.parser_proposals.human_drafts import reject_active_human_draft_in_transaction

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    target_id = _insert_decision(
        conn, decision_public_id="decision-legacy-reject", state="rejected"
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    reject_active_human_draft_in_transaction(
        conn,
        parser_output_id=target_id,
        authenticated_actor_id="111",
        decision_public_id="decision-legacy-reject",
        decision_binding=None,
        now_epoch=1100,
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    reject_active_human_draft_in_transaction(
        conn,
        parser_output_id=target_id,
        authenticated_actor_id="111",
        decision_public_id="decision-legacy-reject",
        decision_binding=None,
        now_epoch=1200,
    )
    conn.commit()
    assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "rejected"
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == before
    )
    conn.close()


def test_legacy_reject_without_binding_refuses_ambiguous_active_draft_ownership() -> None:
    """Choosing an arbitrary actor-owned draft would terminate the wrong context."""
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftError,
        reject_active_human_draft_in_transaction,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    _start_second_context(conn)
    target_id = _insert_decision(
        conn, decision_public_id="decision-ambiguous-reject", state="rejected"
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(HumanDraftError, match="draft_ownership_ambiguous"):
        reject_active_human_draft_in_transaction(
            conn,
            parser_output_id=target_id,
            authenticated_actor_id="111",
            decision_public_id="decision-ambiguous-reject",
            decision_binding=None,
            now_epoch=1100,
        )
    conn.rollback()
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_drafts WHERE state = 'active'").fetchone()[
            0
        ]
        == 2
    )
    conn.close()


@pytest.mark.parametrize(
    ("action", "redeemed", "expected"),
    [
        ("confirm", True, "reject_binding_invalid"),
        ("reject", False, "reject_binding_invalid"),
    ],
)
def test_d1_reject_requires_redeemed_reject_action(
    action: str,
    redeemed: bool,
    expected: str,
) -> None:
    """Wrong-action or unredeemed references cannot authorize a D1 Reject."""
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftDecisionBinding,
        HumanDraftError,
        reject_active_human_draft_in_transaction,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    reference_public_id = _insert_d1_action_binding(
        conn,
        action=action,
        card_generation_public_id=started.card_generation_public_id,
        suffix="1" if action == "confirm" else "2",
        redeemed=redeemed,
    )
    target_id = _insert_decision(
        conn, decision_public_id="decision-d1-reject-invalid", state="rejected"
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(HumanDraftError, match=expected):
        reject_active_human_draft_in_transaction(
            conn,
            parser_output_id=target_id,
            authenticated_actor_id="111",
            decision_public_id="decision-d1-reject-invalid",
            decision_binding=HumanDraftDecisionBinding(
                reference_public_id,
                started.card_generation_public_id,
                "111",
                "acct",
                "111",
                "binding",
            ),
            now_epoch=1200,
        )
    conn.rollback()
    assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "active"
    conn.close()


def test_valid_older_generation_d1_reject_terminates_current_head() -> None:
    """Requiring Reject's reference card to equal the result card breaks valid old G1 Reject."""
    from finance_core.parser_proposals.human_draft_delivery import (
        begin_human_draft_card_delivery,
        get_human_draft_card,
        reissue_human_draft_card,
    )
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftContext,
        HumanDraftDecisionBinding,
        reject_active_human_draft_in_transaction,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    reference_public_id = _insert_d1_action_binding(
        conn,
        action="reject",
        card_generation_public_id=started.card_generation_public_id,
        suffix="3",
        redeemed=True,
    )
    conn.commit()
    context = HumanDraftContext("111", "acct", "111", "binding")
    attempt_id = hashlib.sha256(
        b"d1-card-delivery-v1\0"
        + (2).to_bytes(4, "big")
        + len(started.card_generation_public_id.encode()).to_bytes(4, "big")
        + started.card_generation_public_id.encode()
        + (5).to_bytes(4, "big")
        + b"reply"
    ).hexdigest()
    begin_human_draft_card_delivery(
        conn,
        context=context,
        card_generation_public_id=started.card_generation_public_id,
        attempt_public_id=attempt_id,
        delivery_material_hash="4" * 64,
        transport_mode="reply",
        outbound_target_message_id=None,
        now_epoch=1001,
    )
    observed = get_human_draft_card(conn, context=context, attempt_public_id=attempt_id)
    recovery_id = hashlib.sha256(
        b"d1-card-recovery-v1\0"
        + (3).to_bytes(4, "big")
        + len(observed.draft_public_id.encode()).to_bytes(4, "big")
        + observed.draft_public_id.encode()
        + len(b"d1start_callback_a").to_bytes(4, "big")
        + b"d1start_callback_a"
        + len(started.card_generation_public_id.encode()).to_bytes(4, "big")
        + started.card_generation_public_id.encode()
    ).hexdigest()
    generation_2 = reissue_human_draft_card(
        conn,
        context=context,
        expected_current_generation_public_id=started.card_generation_public_id,
        original_operation_or_start_public_id="d1start_callback_a",
        recovery_public_id=recovery_id,
        recovery_material_hash="5" * 64,
        queried_delivery_state_hash=observed.delivery_state_hash,
        reason="unknown_after_query",
        now_epoch=1100,
    )
    target_id = _insert_decision(
        conn, decision_public_id="decision-old-g1-reject", state="rejected"
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    reject_active_human_draft_in_transaction(
        conn,
        parser_output_id=target_id,
        authenticated_actor_id="111",
        decision_public_id="decision-old-g1-reject",
        decision_binding=HumanDraftDecisionBinding(
            reference_public_id,
            started.card_generation_public_id,
            "111",
            "acct",
            "111",
            "binding",
        ),
        now_epoch=1200,
    )
    conn.commit()
    operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_type = 'rejected'"
    ).fetchone()
    assert operation["result_card_generation_public_id"] == generation_2.card_generation_public_id
    assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "rejected"
    conn.close()


def test_rejected_decision_uow_rolls_back_when_head_update_fails() -> None:
    """Moving terminal evidence outside the decision UOW leaves orphan authority."""
    from finance_core.parser_proposals.human_drafts import reject_active_human_draft_in_transaction

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    target_id = conn.execute(
        "SELECT decision_target_parser_output_id FROM parser_human_drafts"
    ).fetchone()[0]
    conn.execute(
        """
        CREATE TEMP TRIGGER fail_terminal_head_update
        BEFORE UPDATE ON parser_human_drafts
        WHEN NEW.state = 'rejected'
        BEGIN SELECT RAISE(ABORT, 'injected-head-update-failure'); END
        """
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    _insert_decision(
        conn,
        decision_public_id="decision-rollback",
        state="rejected",
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected-head-update-failure"):
        reject_active_human_draft_in_transaction(
            conn,
            parser_output_id=target_id,
            authenticated_actor_id="111",
            decision_public_id="decision-rollback",
            decision_binding=None,
            now_epoch=1200,
        )
    conn.rollback()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM parser_proposal_authorizations "
            "WHERE confirmation_public_id = 'decision-rollback'"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM parser_human_draft_operations "
            "WHERE operation_public_id = 'decision-rollback'"
        ).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "active"
    assert (
        conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (target_id,)
        ).fetchone()[0]
        == "parsed_pending_confirmation"
    )
    conn.close()


def test_terminal_operation_and_head_transition_must_share_one_transaction() -> None:
    """Splitting terminal evidence from the head transition must fail at commit."""
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    _insert_decision(
        conn,
        decision_public_id="decision-split-transaction",
        state="rejected",
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="mutation shape"):
        conn.execute("UPDATE parser_human_drafts SET state = 'rejected', updated_at = 1100")
    conn.rollback()

    conn.execute("BEGIN IMMEDIATE")
    _insert_terminal_operation(
        conn,
        operation_public_id="decision-split-transaction",
        operation_type="rejected",
        decision_public_id="decision-split-transaction",
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.commit()
    conn.rollback()

    assert conn.execute("SELECT state FROM parser_human_drafts").fetchone()[0] == "active"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM parser_human_draft_operations "
            "WHERE operation_public_id = 'decision-split-transaction'"
        ).fetchone()[0]
        == 0
    )
    conn.close()


def test_multiple_refused_operations_can_share_one_historical_result_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Making result-card closure one-to-one breaks valid append-only refusal history."""
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)

    def refuse(*_args, **_kwargs):
        raise human_drafts._HumanDraftRefusal("D1_DATE_INVALID")

    monkeypatch.setattr(human_drafts, "_validate_human_draft_adapter", refuse)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    for message_id in (101, 102):
        result = apply_human_draft_card(
            conn,
            HumanDraftCommand(
                started.card_generation_public_id,
                message_id,
                f"d1op-refused-{message_id}",
                "111",
                "acct",
                "111",
                "binding",
                text,
                fields,
            ),
            publish=lambda *_: None,
        )
        assert result.operation_outcome == "refused"
        assert result.card_generation_public_id == started.card_generation_public_id
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_cards").fetchone()[0] == 1
    assert (
        conn.execute(
            """
        SELECT COUNT(*) FROM parser_human_draft_operations
        WHERE result_card_generation_public_id = ?
        """,
            (started.card_generation_public_id,),
        ).fetchone()[0]
        == 3
    )
    conn.close()


# ---------------------------------------------------------------------------
# Task 3: deterministic whole-card validation
# ---------------------------------------------------------------------------


def _validation_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent": "personal_expense",
        "amount": "12.50",
        "currency": "USD",
        "transaction_date": "2026-09-13",
        "merchant": "Cafe",
        "description": "Lunch",
        "category": "food",
    }
    payload.update(updates)
    return payload


def _reason_contributor(
    reason_code: str,
    *,
    origin_kind: str = "deterministic",
    flags: tuple[str, ...],
    affected_fields: tuple[str, ...],
    resolution_policy: str,
):
    from finance_core.parser_proposals.human_drafts import HumanReasonContributor

    evidence_required = origin_kind in {"ocr", "ai_observation"}
    return HumanReasonContributor(
        contributor_id=f"reason:{reason_code}",
        reason_code=reason_code,
        origin_kind=origin_kind,
        source_evidence_public_id="source_evidence_1" if evidence_required else None,
        source_evidence_hash="a" * 64 if evidence_required else None,
        flags=flags,
        affected_fields=affected_fields,
        resolution_policy=resolution_policy,
        resolved_by_operation_id=None,
        resolved_fields=(),
        resolution_before_after={},
    )


def test_validation_contract_exposes_only_frozen_result_fields() -> None:
    from dataclasses import fields

    from finance_core.parser_proposals.human_draft_validation import ValidatedHumanDraft

    assert [field.name for field in fields(ValidatedHumanDraft)] == [
        "canonical_payload",
        "changed_fields",
        "completeness",
        "reason_contributors",
        "unresolved_flags",
        "explicit_clears",
    ]


@pytest.mark.parametrize(
    ("amount", "code"),
    [
        ("-0", "D1_AMOUNT_SYNTAX"),
        ("-1", "D1_AMOUNT_SYNTAX"),
        ("+12", "D1_AMOUNT_SYNTAX"),
        (".50", "D1_AMOUNT_SYNTAX"),
        ("12.", "D1_AMOUNT_SYNTAX"),
        ("１２", "D1_AMOUNT_SYNTAX"),
        ("1e2", "D1_AMOUNT_SYNTAX"),
        ("NaN", "D1_AMOUNT_SYNTAX"),
        ("Infinity", "D1_AMOUNT_SYNTAX"),
        ("1 2", "D1_AMOUNT_SYNTAX"),
        ("1" * 19, "D1_AMOUNT_LIMIT"),
        ("1.1234567", "D1_AMOUNT_LIMIT"),
        ("0", "D1_AMOUNT_NONPOSITIVE"),
        ("00.000000", "D1_AMOUNT_NONPOSITIVE"),
    ],
)
def test_validation_amount_refusal_precedence(amount: str, code: str) -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    with pytest.raises(HumanDraftValidationError) as caught:
        validate_human_draft(
            _validation_payload(currency=None, amount=None),
            {"amount": amount},
            source_type="telegram_text",
            reason_contributors=(),
            operation_public_id="d1op_amount",
        )
    assert caught.value.code == code


def test_validation_currencyless_amount_is_bounded_canonical_and_incomplete() -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    maximum = "999999999999999999.999999"
    result = validate_human_draft(
        _validation_payload(amount=None, currency=None),
        {"amount": maximum},
        source_type="telegram_text",
        reason_contributors=(),
        operation_public_id="d1op_lexical_maximum",
    )
    assert result.canonical_payload["amount"] == maximum
    assert result.canonical_payload["currency"] is None
    assert result.completeness == "incomplete"

    normalized = validate_human_draft(
        _validation_payload(amount=None, currency=None),
        {"amount": "00012.00"},
        source_type="telegram_text",
        reason_contributors=(),
        operation_public_id="d1op_zero_normalization",
    )
    assert normalized.canonical_payload["amount"] == "12"
    assert normalized.changed_fields == ("amount",)
    assert (
        validate_human_draft(
            _validation_payload(amount=None, currency=None),
            {"amount": "0.500000"},
            source_type="telegram_text",
            reason_contributors=(),
            operation_public_id="d1op_fraction_normalization",
        ).canonical_payload["amount"]
        == "0.5"
    )


@pytest.mark.parametrize("amount", ["0000000000000000001", "0000000000000000000.000001"])
def test_validation_applies_amount_limits_before_zero_stripping(amount: str) -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    with pytest.raises(HumanDraftValidationError) as caught:
        validate_human_draft(
            _validation_payload(amount=None, currency=None),
            {"amount": amount},
            source_type="telegram_text",
            reason_contributors=(),
            operation_public_id="d1op_leading_zero_limit",
        )
    assert caught.value.code == "D1_AMOUNT_LIMIT"


def test_validation_revalidates_simultaneous_and_currency_only_target_money() -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    staged = _validation_payload(amount="12.34", currency=None)
    with pytest.raises(HumanDraftValidationError) as caught:
        validate_human_draft(
            staged,
            {"currency": "JPY"},
            source_type="telegram_text",
            reason_contributors=(),
            operation_public_id="d1op_jpy_scale",
        )
    assert caught.value.code == "D1_MONEY_INVALID"
    usd = validate_human_draft(
        staged,
        {"currency": "usd"},
        source_type="telegram_text",
        reason_contributors=(),
        operation_public_id="d1op_usd_scale",
    )
    assert usd.canonical_payload["amount"] == "12.34"
    assert usd.canonical_payload["currency"] == "USD"
    assert usd.completeness == "publishable"

    jpy = validate_human_draft(
        _validation_payload(amount="12", currency=None),
        {"amount": "12", "currency": "JPY"},
        source_type="telegram_text",
        reason_contributors=(),
        operation_public_id="d1op_jpy_pair",
    )
    assert jpy.canonical_payload["amount"] == "12"
    usd_integer = validate_human_draft(
        _validation_payload(amount="12", currency=None),
        {"amount": "12", "currency": "USD"},
        source_type="telegram_text",
        reason_contributors=(),
        operation_public_id="d1op_usd_pair",
    )
    assert usd_integer.canonical_payload["amount"] == "12.00"
    assert usd_integer.changed_fields == ("currency",)


def test_validation_accepts_large_finite_supported_money_within_d1_bounds() -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    result = validate_human_draft(
        _validation_payload(amount=None, currency=None),
        {"amount": "999999999999999999.99", "currency": "USD"},
        source_type="telegram_text",
        reason_contributors=(),
        operation_public_id="d1op_large_money",
    )
    assert result.canonical_payload["amount"] == "999999999999999999.99"
    assert result.completeness == "publishable"


@pytest.mark.parametrize(
    "transaction_date",
    ["2026-9-13", "2026/09/13", "2026-09-13T00:00:00", "2026-02-30"],
)
def test_validation_requires_strict_real_calendar_date(transaction_date: str) -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    with pytest.raises(HumanDraftValidationError) as caught:
        validate_human_draft(
            _validation_payload(),
            {"transaction_date": transaction_date},
            source_type="telegram_text",
            reason_contributors=(),
            operation_public_id="d1op_date",
        )
    assert caught.value.code == "D1_DATE_INVALID"


def test_validation_text_and_receipt_completeness_and_explicit_clears() -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    text = validate_human_draft(
        _validation_payload(merchant="Cafe", description="Lunch"),
        {"merchant": "", "description": " memo "},
        source_type="telegram_text",
        reason_contributors=(),
        operation_public_id="d1op_text_description",
    )
    assert text.canonical_payload["merchant"] == ""
    assert text.canonical_payload["description"] == "memo"
    assert text.completeness == "publishable"
    assert text.explicit_clears == {"merchant": ("Cafe", "")}

    receipt = validate_human_draft(
        _validation_payload(description="Lunch", category=None),
        {"description": "", "category": ""},
        source_type="telegram_image",
        reason_contributors=(),
        operation_public_id="d1op_receipt_clear",
    )
    assert receipt.canonical_payload["description"] is None
    assert receipt.canonical_payload["category"] is None
    assert receipt.explicit_clears == {"description": ("Lunch", None)}
    assert receipt.completeness == "publishable"
    missing_merchant = validate_human_draft(
        _validation_payload(merchant="Cafe"),
        {"merchant": "", "description": "still present"},
        source_type="telegram_image",
        reason_contributors=(),
        operation_public_id="d1op_receipt_merchant",
    )
    assert missing_merchant.completeness == "incomplete"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("merchant", "m" * 1025),
        ("description", "é" * 513),
        ("category", "c" * 129),
        ("merchant", "bad\x00value"),
        ("description", "bad\u2028value"),
        ("category", "\ud800"),
    ],
)
def test_validation_rejects_text_bounds_and_invalid_scalars(field: str, value: str) -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    with pytest.raises(HumanDraftValidationError) as caught:
        validate_human_draft(
            _validation_payload(),
            {field: value},
            source_type="telegram_text",
            reason_contributors=(),
            operation_public_id="d1op_text_bounds",
        )
    assert caught.value.code in {"D1_TEXT_INVALID", "D1_TEXT_LIMIT"}


def test_validation_rejects_unsupported_keys_and_detects_canonical_noop() -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    with pytest.raises(HumanDraftValidationError) as caught:
        validate_human_draft(
            _validation_payload(),
            {"account": "cash"},
            source_type="telegram_text",
            reason_contributors=(),
            operation_public_id="d1op_account",
        )
    assert caught.value.code == "D1_FIELD_UNSUPPORTED"

    no_op = validate_human_draft(
        _validation_payload(),
        {
            "amount": "12.5",
            "currency": "usd",
            "transaction_date": "2026-09-13",
            "merchant": " Cafe ",
            "description": "Lunch",
            "category": "food",
        },
        source_type="telegram_text",
        reason_contributors=(),
        operation_public_id="d1op_noop",
    )
    assert no_op.changed_fields == ()
    assert no_op.canonical_payload["amount"] == "12.50"


def test_receipt_reason_policy_inventory_is_exhaustive() -> None:
    from finance_core.parser_proposals.human_draft_validation import RECEIPT_REASON_POLICY
    from finance_core.parser_proposals.receipt_total_parser import RECEIPT_AMBIGUITY_FLAGS

    assert set(RECEIPT_REASON_POLICY) == set(RECEIPT_AMBIGUITY_FLAGS)
    assert len(RECEIPT_REASON_POLICY) == 14


def test_initial_receipt_contributors_require_verified_relational_evidence() -> None:
    from finance_core.parser_proposals.human_draft_validation import initial_reason_contributors

    payload = _validation_payload(
        ambiguity_flags=["total_not_found"],
        ocr_evidence={
            "extraction_public_id": "rocr_source",
            "normalized_result_hash": "a" * 64,
            "extraction_status": "succeeded",
        },
    )
    unverified = initial_reason_contributors(payload, source_type="telegram_image")
    assert unverified[0].origin_kind == "unknown"
    assert unverified[0].resolution_policy == "never_discharge"

    verified = initial_reason_contributors(
        payload,
        source_type="telegram_image",
        verified_ocr_evidence={
            "extraction_public_id": "rocr_source",
            "normalized_result_hash": "a" * 64,
            "extraction_status": "succeeded",
        },
    )
    assert verified[0].reason_code == "total_not_found"
    assert verified[0].origin_kind == "ocr"
    assert verified[0].source_evidence_public_id == "rocr_source"
    assert verified[0].source_evidence_hash == "a" * 64

    inherited = initial_reason_contributors(
        {key: value for key, value in payload.items() if key != "ocr_evidence"},
        source_type="telegram_image",
        verified_ocr_evidence={
            "extraction_public_id": "rocr_source",
            "normalized_result_hash": "a" * 64,
            "extraction_status": "succeeded",
        },
        verified_ai_observations={},
    )
    assert inherited[0].origin_kind == "ocr"
    assert inherited[0].source_evidence_public_id == "rocr_source"


def test_initial_verified_ai_observation_consumes_its_shared_conflict_flag() -> None:
    from finance_core.parser_proposals.human_draft_validation import initial_reason_contributors

    contributors = initial_reason_contributors(
        _validation_payload(ambiguity_flags=["ambiguous_merchant", "source_conflict"]),
        source_type="telegram_text",
        verified_ai_observations={"ambiguous_merchant": ("air_source", "b" * 64)},
    )
    assert len(contributors) == 1
    assert contributors[0].reason_code == "ambiguous_merchant"
    assert contributors[0].origin_kind == "ai_observation"
    assert contributors[0].flags == ("ambiguous_merchant", "source_conflict")


@pytest.mark.parametrize(
    ("reason_code", "origin", "flags", "affected", "policy", "updates"),
    [
        (
            "total_not_found",
            "ocr",
            ("missing_amount",),
            ("amount",),
            "explicit_valid_amount",
            {"amount": "13"},
        ),
        (
            "missing_amount",
            "deterministic",
            ("missing_amount",),
            ("amount",),
            "explicit_valid_amount",
            {"amount": "13"},
        ),
        (
            "conflicting_total_candidates",
            "ocr",
            ("ambiguous_amount", "source_conflict"),
            ("amount",),
            "explicit_material_amount_pair",
            {"amount": "13"},
        ),
        (
            "conflicting_text_candidates",
            "deterministic",
            ("ambiguous_amount", "source_conflict"),
            ("amount",),
            "explicit_material_amount_pair",
            {"amount": "13"},
        ),
        (
            "currency_not_determined",
            "ocr",
            ("missing_currency",),
            ("currency",),
            "explicit_supported_currency",
            {"currency": "USD"},
        ),
        (
            "ambiguous_currency_symbol",
            "ocr",
            ("ambiguous_currency", "source_conflict"),
            ("currency",),
            "explicit_supported_currency",
            {"currency": "USD"},
        ),
        (
            "unsupported_currency_for_amount",
            "ocr",
            ("ambiguous_currency", "source_conflict"),
            ("amount", "currency"),
            "explicit_target_money_pair",
            {"amount": "13", "currency": "USD"},
        ),
        (
            "transaction_date_not_found",
            "ocr",
            ("missing_date",),
            ("transaction_date",),
            "explicit_valid_date",
            {"transaction_date": "2026-09-14"},
        ),
        (
            "ambiguous_transaction_date",
            "ocr",
            ("ambiguous_date", "source_conflict"),
            ("transaction_date",),
            "explicit_valid_date",
            {"transaction_date": "2026-09-14"},
        ),
        (
            "conflicting_date_candidates",
            "ocr",
            ("ambiguous_date", "source_conflict"),
            ("transaction_date",),
            "explicit_valid_date",
            {"transaction_date": "2026-09-14"},
        ),
        (
            "merchant_not_determined",
            "ocr",
            ("missing_merchant_or_description",),
            ("merchant",),
            "explicit_nonempty_merchant",
            {"merchant": "Shop"},
        ),
        (
            "ambiguous_merchant",
            "ai_observation",
            ("ambiguous_merchant", "source_conflict"),
            ("merchant",),
            "explicit_nonempty_merchant",
            {"merchant": "Shop"},
        ),
        (
            "missing_merchant_or_description",
            "deterministic",
            ("missing_merchant_or_description",),
            ("merchant", "description"),
            "explicit_text_merchant_or_description",
            {"description": "memo"},
        ),
    ],
)
def test_validation_reason_policy_positive_matrix(
    reason_code: str,
    origin: str,
    flags: tuple[str, ...],
    affected: tuple[str, ...],
    policy: str,
    updates: dict[str, str],
) -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    payload = _validation_payload()
    for field in affected:
        if field == "amount":
            payload[field] = (
                None if "missing" in reason_code or reason_code == "total_not_found" else "12.50"
            )
        elif field == "currency":
            payload[field] = None if reason_code == "currency_not_determined" else "EUR"
        elif field == "transaction_date":
            payload[field] = None if reason_code == "transaction_date_not_found" else "2026-09-13"
        elif field in {"merchant", "description"}:
            payload[field] = None
    contributor = _reason_contributor(
        reason_code,
        origin_kind=origin,
        flags=flags,
        affected_fields=affected,
        resolution_policy=policy,
    )
    result = validate_human_draft(
        payload,
        updates,
        source_type="telegram_image" if origin == "ocr" else "telegram_text",
        reason_contributors=(contributor,),
        operation_public_id=f"d1op_{reason_code}",
    )
    resolved = result.reason_contributors[0]
    assert resolved.resolved_by_operation_id == f"d1op_{reason_code}"
    assert set(resolved.resolved_fields) <= set(updates)
    assert set(resolved.resolution_before_after) == set(resolved.resolved_fields)
    assert result.unresolved_flags == ()


def test_validation_reason_resolution_requires_material_explicit_valid_fields() -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    contributor = _reason_contributor(
        "unsupported_currency_for_amount",
        origin_kind="ocr",
        flags=("ambiguous_currency", "source_conflict"),
        affected_fields=("amount", "currency"),
        resolution_policy="explicit_target_money_pair",
    )
    copied = validate_human_draft(
        _validation_payload(),
        {"amount": "12.50", "currency": "USD"},
        source_type="telegram_image",
        reason_contributors=(contributor,),
        operation_public_id="d1op_copied",
    )
    assert copied.reason_contributors == (contributor,)
    assert copied.unresolved_flags == ("ambiguous_currency", "source_conflict")
    assert copied.completeness == "incomplete"

    amount_only = validate_human_draft(
        _validation_payload(currency=None),
        {"amount": "13"},
        source_type="telegram_image",
        reason_contributors=(contributor,),
        operation_public_id="d1op_incomplete_pair",
    )
    assert amount_only.reason_contributors[0].resolved_by_operation_id is None
    assert amount_only.completeness == "incomplete"


def test_validation_shared_source_conflict_clears_only_after_every_contributor() -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    amount_reason = _reason_contributor(
        "conflicting_text_candidates",
        flags=("ambiguous_amount", "source_conflict"),
        affected_fields=("amount",),
        resolution_policy="explicit_material_amount_pair",
    )
    date_reason = _reason_contributor(
        "ambiguous_date",
        origin_kind="ai_observation",
        flags=("ambiguous_date", "source_conflict"),
        affected_fields=("transaction_date",),
        resolution_policy="explicit_valid_date",
    )
    first = validate_human_draft(
        _validation_payload(),
        {"amount": "13"},
        source_type="telegram_text",
        reason_contributors=(amount_reason, date_reason),
        operation_public_id="d1op_amount_first",
    )
    assert first.reason_contributors[0].resolved_by_operation_id == "d1op_amount_first"
    assert first.reason_contributors[1].resolved_by_operation_id is None
    assert first.unresolved_flags == ("ambiguous_date", "source_conflict")
    second = validate_human_draft(
        first.canonical_payload,
        {"transaction_date": "2026-09-14"},
        source_type="telegram_text",
        reason_contributors=first.reason_contributors,
        operation_public_id="d1op_date_second",
    )
    assert second.reason_contributors[1].resolved_by_operation_id == "d1op_date_second"
    assert second.unresolved_flags == ()


@pytest.mark.parametrize("origin", ["model_only", "unknown"])
def test_validation_model_only_and_unknown_reasons_never_discharge(origin: str) -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    contributor = _reason_contributor(
        "amount_looks_wrong",
        origin_kind=origin,
        flags=("original_unknown_flag",),
        affected_fields=("amount",),
        resolution_policy="never_discharge",
    )
    result = validate_human_draft(
        _validation_payload(),
        {"amount": "13", "currency": "USD"},
        source_type="telegram_text",
        reason_contributors=(contributor,),
        operation_public_id="d1op_unattributed",
    )
    assert result.reason_contributors == (contributor,)
    assert result.unresolved_flags == ("original_unknown_flag",)
    assert result.completeness == "incomplete"


def test_validation_unsupported_intent_and_unverifiable_origin_fail_closed() -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    unsupported = _reason_contributor(
        "unsupported_intent",
        flags=("unsupported_intent",),
        affected_fields=(),
        resolution_policy="never_discharge",
    )
    result = validate_human_draft(
        _validation_payload(intent="transfer"),
        {"merchant": "Shop"},
        source_type="telegram_text",
        reason_contributors=(unsupported,),
        operation_public_id="d1op_intent",
    )
    assert result.completeness == "incomplete"
    assert result.unresolved_flags == ("unsupported_intent",)

    bad_origin = type(unsupported)(**{**unsupported.__dict__, "origin_kind": "provider_guess"})
    with pytest.raises(HumanDraftValidationError) as caught:
        validate_human_draft(
            _validation_payload(),
            {"merchant": "Shop"},
            source_type="telegram_text",
            reason_contributors=(bad_origin,),
            operation_public_id="d1op_bad_origin",
        )
    assert caught.value.code == "D1_REASON_UNVERIFIABLE"


@pytest.mark.parametrize(
    ("updates", "expected_code"),
    [
        ({"amount": "12.50"}, None),
        ({"amount": ""}, None),
        ({"amount": "-1"}, "D1_AMOUNT_SYNTAX"),
        ({"amount": "1" * 19}, "D1_AMOUNT_LIMIT"),
        ({"amount": "0.001"}, "D1_MONEY_INVALID"),
        ({"amount": "0"}, "D1_AMOUNT_NONPOSITIVE"),
        ({"amount": "13", "currency": "ZZZ"}, "D1_CURRENCY_INVALID"),
        ({"amount": "0.01", "currency": "JPY"}, "D1_MONEY_INVALID"),
    ],
)
def test_total_amount_invalid_requires_material_valid_target_pair(
    updates: dict[str, str], expected_code: str | None
) -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    contributor = _reason_contributor(
        "total_amount_invalid",
        origin_kind="ocr",
        flags=("total_amount_invalid",),
        affected_fields=("amount",),
        resolution_policy="explicit_material_amount_pair",
    )
    if expected_code is not None:
        with pytest.raises(HumanDraftValidationError) as caught:
            validate_human_draft(
                _validation_payload(),
                updates,
                source_type="telegram_image",
                reason_contributors=(contributor,),
                operation_public_id="d1op_invalid_total",
            )
        assert caught.value.code == expected_code
        return
    result = validate_human_draft(
        _validation_payload(),
        updates,
        source_type="telegram_image",
        reason_contributors=(contributor,),
        operation_public_id="d1op_invalid_total",
    )
    assert result.reason_contributors == (contributor,)
    assert result.unresolved_flags == ("total_amount_invalid",)


def test_total_amount_invalid_valid_resolution_preserves_ocr_identity() -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    contributor = _reason_contributor(
        "total_amount_invalid",
        origin_kind="ocr",
        flags=("total_amount_invalid",),
        affected_fields=("amount",),
        resolution_policy="explicit_material_amount_pair",
    )
    result = validate_human_draft(
        _validation_payload(),
        {"amount": "13", "currency": "USD"},
        source_type="telegram_image",
        reason_contributors=(contributor,),
        operation_public_id="d1op_fix_invalid_total",
    )
    resolved = result.reason_contributors[0]
    assert resolved.source_evidence_public_id == contributor.source_evidence_public_id
    assert resolved.source_evidence_hash == contributor.source_evidence_hash
    assert resolved.flags == ("total_amount_invalid",)
    assert resolved.affected_fields == ("amount",)
    assert resolved.resolved_by_operation_id == "d1op_fix_invalid_total"
    assert resolved.resolution_before_after == {"amount": ("12.50", "13.00")}
    assert result.unresolved_flags == ()
    assert result.completeness == "publishable"


@pytest.mark.parametrize(
    "reason_code",
    ["ocr_no_text", "ocr_unsupported_input", "ocr_engine_failed", "ocr_resource_rejected"],
)
def test_ocr_source_failures_explicitly_never_discharge(reason_code: str) -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        RECEIPT_REASON_POLICY,
        validate_human_draft,
    )

    policy = RECEIPT_REASON_POLICY[reason_code]
    contributor = _reason_contributor(
        reason_code,
        origin_kind="ocr",
        flags=(reason_code,),
        affected_fields=(),
        resolution_policy=policy.resolution_policy,
    )
    result = validate_human_draft(
        _validation_payload(),
        {
            "amount": "13",
            "currency": "USD",
            "transaction_date": "2026-09-14",
            "merchant": "Shop",
        },
        source_type="telegram_image",
        reason_contributors=(contributor,),
        operation_public_id=f"d1op_{reason_code}",
    )
    assert result.reason_contributors == (contributor,)
    assert result.unresolved_flags == (reason_code,)
    assert result.completeness == "incomplete"


def test_repository_maps_publishable_validation_and_persists_clear_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id, merchant="Cafe 2")
    fields["currency"] = "USD"
    fields["description"] = ""
    text = text.replace("币种：MYR", "币种：USD")
    text = text.replace("描述: Lunch: set", "描述:")
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op_real_validation",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    result = apply_human_draft_card(conn, command, publish=_valid_child_publisher)
    assert result.completeness == "complete"
    operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
        (command.operation_public_id,),
    ).fetchone()
    assert json.loads(operation["explicit_clears_json"]) == {"description": ["Lunch", ""]}
    assert json.loads(operation["material_changes_json"])["description"] == ["Lunch", ""]
    _assert_insert_or_replace_refused(conn, ("parser_human_draft_publications",))
    conn.close()


def test_repository_catches_only_typed_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_draft_validation import HumanDraftValidationError
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op_typed_refusal",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )

    def typed_refusal(*args, **kwargs):
        raise HumanDraftValidationError("D1_DATE_INVALID")

    monkeypatch.setattr(
        "finance_core.parser_proposals.human_draft_validation.validate_human_draft", typed_refusal
    )
    refused = apply_human_draft_card(conn, command, publish=lambda *_: None)
    assert refused.operation_outcome == "refused"
    assert refused.refusal_code == "D1_DATE_INVALID"
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0] == 1
    conn.close()
    monkeypatch.undo()

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    text, fields = _card_text(started.card_generation_public_id)
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op_unexpected_validation",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)

    def unexpected(*args, **kwargs):
        raise RuntimeError("unexpected validator defect")

    monkeypatch.setattr(
        "finance_core.parser_proposals.human_draft_validation.validate_human_draft", unexpected
    )
    with pytest.raises(RuntimeError, match="unexpected validator defect"):
        apply_human_draft_card(conn, command, publish=lambda *_: None)
    assert conn.execute("SELECT COUNT(*) FROM parser_human_draft_reply_evidence").fetchone()[0] == 0
    assert conn.execute("SELECT current_draft_version FROM parser_human_drafts").fetchone()[0] == 0
    conn.close()


def test_validation_currency_without_amount_is_canonical_but_incomplete() -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    result = validate_human_draft(
        _validation_payload(amount=None, currency=None),
        {"currency": " usd "},
        source_type="telegram_text",
        reason_contributors=(),
        operation_public_id="d1op_currency_only",
    )
    assert result.canonical_payload["amount"] is None
    assert result.canonical_payload["currency"] == "USD"
    assert result.changed_fields == ("currency",)
    assert result.completeness == "incomplete"


def test_start_completeness_uses_money_and_source_specific_validation() -> None:
    unsupported = _connection()
    apply_migration_paths(unsupported, TEMP_DB_MIGRATION_PATHS)
    unsupported_result = _start(unsupported)
    assert unsupported_result.completeness == "incomplete"
    unsupported.close()

    receipt = _connection()
    apply_migration_paths(receipt, TEMP_DB_MIGRATION_PATHS)
    receipt_result = _start(
        receipt,
        source_type="telegram_image",
        payload=_validation_payload(
            merchant=None,
            description="Receipt text is not a merchant substitute",
        ),
    )
    assert receipt_result.completeness == "incomplete"
    receipt.close()


def test_validation_verified_ai_evidence_is_required_for_discharge() -> None:
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    valid = _reason_contributor(
        "missing_date",
        origin_kind="ai_observation",
        flags=("missing_date",),
        affected_fields=("transaction_date",),
        resolution_policy="explicit_valid_date",
    )
    invalid = type(valid)(**{**valid.__dict__, "source_evidence_hash": None})
    with pytest.raises(HumanDraftValidationError) as caught:
        validate_human_draft(
            _validation_payload(transaction_date=None),
            {"transaction_date": "2026-09-14"},
            source_type="telegram_text",
            reason_contributors=(invalid,),
            operation_public_id="d1op_ai_unverified",
        )
    assert caught.value.code == "D1_REASON_UNVERIFIABLE"


def test_validation_empty_field_cannot_discharge_missing_text_reason() -> None:
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    contributor = _reason_contributor(
        "missing_merchant_or_description",
        flags=("missing_merchant_or_description",),
        affected_fields=("merchant", "description"),
        resolution_policy="explicit_text_merchant_or_description",
    )
    result = validate_human_draft(
        _validation_payload(merchant=None, description=None),
        {"merchant": ""},
        source_type="telegram_text",
        reason_contributors=(contributor,),
        operation_public_id="d1op_empty_merchant",
    )
    assert result.reason_contributors == (contributor,)
    assert result.unresolved_flags == ("missing_merchant_or_description",)
    assert result.completeness == "incomplete"


def test_validation_canonical_hash_identity_does_not_replace_raw_replay_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftCommand,
        HumanDraftError,
        apply_human_draft_card,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    payload = _validation_payload()
    started = _start(conn, payload=payload)
    fields = {
        "amount": "00012.500000",
        "currency": "USD",
        "transaction_date": "2026-09-13",
        "merchant": "Cafe",
        "description": "Lunch",
        "category": "food",
    }
    text = (
        f"Card Ref: {started.card_generation_public_id}\n"
        f"Amount: {fields['amount']}\nCurrency: USD\nDate: 2026-09-13\n"
        "Merchant: Cafe\nDescription: Lunch\nCategory: food"
    )
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    command = HumanDraftCommand(
        started.card_generation_public_id,
        101,
        "d1op_canonical_noop",
        "111",
        "acct",
        "111",
        "binding",
        text,
        fields,
    )
    result = apply_human_draft_card(conn, command, publish=lambda *_: None)
    assert result.operation_outcome == "noop"
    assert result.draft_content_hash == started.draft_content_hash
    changed_text = text.replace("00012.500000", "12.5000")
    changed_fields = {**fields, "amount": "12.5000"}
    with pytest.raises(HumanDraftError, match="operation_conflict"):
        apply_human_draft_card(
            conn,
            HumanDraftCommand(
                **{
                    **command.__dict__,
                    "raw_card_text": changed_text,
                    "field_values": changed_fields,
                }
            ),
            publish=lambda *_: None,
        )
    conn.close()


def test_legacy_date_full_card_noop_then_real_date_edit_publishes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy ``date`` is the inherited canonical transaction date, not a missing value."""
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(
        conn,
        payload={
            "intent": "personal_expense",
            "amount": "12.50",
            "currency": "USD",
            "date": "2026-09-13",
            "merchant": "Kopitiam",
            "description": "Lunch",
            "category": "food",
        },
    )
    fields = dict(started.field_values)

    def card_text(values: dict[str, str]) -> str:
        return (
            f"资料卡编号：{started.card_generation_public_id}\r\n"
            f"金额: {values['amount']}\r\n币种：{values['currency']}\r\n"
            f"日期: {values['transaction_date']}\r\n商户: {values['merchant']}\r\n"
            f"描述: {values['description']}\r\n分类: {values['category']}"
        )

    publisher_calls = 0

    def publisher(conn, payload, context):
        nonlocal publisher_calls
        publisher_calls += 1
        return _valid_child_publisher(conn, payload, context)

    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    unchanged = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op-legacy-date-noop",
            "111",
            "acct",
            "111",
            "binding",
            card_text(fields),
            fields,
        ),
        publish=publisher,
    )
    assert unchanged.operation_outcome == "noop"
    assert unchanged.draft_version == started.draft_version
    assert unchanged.draft_content_hash == started.draft_content_hash
    assert unchanged.card_generation_public_id == started.card_generation_public_id
    assert unchanged.proposal_public_id == started.proposal_public_id
    assert publisher_calls == 0

    edited_fields = {**fields, "transaction_date": "2026-09-14"}
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1002)
    edited = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            102,
            "d1op-legacy-date-edit",
            "111",
            "acct",
            "111",
            "binding",
            card_text(edited_fields),
            edited_fields,
        ),
        publish=publisher,
    )
    assert edited.operation_outcome == "accepted"
    assert edited.field_values["transaction_date"] == "2026-09-14"
    assert publisher_calls == 1
    material_changes = conn.execute(
        "SELECT material_changes_json FROM parser_human_draft_operations "
        "WHERE operation_public_id = 'd1op-legacy-date-edit'"
    ).fetchone()[0]
    assert json.loads(material_changes) == {"transaction_date": ["2026-09-13", "2026-09-14"]}
    conn.close()


@pytest.mark.parametrize("first_field", ["amount", "currency"])
def test_validation_resolves_staged_missing_money_contributors_in_either_order(
    first_field: str,
) -> None:
    """Requiring a complete pair for each missing-field reason deadlocks staged edits."""
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    amount_reason = _reason_contributor(
        "missing_amount",
        flags=("missing_amount",),
        affected_fields=("amount",),
        resolution_policy="explicit_valid_amount",
    )
    currency_reason = _reason_contributor(
        "missing_currency",
        flags=("missing_currency",),
        affected_fields=("currency",),
        resolution_policy="explicit_supported_currency",
    )
    first_value = "12.50" if first_field == "amount" else "USD"
    second_field = "currency" if first_field == "amount" else "amount"
    second_value = "USD" if second_field == "currency" else "12.50"
    first = validate_human_draft(
        _validation_payload(amount=None, currency=None),
        {first_field: first_value},
        source_type="telegram_text",
        reason_contributors=(amount_reason, currency_reason),
        operation_public_id=f"d1op_{first_field}_first",
    )
    assert first.completeness == "incomplete"
    assert first.reason_contributors[0 if first_field == "amount" else 1].resolved_by_operation_id
    second = validate_human_draft(
        first.canonical_payload,
        {second_field: second_value},
        source_type="telegram_text",
        reason_contributors=first.reason_contributors,
        operation_public_id=f"d1op_{second_field}_second",
    )
    assert second.unresolved_flags == ()
    assert second.completeness == "publishable"


def test_validation_treats_receipt_empty_optional_snapshot_as_canonical_noop() -> None:
    """Canonical receipt absence must not fabricate a human clear or revision."""
    from finance_core.parser_proposals.human_draft_validation import validate_human_draft

    payload = _validation_payload(description="", category="")
    result = validate_human_draft(
        payload,
        {
            "amount": "12.50",
            "currency": "USD",
            "transaction_date": "2026-09-13",
            "merchant": "Cafe",
            "description": "",
            "category": "",
        },
        source_type="telegram_image",
        reason_contributors=(),
        operation_public_id="d1op_receipt_absence_noop",
    )
    assert result.changed_fields == ()
    assert result.explicit_clears == {}
    assert result.canonical_payload["description"] is None
    assert result.canonical_payload["category"] is None


@pytest.mark.parametrize("bad_merchant", [["not", "text"], "bad\x00merchant"])
def test_validation_rejects_malformed_inherited_snapshot_text(bad_merchant: object) -> None:
    """Validating only supplied keys lets malformed inherited text publish."""
    from finance_core.parser_proposals.human_draft_validation import (
        HumanDraftValidationError,
        validate_human_draft,
    )

    with pytest.raises(HumanDraftValidationError, match="D1_TEXT_INVALID"):
        validate_human_draft(
            _validation_payload(merchant=bad_merchant),
            {"category": "travel"},
            source_type="telegram_image",
            reason_contributors=(),
            operation_public_id="d1op_inherited_bad_text",
        )


def test_publication_requires_exact_accepted_operation_lineage() -> None:
    """A start operation cannot anchor an arbitrary durable publication revision."""
    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    _start(conn)
    draft = conn.execute("SELECT * FROM parser_human_drafts").fetchone()
    operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_type = 'start'"
    ).fetchone()
    proposal = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?", (draft["source_parser_output_id"],)
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO parser_human_draft_publications (
                publication_public_id, draft_id, operation_id, draft_version,
                draft_content_hash, parser_output_id, proposal_public_id,
                proposal_version, proposal_content_hash, published_at
            ) VALUES ('forged-start-publication', ?, ?, 99, ?, ?, ?, 0, ?, 1100)
            """,
            (
                draft["id"],
                operation["id"],
                "f" * 64,
                proposal["id"],
                proposal["public_id"],
                draft["decision_target_proposal_content_hash"],
            ),
        )
    conn.rollback()
    conn.close()


def test_d1_reject_replay_revalidates_binding_and_expiry() -> None:
    """Exact terminal replay must not turn an expired or forged capability into success."""
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftDecisionBinding,
        HumanDraftError,
        reject_active_human_draft_in_transaction,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    started = _start(conn)
    reference_public_id = _insert_d1_action_binding(
        conn,
        action="reject",
        card_generation_public_id=started.card_generation_public_id,
        suffix="7",
        redeemed=True,
    )
    target_id = _insert_decision(
        conn, decision_public_id="decision-reject-replay-expiry", state="rejected"
    )
    binding = HumanDraftDecisionBinding(
        reference_public_id,
        started.card_generation_public_id,
        "111",
        "acct",
        "111",
        "binding",
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    reject_active_human_draft_in_transaction(
        conn,
        parser_output_id=target_id,
        authenticated_actor_id="111",
        decision_public_id="decision-reject-replay-expiry",
        decision_binding=binding,
        now_epoch=1100,
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(HumanDraftError, match="reject_binding_stale"):
        reject_active_human_draft_in_transaction(
            conn,
            parser_output_id=target_id,
            authenticated_actor_id="111",
            decision_public_id="decision-reject-replay-expiry",
            decision_binding=binding,
            now_epoch=2500,
        )
    conn.rollback()
    assert (
        conn.execute("SELECT COUNT(*) FROM parser_human_draft_operations").fetchone()[0] == before
    )
    conn.close()


def test_bound_d1_reject_selects_its_context_before_legacy_ambiguity() -> None:
    """Another active context must not deny a valid explicitly bound Reject."""
    from finance_core.parser_proposals.human_drafts import (
        HumanDraftDecisionBinding,
        reject_active_human_draft_in_transaction,
    )

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    first = _start(conn)
    first_draft_id = conn.execute("SELECT id FROM parser_human_drafts").fetchone()[0]
    reference_public_id = _insert_d1_action_binding(
        conn,
        action="reject",
        card_generation_public_id=first.card_generation_public_id,
        suffix="8",
        redeemed=True,
    )
    _start_second_context(conn)
    target_id = _insert_decision(
        conn, decision_public_id="decision-bound-context", state="rejected"
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    reject_active_human_draft_in_transaction(
        conn,
        parser_output_id=target_id,
        authenticated_actor_id="111",
        decision_public_id="decision-bound-context",
        decision_binding=HumanDraftDecisionBinding(
            reference_public_id,
            first.card_generation_public_id,
            "111",
            "acct",
            "111",
            "binding",
        ),
        now_epoch=1100,
    )
    conn.commit()
    states = {
        row["id"]: row["state"] for row in conn.execute("SELECT id, state FROM parser_human_drafts")
    }
    assert states[first_draft_id] == "rejected"
    assert list(states.values()).count("active") == 1
    conn.close()


def test_repository_persists_reason_before_after_hashes_and_resolution_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_core.parser_proposals import human_drafts
    from finance_core.parser_proposals.human_drafts import HumanDraftCommand, apply_human_draft_card

    conn = _connection()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    payload = _validation_payload(amount=None)
    started = _start(conn, payload=payload)
    assert started.unresolved_flags == ("missing_amount",)
    fields = {
        "amount": "13",
        "currency": "USD",
        "transaction_date": "2026-09-13",
        "merchant": "Cafe",
        "description": "Lunch",
        "category": "food",
    }
    text = (
        f"资料卡编号: {started.card_generation_public_id}\n金额: 13\n币种: USD\n"
        "日期: 2026-09-13\n商户: Cafe\n描述: Lunch\n分类: food"
    )
    monkeypatch.setattr(human_drafts, "_now_epoch", lambda: 1001)
    result = apply_human_draft_card(
        conn,
        HumanDraftCommand(
            started.card_generation_public_id,
            101,
            "d1op_resolve_missing_amount",
            "111",
            "acct",
            "111",
            "binding",
            text,
            fields,
        ),
        publish=_valid_child_publisher,
    )
    assert result.completeness == "complete"
    operation = conn.execute(
        "SELECT * FROM parser_human_draft_operations WHERE operation_public_id = ?",
        ("d1op_resolve_missing_amount",),
    ).fetchone()
    before = json.loads(operation["reason_contributors_before_json"])
    after = json.loads(operation["reason_contributors_after_json"])
    assert len(before) == len(after) == 1
    assert before[0]["resolved_by_operation_id"] is None
    assert after[0]["resolved_by_operation_id"] == "d1op_resolve_missing_amount"
    assert after[0]["resolved_fields"] == ["amount"]
    assert after[0]["resolution_before_after"] == {"amount": [None, "13.00"]}
    canonical_before = json.dumps(before, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    canonical_after = json.dumps(after, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert (
        operation["reason_contributors_before_hash"]
        == hashlib.sha256(canonical_before.encode("utf-8")).hexdigest()
    )
    assert (
        operation["reason_contributors_after_hash"]
        == hashlib.sha256(canonical_after.encode("utf-8")).hexdigest()
    )
    assert json.loads(operation["unresolved_flags_json"]) == []
    conn.close()
