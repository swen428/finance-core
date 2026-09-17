"""S5e-A migration 042 tests use only disposable in-memory databases."""

from __future__ import annotations

import sqlite3

import pytest

from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    MigrationExecutionError,
    apply_migration_paths,
    build_migration_manifest,
    migration_ledger_rows,
    verify_migration_history,
)

PATHS_THROUGH_042 = TEMP_DB_MIGRATION_PATHS[:42]
HASH = "a" * 64
RETAINED_RESPONSE = b"{}"
RETAINED_RESPONSE_HASH = "44136fa355b3678a1146ad16f7e8649e94fb4fc21c4f6a9d1b7b5d2d2f0f3d58"


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def seed_parent(conn: sqlite3.Connection, marker: str) -> tuple[int, int]:
    cursor = conn.execute(
        "INSERT INTO parser_outputs ("
        "public_id, source_type, source_public_id, raw_text, parsed_payload, "
        "normalized_payload, parse_status"
        ") VALUES (?, 'telegram_text', ?, 'original source', '{}', '{}', "
        "'parsed_pending_confirmation')",
        (f"prop_s5e_parent_{marker}", f"raw_s5e_parent_{marker}"),
    )
    parent_id = int(cursor.lastrowid)
    cursor = conn.execute(
        "INSERT INTO raw_intake_records ("
        "public_id, source_type, source_channel, raw_input, received_at, "
        "source_received_at, status, parser_output_id"
        ") VALUES (?, 'telegram_text', 'telegram', 'original source', "
        "'2026-08-17T00:00:00+00:00', '2026-08-17T00:00:00+00:00', "
        "'parsed_pending_confirmation', ?)",
        (f"raw_s5e_parent_{marker}", parent_id),
    )
    return parent_id, int(cursor.lastrowid)


def insert_attempt(
    conn: sqlite3.Connection,
    parent_id: int,
    intake_id: int,
    marker: str,
    *,
    fallback_mode: str = "child_eligible",
    eligibility_reasons_json: str = '["deterministic_fields_incomplete"]',
    request_blob_override: bytes | str = b"{}",
    parent_proposal_version: int | float | str = 0,
    source_projection_byte_count: int | float | str = 2,
    request_byte_count_override: int | float | str | None = None,
    prepared_at_ms: int | float | str = 1000,
    invoke_not_after_ms: int | float | str = 6000,
    result_not_after_ms: int | float | str = 41000,
    attempt_public_id_override: str | bytes | None = None,
    preparation_material_hash_override: str | bytes | None = None,
    runtime_policy_version_override: str | bytes | None = None,
    expected_provider_override: str | bytes | None = None,
) -> int:
    identity_hash = marker * 64
    cursor = conn.execute(
        """
        INSERT INTO ai_fallback_attempts (
            attempt_public_id, preparation_material_hash, raw_intake_record_id,
            parent_parser_output_id, parent_proposal_version, parent_effective_content_hash,
            source_kind, source_projection_hash, source_projection_byte_count,
            source_selection_manifest_hash, source_field_state_hash, fallback_mode,
            eligibility_reasons_json, runtime_policy_version, runtime_policy_hash,
            prompt_version, prompt_template_hash, intent_policy_version, intent_policy_hash,
            intent_policy_result, intent_evidence_hash, default_policy_version,
            default_policy_hash, default_evidence_hash, sensitive_text_policy_version,
            sensitive_text_policy_hash, sensitive_text_scan_hash, deadline_policy_version,
            deadline_policy_hash, sqlite_money_policy_version, sqlite_money_policy_hash,
            expected_provider, expected_model, expected_agent_id,
            expected_audit_caller_kind, expected_audit_caller_id, expected_audit_caller_name,
            expected_audit_purpose, expected_audit_session_key_sha256, request_blob,
            request_sha256, request_byte_count, prepared_at_ms, invoke_not_after_ms,
            result_not_after_ms
        ) VALUES (
            ?, ?, ?, ?, ?, ?,
            'telegram_raw_text', ?, ?, ?, ?, ?, ?,
            ?, ?, 'synthetic-prompt-v1', ?, 'synthetic-intent-v1', ?,
            'positive', ?, 'synthetic-default-v1', ?, NULL, 'synthetic-sensitive-v1', ?, ?,
            'synthetic-deadline-v1', ?, 'synthetic-money-v1', ?, ?, 'synthetic/model',
            'finance-test-agent', 'plugin', 'finance-bridge', NULL, 'finance-bridge.ai-proposal-v1',
            NULL, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            (
                attempt_public_id_override
                if attempt_public_id_override is not None
                else f"aifa_{identity_hash}"
            ),
            (
                preparation_material_hash_override
                if preparation_material_hash_override is not None
                else identity_hash
            ),
            intake_id,
            parent_id,
            parent_proposal_version,
            HASH,  # parent_effective_content_hash
            HASH,  # source_projection_hash
            source_projection_byte_count,
            HASH,  # source_selection_manifest_hash
            HASH,  # source_field_state_hash
            fallback_mode,
            eligibility_reasons_json,
            (
                runtime_policy_version_override
                if runtime_policy_version_override is not None
                else "synthetic-runtime-v1"
            ),
            HASH,  # runtime_policy_hash
            HASH,  # prompt_template_hash
            HASH,  # intent_policy_hash
            HASH,  # intent_evidence_hash
            HASH,  # default_policy_hash
            HASH,  # sensitive_text_policy_hash
            HASH,  # sensitive_text_scan_hash
            HASH,  # deadline_policy_hash
            HASH,  # sqlite_money_policy_hash
            (expected_provider_override if expected_provider_override is not None else "synthetic"),
            request_blob_override,
            HASH,  # request_sha256
            (
                request_byte_count_override
                if request_byte_count_override is not None
                else len(request_blob_override)
            ),
            prepared_at_ms,
            invoke_not_after_ms,
            result_not_after_ms,
        ),
    )
    return int(cursor.lastrowid)


def insert_claim(
    conn: sqlite3.Connection,
    attempt_id: int,
    marker: str,
    *,
    invocation_claimed_at_ms: int | float | str = 1000,
    call_start_not_after_ms: int | float | str = 1250,
) -> int:
    identity_hash = marker * 64
    cursor = conn.execute(
        "INSERT INTO ai_fallback_invocation_claims ("
        "claim_public_id, claim_material_hash, attempt_id, "
        "invocation_claimed_at_ms, call_start_not_after_ms, invocation_disposition"
        ") VALUES (?, ?, ?, ?, ?, 'invoke_once')",
        (
            f"aicl_{identity_hash}",
            identity_hash,
            attempt_id,
            invocation_claimed_at_ms,
            call_start_not_after_ms,
        ),
    )
    return int(cursor.lastrowid)


def insert_result(
    conn: sqlite3.Connection,
    attempt_id: int,
    claim_id: int,
    marker: str,
    *,
    transport_outcome: str,
    result_status: str,
    recovery_disposition: str | None,
    non_child_reason: str | None = None,
    response_byte_count_override: int | float | str | None = None,
    response_code_unit_count_override: int | float | str | None = None,
    response_blob_override: bytes | str | None = None,
    result_received_at_ms: int | float | str = 1000,
    post_lock_at_ms: int | float | str = 1001,
    decision_at_ms: int | float | str = 1002,
) -> int:
    identity_hash = marker * 64
    failure_code = "host_llm_failed" if result_status == "provider_error" else None
    is_retained = transport_outcome == "response_received"
    is_oversize = transport_outcome == "response_oversize"
    is_unencodable = transport_outcome == "response_unencodable"
    is_resource_refused = transport_outcome == "response_resource_refused"
    is_normal_response = transport_outcome in {
        "response_received",
        "response_oversize",
        "response_unencodable",
        "response_resource_refused",
    }
    is_metadata_refusal = transport_outcome == "response_metadata_refused"
    retention_state = (
        "blob_retained"
        if is_retained
        else "unretained_oversize"
        if is_oversize
        else "unretained_unencodable"
        if is_unencodable
        else "unretained_resource_refused"
        if is_resource_refused
        else "none"
    )
    response_body_state = (
        "retained"
        if is_retained
        else "oversize"
        if is_oversize
        else "unencodable"
        if is_unencodable
        else "resource_refused"
        if is_resource_refused
        else "none"
    )
    response_blob = (
        response_blob_override
        if is_retained and response_blob_override is not None
        else RETAINED_RESPONSE
        if is_retained
        else None
    )
    response_sha256 = RETAINED_RESPONSE_HASH if is_retained or is_oversize else None
    response_byte_count = (
        response_byte_count_override
        if response_byte_count_override is not None
        else len(response_blob)
        if is_retained
        else 65_537
        if is_oversize
        else None
    )
    response_code_unit_count = (
        response_code_unit_count_override
        if response_code_unit_count_override is not None
        else 1
        if is_oversize or is_unencodable
        else 131_073
        if is_resource_refused
        else None
    )
    response_utf16_sha256 = HASH if is_unencodable else None
    normal_attribution_hash = HASH if is_normal_response else None
    metadata_refusal_hash = HASH if is_metadata_refusal else None
    usage_hash = HASH if is_normal_response else None
    cursor = conn.execute(
        """
        INSERT INTO ai_fallback_results (
            result_public_id, result_material_hash, attempt_id, claim_id, transport_outcome,
            result_status, retention_state, normal_attribution_hash, metadata_refusal_hash,
            usage_hash, result_received_at_ms, post_lock_at_ms, decision_at_ms,
            deadline_policy_version, deadline_policy_hash, deadline_disposition,
            response_body_state, response_blob, response_sha256, response_byte_count,
            response_code_unit_count, response_utf16_sha256, failure_code, non_child_reason,
            recovery_disposition, normalized_payload_hash, source_field_state_hash,
            ambiguity_hash, evidence_set_hash
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?,
            'synthetic-deadline-v1', ?, 'within_deadline', ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, NULL, ?, NULL, NULL
        )
        """,
        (
            f"air_{identity_hash}",
            identity_hash,
            attempt_id,
            claim_id,
            transport_outcome,
            result_status,
            retention_state,
            normal_attribution_hash,
            metadata_refusal_hash,
            usage_hash,
            result_received_at_ms,
            post_lock_at_ms,
            decision_at_ms,
            HASH,
            response_body_state,
            response_blob,
            response_sha256,
            response_byte_count,
            response_code_unit_count,
            response_utf16_sha256,
            failure_code,
            non_child_reason,
            recovery_disposition,
            HASH,
        ),
    )
    return int(cursor.lastrowid)


def test_fresh_upgrade_replay_and_manifest_are_deterministic() -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        rows = migration_ledger_rows(conn)
        assert rows[-1]["migration_id"] == "042"
        assert rows[-1]["migration_filename"] == "042_s5e_ai_fallback_provenance_foundation.sql"
        verify_migration_history(conn, PATHS_THROUGH_042)
        first = [tuple(row) for row in rows]
        apply_migration_paths(conn, PATHS_THROUGH_042)
        assert [tuple(row) for row in migration_ledger_rows(conn)] == first
        assert build_migration_manifest(PATHS_THROUGH_042)[-1].sequence == 42
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    upgraded = connection()
    try:
        apply_migration_paths(upgraded, PATHS_THROUGH_042[:41])
        assert migration_ledger_rows(upgraded)[-1]["migration_id"] == "041"
        apply_migration_paths(upgraded, PATHS_THROUGH_042)
        assert migration_ledger_rows(upgraded)[-1]["migration_id"] == "042"
    finally:
        upgraded.close()


def test_append_only_records_collisions_and_conditional_source_seals() -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        parent_id, intake_id = seed_parent(conn, "one")
        conn.execute(
            "UPDATE parser_outputs SET raw_text = 'pre-attempt rewrite' WHERE id = ?",
            (parent_id,),
        )
        attempt_id = insert_attempt(conn, parent_id, intake_id, "a")
        claim_id = insert_claim(conn, attempt_id, "b")
        result_id = insert_result(
            conn,
            attempt_id,
            claim_id,
            "c",
            transport_outcome="response_received",
            result_status="proposal_created",
            recovery_disposition=None,
            response_blob_override=b"x" * 16_384,
        )
        assert (
            conn.execute(
                "SELECT typeof(request_blob) FROM ai_fallback_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()[0]
            == "blob"
        )
        assert (
            conn.execute(
                "SELECT typeof(response_blob) FROM ai_fallback_results WHERE id = ?", (result_id,)
            ).fetchone()[0]
            == "blob"
        )
        assert (
            tuple(
                conn.execute(
                    "SELECT typeof(parent_proposal_version), "
                    "typeof(source_projection_byte_count), typeof(request_byte_count), "
                    "typeof(prepared_at_ms), typeof(invoke_not_after_ms), "
                    "typeof(result_not_after_ms) "
                    "FROM ai_fallback_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
            )
            == ("integer",) * 6
        )
        assert (
            tuple(
                conn.execute(
                    "SELECT typeof(invocation_claimed_at_ms), typeof(call_start_not_after_ms) "
                    "FROM ai_fallback_invocation_claims WHERE id = ?",
                    (claim_id,),
                ).fetchone()
            )
            == ("integer",) * 2
        )
        assert (
            tuple(
                conn.execute(
                    "SELECT typeof(result_received_at_ms), typeof(post_lock_at_ms), "
                    "typeof(decision_at_ms), typeof(response_byte_count) "
                    "FROM ai_fallback_results WHERE id = ?",
                    (result_id,),
                ).fetchone()
            )
            == ("integer",) * 4
        )
        child_cursor = conn.execute(
            "INSERT INTO parser_outputs ("
            "public_id, source_type, parse_status, parent_parser_output_id"
            ") VALUES ('prop_s5e_child_one', 'telegram_text', "
            "'parsed_pending_confirmation', ?)",
            (parent_id,),
        )
        child_id = int(child_cursor.lastrowid)
        link_cursor = conn.execute(
            "INSERT INTO ai_fallback_proposal_links ("
            "link_public_id, link_material_hash, result_id, parser_output_id, "
            "effective_content_hash"
            ") VALUES ('aipl_dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', "
            "?, ?, ?, ?)",
            (HASH, result_id, child_id, HASH),
        )
        link_id = int(link_cursor.lastrowid)
        for table, record_id in (
            ("ai_fallback_attempts", attempt_id),
            ("ai_fallback_invocation_claims", claim_id),
            ("ai_fallback_results", result_id),
            ("ai_fallback_proposal_links", link_id),
        ):
            text_columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})")
                if row["type"].upper() == "TEXT"
            ]
            text_storage_types = conn.execute(
                "SELECT "
                + ", ".join(f"typeof({column})" for column in text_columns)
                + f" FROM {table} WHERE id = ?",
                (record_id,),
            ).fetchone()
            assert set(text_storage_types) <= {"text", "null"}

        other_parent_id, other_intake_id = seed_parent(conn, "two")
        other_attempt_id = insert_attempt(conn, other_parent_id, other_intake_id, "d")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempts WHERE request_sha256 = ?",
                (HASH,),
            ).fetchone()[0]
            == 2
        )
        other_claim_id = insert_claim(conn, other_attempt_id, "e")
        other_child_cursor = conn.execute(
            "INSERT INTO parser_outputs ("
            "public_id, source_type, parse_status, parent_parser_output_id"
            ") VALUES ('prop_s5e_child_two', 'telegram_text', "
            "'parsed_pending_confirmation', ?)",
            (other_parent_id,),
        )
        other_child_id = int(other_child_cursor.lastrowid)
        mismatch_parent_id, _ = seed_parent(conn, "three")
        _, mismatch_intake_id = seed_parent(conn, "four")
        oversize_parent_id, oversize_intake_id = seed_parent(conn, "five")
        oversize_attempt_id = insert_attempt(conn, oversize_parent_id, oversize_intake_id, "6")
        oversize_claim_id = insert_claim(conn, oversize_attempt_id, "7")
        metadata_parent_id, metadata_intake_id = seed_parent(conn, "six")
        metadata_attempt_id = insert_attempt(conn, metadata_parent_id, metadata_intake_id, "b")
        metadata_claim_id = insert_claim(conn, metadata_attempt_id, "c")
        invalid_metadata_parent_id, invalid_metadata_intake_id = seed_parent(conn, "seven")
        invalid_metadata_attempt_id = insert_attempt(
            conn, invalid_metadata_parent_id, invalid_metadata_intake_id, "e"
        )
        invalid_metadata_claim_id = insert_claim(conn, invalid_metadata_attempt_id, "f")
        retained_oversize_parent_id, retained_oversize_intake_id = seed_parent(conn, "eight")
        retained_oversize_attempt_id = insert_attempt(
            conn, retained_oversize_parent_id, retained_oversize_intake_id, "c"
        )
        retained_oversize_claim_id = insert_claim(conn, retained_oversize_attempt_id, "d")
        late_parent_id, late_intake_id = seed_parent(conn, "nine")
        late_attempt_id = insert_attempt(conn, late_parent_id, late_intake_id, "f")
        late_claim_id = insert_claim(conn, late_attempt_id, "9")
        late_retained_parent_id, late_retained_intake_id = seed_parent(conn, "ten")
        late_retained_attempt_id = insert_attempt(
            conn, late_retained_parent_id, late_retained_intake_id, "1"
        )
        late_retained_claim_id = insert_claim(conn, late_retained_attempt_id, "1")
        stale_oversize_parent_id, stale_oversize_intake_id = seed_parent(conn, "eleven")
        stale_oversize_attempt_id = insert_attempt(
            conn, stale_oversize_parent_id, stale_oversize_intake_id, "2"
        )
        stale_oversize_claim_id = insert_claim(conn, stale_oversize_attempt_id, "2")
        unencodable_parent_id, unencodable_intake_id = seed_parent(conn, "twelve")
        unencodable_attempt_id = insert_attempt(
            conn, unencodable_parent_id, unencodable_intake_id, "3"
        )
        unencodable_claim_id = insert_claim(conn, unencodable_attempt_id, "3")
        resource_refused_parent_id, resource_refused_intake_id = seed_parent(conn, "thirteen")
        resource_refused_attempt_id = insert_attempt(
            conn, resource_refused_parent_id, resource_refused_intake_id, "4"
        )
        resource_refused_claim_id = insert_claim(conn, resource_refused_attempt_id, "4")
        unsealed_parser_id = int(
            conn.execute(
                "INSERT INTO parser_outputs (public_id, source_type, parse_status) "
                "VALUES ('prop_s5e_unsealed', 'telegram_text', 'parsed_pending_confirmation')"
            ).lastrowid
        )

        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA recursive_triggers = OFF")
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0

        with pytest.raises(sqlite3.IntegrityError, match="does not bind its parent"):
            insert_attempt(conn, mismatch_parent_id, mismatch_intake_id, "5")

        with pytest.raises(sqlite3.IntegrityError):
            insert_result(
                conn,
                oversize_attempt_id,
                oversize_claim_id,
                "8",
                transport_outcome="response_oversize",
                result_status="response_oversize",
                recovery_disposition="use_manual_intake",
                response_byte_count_override=65_536,
            )
        oversize_result_id = insert_result(
            conn,
            oversize_attempt_id,
            oversize_claim_id,
            "9",
            transport_outcome="response_oversize",
            result_status="response_oversize",
            recovery_disposition="use_manual_intake",
        )
        assert tuple(
            conn.execute(
                "SELECT typeof(response_byte_count), typeof(response_code_unit_count) "
                "FROM ai_fallback_results WHERE id = ?",
                (oversize_result_id,),
            ).fetchone()
        ) == ("integer", "integer")
        with pytest.raises(sqlite3.IntegrityError):
            insert_result(
                conn,
                retained_oversize_attempt_id,
                retained_oversize_claim_id,
                "e",
                transport_outcome="response_received",
                result_status="proposal_created",
                recovery_disposition=None,
                response_blob_override=b"x" * 16_385,
            )
        for result_status, non_child_reason in (
            ("classification_only", "intent_unproven"),
            ("response_refused", "validation_refused"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                insert_result(
                    conn,
                    retained_oversize_attempt_id,
                    retained_oversize_claim_id,
                    "e",
                    transport_outcome="response_received",
                    result_status=result_status,
                    recovery_disposition=None,
                    non_child_reason=non_child_reason,
                    response_blob_override=b"x" * 16_385,
                )
        insert_result(
            conn,
            retained_oversize_attempt_id,
            retained_oversize_claim_id,
            "e",
            transport_outcome="response_received",
            result_status="response_oversize",
            recovery_disposition="use_manual_intake",
            response_blob_override=b"x" * 16_385,
        )
        insert_result(
            conn,
            late_attempt_id,
            late_claim_id,
            "f",
            transport_outcome="response_oversize",
            result_status="late_result",
            recovery_disposition="resend_new_intake_after_late_result",
        )
        insert_result(
            conn,
            late_retained_attempt_id,
            late_retained_claim_id,
            "1",
            transport_outcome="response_received",
            result_status="late_result",
            recovery_disposition="resend_new_intake_after_late_result",
            response_blob_override=b"x" * 16_385,
        )
        insert_result(
            conn,
            metadata_attempt_id,
            metadata_claim_id,
            "a",
            transport_outcome="response_metadata_refused",
            result_status="attribution_refused",
            recovery_disposition="operator_runtime_review",
        )
        with pytest.raises(sqlite3.IntegrityError):
            insert_result(
                conn,
                invalid_metadata_attempt_id,
                invalid_metadata_claim_id,
                "b",
                transport_outcome="response_metadata_refused",
                result_status="proposal_created",
                recovery_disposition=None,
            )
        insert_result(
            conn,
            invalid_metadata_attempt_id,
            invalid_metadata_claim_id,
            "b",
            transport_outcome="response_received",
            result_status="attribution_refused",
            recovery_disposition="operator_runtime_review",
            response_blob_override=b"x" * 20_000,
        )
        insert_result(
            conn,
            stale_oversize_attempt_id,
            stale_oversize_claim_id,
            "2",
            transport_outcome="response_oversize",
            result_status="stale_parent",
            recovery_disposition="review_current_parent_state",
        )
        insert_result(
            conn,
            unencodable_attempt_id,
            unencodable_claim_id,
            "4",
            transport_outcome="response_unencodable",
            result_status="attribution_refused",
            recovery_disposition="operator_runtime_review",
        )
        insert_result(
            conn,
            resource_refused_attempt_id,
            resource_refused_claim_id,
            "5",
            transport_outcome="response_resource_refused",
            result_status="stale_parent",
            recovery_disposition="review_current_parent_state",
        )

        with pytest.raises(sqlite3.IntegrityError, match="claim does not belong"):
            insert_result(
                conn,
                other_attempt_id,
                claim_id,
                "1",
                transport_outcome="provider_error",
                result_status="provider_error",
                recovery_disposition="resend_new_intake_after_provider_failure",
            )

        with pytest.raises(sqlite3.IntegrityError):
            insert_result(
                conn,
                other_attempt_id,
                other_claim_id,
                "2",
                transport_outcome="response_received",
                result_status="provider_error",
                recovery_disposition="resend_new_intake_after_provider_failure",
            )

        other_result_id = insert_result(
            conn,
            other_attempt_id,
            other_claim_id,
            "3",
            transport_outcome="provider_error",
            result_status="provider_error",
            recovery_disposition="resend_new_intake_after_provider_failure",
        )
        with pytest.raises(sqlite3.IntegrityError, match="proposal-created result"):
            conn.execute(
                "INSERT INTO ai_fallback_proposal_links ("
                "link_public_id, link_material_hash, result_id, parser_output_id, "
                "effective_content_hash"
                ") VALUES ('aipl_"
                "3333333333333333333333333333333333333333333333333333333333333333', "
                "?, ?, ?, ?)",
                (HASH, other_result_id, other_child_id, HASH),
            )
        with pytest.raises(sqlite3.IntegrityError, match="attempt child"):
            conn.execute(
                "INSERT INTO ai_fallback_proposal_links ("
                "link_public_id, link_material_hash, result_id, parser_output_id, "
                "effective_content_hash"
                ") VALUES ('aipl_"
                "4444444444444444444444444444444444444444444444444444444444444444', "
                "?, ?, ?, ?)",
                (HASH, result_id, other_child_id, HASH),
            )

        for table in (
            "ai_fallback_attempts",
            "ai_fallback_invocation_claims",
            "ai_fallback_results",
            "ai_fallback_proposal_links",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"UPDATE {table} SET created_at = '2099-01-01T00:00:00+00:00'")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"DELETE FROM {table}")

        for table, record_id in (
            ("ai_fallback_attempts", attempt_id),
            ("ai_fallback_invocation_claims", claim_id),
            ("ai_fallback_results", result_id),
            ("ai_fallback_proposal_links", link_id),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} WHERE id = ?",
                    (record_id,),
                )
        for sealed_parser_output_id in (parent_id, child_id):
            sealed_public_id = conn.execute(
                "SELECT public_id FROM parser_outputs WHERE id = ?", (sealed_parser_output_id,)
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="sealed parser output"):
                conn.execute(
                    "UPDATE OR REPLACE parser_outputs SET public_id = ? WHERE id = ?",
                    (sealed_public_id, unsealed_parser_id),
                )
            with pytest.raises(sqlite3.IntegrityError, match="sealed parser output"):
                conn.execute(
                    "UPDATE OR REPLACE parser_outputs SET id = ? WHERE id = ?",
                    (sealed_parser_output_id, unsealed_parser_id),
                )
        for table, record_id in (
            ("parser_outputs", parent_id),
            ("parser_outputs", child_id),
            ("raw_intake_records", intake_id),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="cannot be replaced"):
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} WHERE id = ?",
                    (record_id,),
                )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE parser_outputs SET raw_text = 'rewritten' WHERE id = ?",
                (parent_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE raw_intake_records SET raw_input = 'rewritten' WHERE id = ?",
                (intake_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE parser_outputs SET raw_text = 'rewritten' WHERE id = ?",
                (child_id,),
            )
        conn.execute(
            "UPDATE parser_outputs SET parse_status = 'confirmed' WHERE id = ?",
            (parent_id,),
        )
        assert (
            conn.execute(
                "SELECT parse_status FROM parser_outputs WHERE id = ?", (parent_id,)
            ).fetchone()[0]
            == "confirmed"
        )
    finally:
        conn.close()


def test_fk_off_lineage_guards_require_every_durable_ancestor() -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA recursive_triggers = OFF")

        orphan_parent_id, orphan_intake_id = seed_parent(conn, "orphan_parent")
        conn.execute("DELETE FROM parser_outputs WHERE id = ?", (orphan_parent_id,))
        with pytest.raises(sqlite3.IntegrityError, match="does not bind its parent"):
            insert_attempt(conn, orphan_parent_id, orphan_intake_id, "a")

        unbound_parent_id = int(
            conn.execute(
                "INSERT INTO parser_outputs (public_id, source_type, parse_status) "
                "VALUES ('prop_s5e_no_raw', 'telegram_text', 'parsed_pending_confirmation')"
            ).lastrowid
        )
        with pytest.raises(sqlite3.IntegrityError, match="does not bind its parent"):
            insert_attempt(conn, unbound_parent_id, 999_999, "b")

        valid_parent_id, valid_intake_id = seed_parent(conn, "lineage")
        valid_attempt_id = insert_attempt(conn, valid_parent_id, valid_intake_id, "c")
        with pytest.raises(sqlite3.IntegrityError, match="requires its attempt"):
            insert_claim(conn, 999_999, "d")
        with pytest.raises(sqlite3.IntegrityError, match="claim does not belong"):
            insert_result(
                conn,
                valid_attempt_id,
                999_999,
                "e",
                transport_outcome="provider_error",
                result_status="provider_error",
                recovery_disposition="resend_new_intake_after_provider_failure",
            )
        valid_claim_id = insert_claim(conn, valid_attempt_id, "f")
        insert_result(
            conn,
            valid_attempt_id,
            valid_claim_id,
            "1",
            transport_outcome="provider_error",
            result_status="provider_error",
            recovery_disposition="resend_new_intake_after_provider_failure",
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("request_blob_override", "eligibility_reasons_json"),
    (
        ("{}", '["deterministic_fields_incomplete"]'),
        ("汉", '["deterministic_fields_incomplete"]'),
        (b"{}", "not-json"),
        (b"{}", "[]"),
        (b"{}", '["unknown_reason"]'),
        (b"{}", '["unsupported_language", "unsupported_language"]'),
        (b"{}", '["intent_classification_required"]'),
    ),
)
def test_attempt_rejects_text_blobs_and_noncanonical_closed_eligibility_reasons(
    request_blob_override: bytes | str, eligibility_reasons_json: str
) -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        parent_id, intake_id = seed_parent(conn, "attempt_rejection")
        with pytest.raises(sqlite3.IntegrityError):
            insert_attempt(
                conn,
                parent_id,
                intake_id,
                "a",
                request_blob_override=request_blob_override,
                eligibility_reasons_json=eligibility_reasons_json,
            )
    finally:
        conn.close()


def test_classification_only_attempt_requires_and_accepts_its_closed_reason() -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        parent_id, intake_id = seed_parent(conn, "classification_only")
        attempt_id = insert_attempt(
            conn,
            parent_id,
            intake_id,
            "a",
            fallback_mode="classification_only",
            eligibility_reasons_json='["intent_classification_required"]',
        )
        assert attempt_id > 0
    finally:
        conn.close()


@pytest.mark.parametrize("response_blob_override", ("text", "汉"))
def test_retained_response_rejects_text_evidence(response_blob_override: str) -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        parent_id, intake_id = seed_parent(conn, "response_text")
        attempt_id = insert_attempt(conn, parent_id, intake_id, "a")
        claim_id = insert_claim(conn, attempt_id, "b")
        with pytest.raises(sqlite3.IntegrityError):
            insert_result(
                conn,
                attempt_id,
                claim_id,
                "c",
                transport_outcome="response_received",
                result_status="proposal_created",
                recovery_disposition=None,
                response_blob_override=response_blob_override,
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "table",
    (
        "ai_fallback_attempts",
        "ai_fallback_invocation_claims",
        "ai_fallback_results",
        "ai_fallback_proposal_links",
    ),
)
def test_every_provenance_text_column_requires_actual_text_storage(table: str) -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()[0]
        text_columns = [
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})")
            if row["type"].upper() == "TEXT"
        ]
        assert text_columns
        for column in text_columns:
            assert f"typeof({column}) = 'text'" in table_sql
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("attempt_kwarg", "blob_value"),
    (
        ("attempt_public_id_override", b"aifa_" + b"a" * 64),
        ("preparation_material_hash_override", b"a" * 64),
        ("runtime_policy_version_override", b"synthetic-runtime-v1"),
        ("expected_provider_override", b"synthetic"),
    ),
)
def test_attempt_rejects_blob_identity_hash_version_and_expected_text(
    attempt_kwarg: str, blob_value: bytes
) -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        parent_id, intake_id = seed_parent(conn, "text_blob")
        with pytest.raises(sqlite3.IntegrityError):
            insert_attempt(conn, parent_id, intake_id, "a", **{attempt_kwarg: blob_value})
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("attempt_kwarg", "real_value"),
    (
        ("parent_proposal_version", 0.5),
        ("source_projection_byte_count", 2.5),
        ("request_byte_count_override", 2.5),
        ("prepared_at_ms", 1000.5),
        ("invoke_not_after_ms", 6000.5),
        ("result_not_after_ms", 41000.5),
    ),
)
@pytest.mark.parametrize("invalid_kind", ("real", "text"))
def test_attempt_material_scalars_require_actual_integer_storage(
    attempt_kwarg: str, real_value: float, invalid_kind: str
) -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        parent_id, intake_id = seed_parent(conn, "attempt_scalar")
        invalid_value: float | str = real_value if invalid_kind == "real" else "not-an-integer"
        with pytest.raises(sqlite3.IntegrityError):
            insert_attempt(conn, parent_id, intake_id, "a", **{attempt_kwarg: invalid_value})
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("claim_kwarg", "real_value"),
    (
        ("invocation_claimed_at_ms", 1000.5),
        ("call_start_not_after_ms", 1250.5),
    ),
)
@pytest.mark.parametrize("invalid_kind", ("real", "text"))
def test_claim_material_clocks_require_actual_integer_storage(
    claim_kwarg: str, real_value: float, invalid_kind: str
) -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        parent_id, intake_id = seed_parent(conn, "claim_scalar")
        attempt_id = insert_attempt(conn, parent_id, intake_id, "a")
        invalid_value: float | str = real_value if invalid_kind == "real" else "not-an-integer"
        with pytest.raises(sqlite3.IntegrityError):
            insert_claim(conn, attempt_id, "b", **{claim_kwarg: invalid_value})
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("result_kwarg", "real_value", "transport_outcome", "result_status", "recovery_disposition"),
    (
        ("result_received_at_ms", 1000.5, "response_received", "proposal_created", None),
        ("post_lock_at_ms", 1001.5, "response_received", "proposal_created", None),
        ("decision_at_ms", 1002.5, "response_received", "proposal_created", None),
        (
            "response_byte_count_override",
            65537.5,
            "response_oversize",
            "response_oversize",
            "use_manual_intake",
        ),
        (
            "response_code_unit_count_override",
            1.5,
            "response_oversize",
            "response_oversize",
            "use_manual_intake",
        ),
    ),
)
@pytest.mark.parametrize("invalid_kind", ("real", "text"))
def test_result_material_scalars_require_actual_integer_storage(
    result_kwarg: str,
    real_value: float,
    transport_outcome: str,
    result_status: str,
    recovery_disposition: str | None,
    invalid_kind: str,
) -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        parent_id, intake_id = seed_parent(conn, "result_scalar")
        attempt_id = insert_attempt(conn, parent_id, intake_id, "a")
        claim_id = insert_claim(conn, attempt_id, "b")
        invalid_value: float | str = real_value if invalid_kind == "real" else "not-an-integer"
        with pytest.raises(sqlite3.IntegrityError):
            insert_result(
                conn,
                attempt_id,
                claim_id,
                "c",
                transport_outcome=transport_outcome,
                result_status=result_status,
                recovery_disposition=recovery_disposition,
                **{result_kwarg: invalid_value},
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "table",
    (
        "ai_fallback_attempts",
        "ai_fallback_invocation_claims",
        "ai_fallback_results",
        "ai_fallback_proposal_links",
    ),
)
def test_squatter_table_fails_closed_without_a_042_ledger_row(table: str) -> None:
    conn = connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042[:41])
        conn.execute(f"CREATE TABLE {table} (bogus INTEGER)")
        conn.commit()
        with pytest.raises(MigrationExecutionError):
            apply_migration_paths(conn, PATHS_THROUGH_042)
        assert migration_ledger_rows(conn)[-1]["migration_id"] == "041"
    finally:
        conn.close()
