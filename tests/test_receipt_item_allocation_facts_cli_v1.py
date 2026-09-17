"""IAF.4 fact-set review/operations CLI tests.

The review commands are SELECT-only staging reports.  The ``persist`` and
``supersede`` commands load explicit human-authored JSON command files and
delegate entirely to the existing IAF.2/IAF.3 services.  All databases and
command files in this suite are disposable ``tmp_path`` artifacts.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import finance_core.parser_proposals.receipt_item_allocation_facts_cli as cli_module
import finance_core.parser_proposals.receipt_item_allocation_facts_review as review_module
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    supersede_receipt_item_allocation_facts,
)
from finance_core.parser_proposals.receipt_item_allocation_facts_cli import (
    _open_connection,
)
from finance_core.parser_proposals.receipt_item_allocation_facts_cli import (
    main as cli_main,
)
from finance_core.parser_proposals.receipt_item_allocation_facts_review import (
    DEFAULT_REVIEW_LIMIT,
    MAX_HISTORY_LIMIT,
    MAX_REVIEW_LIMIT,
    FactSetReviewIntegrityError,
    FactSetReviewReceiptNotFoundError,
    FactSetReviewStagingDatabaseRejectedError,
    InvalidFactSetReviewRequestError,
    get_fact_set_review_detail,
    list_fact_set_review_receipts,
)
from tests.conftest import LIVE_DB_PATH, connect_temp_db
from tests.test_receipt_facts_conversion_v1 import (
    count_diff,
    evidence_rows,
    table_counts,
)
from tests.test_receipt_item_allocation_facts_service_v1 import (
    iaf_command,
    receipt_state,
    setup_receipt,
)
from tests.test_receipt_item_allocation_facts_supersession_v1 import (
    correction_command,
)


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    code = cli_main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def write_command_file(tmp_path: Path, name: str, command: object) -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(dataclasses.asdict(command), sort_keys=True),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def staged_receipt(
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> tuple[Path, sqlite3.Connection, Any]:
    conn = connect_temp_db(migrated_temp_db_path)
    ctx = setup_receipt(conn, tmp_path, "iaf4")
    try:
        yield migrated_temp_db_path, conn, ctx
    finally:
        conn.close()


def test_review_lists_and_shows_conversion_bound_receipt_select_only(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
) -> None:
    db, conn, ctx = staged_receipt
    before_counts = table_counts(conn)
    before_receipt = receipt_state(conn)
    before_evidence = evidence_rows(conn)

    code, out, err = run_cli(["list-receipts", "--db", str(db)])
    assert code == 0, err
    assert err == ""
    assert "SELECT-only" in out
    assert "candidate_for_fact_set_review" in out
    assert f"receipt_public_id={ctx.receipt_public_id}" in out
    assert f"conversion_command_public_id={ctx.conversion_command_public_id}" in out
    assert f"conversion_result_hash={ctx.conversion_result_hash}" in out
    assert "fact_set_state=none" in out
    assert "fact_set_count=0" in out

    code, out, err = run_cli(
        [
            "show-receipt",
            "--db",
            str(db),
            "--receipt-public-id",
            ctx.receipt_public_id,
        ]
    )
    assert code == 0, err
    assert err == ""
    assert f"receipt_public_id: {ctx.receipt_public_id}" in out
    assert "net_paid_amount_canonical_text: 12.34" in out
    assert "payer_participant_public_id: person_owner" in out
    assert "participant_public_id=person_owner" in out
    assert "participant_public_id=person_alice" in out
    assert "participant_public_id=person_bob" in out
    assert "is_included=false" in out
    assert "expected_current_fact_set: none" in out
    assert "No persisted fact-set versions." in out

    assert table_counts(conn) == before_counts
    assert receipt_state(conn) == before_receipt
    assert evidence_rows(conn) == before_evidence


def test_review_module_is_bounded_deterministic_and_typed(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
) -> None:
    _db, conn, ctx = staged_receipt
    first = list_fact_set_review_receipts(conn)
    second = list_fact_set_review_receipts(conn, limit=DEFAULT_REVIEW_LIMIT)
    assert first == second
    assert [row.receipt_public_id for row in first] == sorted(
        row.receipt_public_id for row in first
    )
    assert first[0].receipt_public_id == ctx.receipt_public_id

    for bad_limit in (0, -1, True, MAX_REVIEW_LIMIT + 1):
        with pytest.raises(InvalidFactSetReviewRequestError):
            list_fact_set_review_receipts(conn, limit=bad_limit)
    for bad_history_limit in (0, -1, True, MAX_HISTORY_LIMIT + 1):
        with pytest.raises(InvalidFactSetReviewRequestError):
            get_fact_set_review_detail(
                conn,
                ctx.receipt_public_id,
                history_limit=bad_history_limit,
            )
    for bad_history_offset in (-1, True, 10**100):
        with pytest.raises(InvalidFactSetReviewRequestError):
            get_fact_set_review_detail(
                conn,
                ctx.receipt_public_id,
                history_offset=bad_history_offset,
            )
    for bad_history_anchor in (0, -1, True, 10**100):
        with pytest.raises(InvalidFactSetReviewRequestError):
            get_fact_set_review_detail(
                conn,
                ctx.receipt_public_id,
                history_anchor_version=bad_history_anchor,
            )
    with pytest.raises(InvalidFactSetReviewRequestError):
        get_fact_set_review_detail(conn, " ")
    with pytest.raises(FactSetReviewReceiptNotFoundError):
        get_fact_set_review_detail(conn, "rcpt_missing")


def test_cli_persist_delegates_to_service_and_replays_exactly(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, ctx = staged_receipt
    command_file = write_command_file(tmp_path, "persist.json", iaf_command("iaf4", ctx))
    before_counts = table_counts(conn)
    before_receipt = receipt_state(conn)
    before_evidence = evidence_rows(conn)

    argv = ["persist", "--db", str(db), "--command-file", str(command_file)]
    code, out, err = run_cli(argv)
    assert code == 0, err
    assert err == ""
    assert "authoritative human-authored item/allocation facts" in out
    assert "fact_set_version: 1" in out
    assert "idempotent_replay: false" in out
    assert "calculator_ready: not evaluated" in out
    assert count_diff(before_counts, table_counts(conn)) == {
        "financial_audit_events": 1,
        "receipt_adjustments": 1,
        "receipt_item_allocation_fact_sets": 1,
        "receipt_item_allocation_facts": 4,
        "receipt_items": 2,
    }
    assert receipt_state(conn) == before_receipt
    assert evidence_rows(conn) == before_evidence

    after_first = table_counts(conn)
    code, replay_out, replay_err = run_cli(argv)
    assert code == 0, replay_err
    assert "idempotent_replay: true" in replay_out
    assert table_counts(conn) == after_first


def test_cli_supersede_uses_reviewed_predecessor_and_preserves_history(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, ctx = staged_receipt
    persist_file = write_command_file(tmp_path, "persist.json", iaf_command("iaf4", ctx))
    assert run_cli(["persist", "--db", str(db), "--command-file", str(persist_file)])[0] == 0
    predecessor = get_fact_set_review_detail(conn, ctx.receipt_public_id).active_fact_set
    assert predecessor is not None
    correction = correction_command("iaf4_v2", ctx, predecessor)
    correction_file = write_command_file(tmp_path, "supersede.json", correction)
    before_receipt = receipt_state(conn)
    before_evidence = evidence_rows(conn)

    argv = ["supersede", "--db", str(db), "--command-file", str(correction_file)]
    code, out, err = run_cli(argv)
    assert code == 0, err
    assert err == ""
    assert "fact_set_version: 2" in out
    assert f"supersedes_fact_set_public_id: {predecessor.fact_set_public_id}" in out
    assert f"superseded_fact_set_result_hash: {predecessor.fact_set_result_hash}" in out
    assert "idempotent_replay: false" in out
    assert receipt_state(conn) == before_receipt
    assert evidence_rows(conn) == before_evidence

    code, show_out, show_err = run_cli(
        [
            "show-receipt",
            "--db",
            str(db),
            "--receipt-public-id",
            ctx.receipt_public_id,
        ]
    )
    assert code == 0, show_err
    assert "fact_set_count: 2" in show_out
    assert "fact_set_version=1" in show_out
    assert "fact_set_status=superseded" in show_out
    assert "fact_set_version=2" in show_out
    assert "fact_set_status=active" in show_out
    active = get_fact_set_review_detail(conn, ctx.receipt_public_id).active_fact_set
    assert active is not None
    assert f"expected_current_fact_set_public_id: {active.fact_set_public_id}" in show_out
    assert f"expected_current_fact_set_result_hash: {active.fact_set_result_hash}" in show_out
    assert '"item_name":"Corrected total"' in show_out

    newest_page = get_fact_set_review_detail(
        conn,
        ctx.receipt_public_id,
        history_limit=1,
        history_offset=0,
    )
    assert [version.fact_set_version for version in newest_page.fact_sets] == [2]
    assert newest_page.history_anchor_version == 2
    assert newest_page.history_has_more is True
    older_page = get_fact_set_review_detail(
        conn,
        ctx.receipt_public_id,
        history_limit=1,
        history_offset=1,
    )
    assert [version.fact_set_version for version in older_page.fact_sets] == [1]
    assert older_page.history_has_more is False

    code, page_out, page_err = run_cli(
        [
            "show-receipt",
            "--db",
            str(db),
            "--receipt-public-id",
            ctx.receipt_public_id,
            "--history-limit",
            "1",
            "--history-offset",
            "0",
        ]
    )
    assert code == 0, page_err
    assert (
        "anchor_version=2 | offset=0 | limit=1 | returned=1 | "
        "anchored_total=2 | current_total=2 | has_more=true"
    ) in page_out
    assert "fact_set_version=2" in page_out
    assert "fact_set_version=1" not in page_out

    active_v2 = newest_page.active_fact_set
    assert active_v2 is not None
    v3 = supersede_receipt_item_allocation_facts(
        conn,
        correction_command("iaf4_v3", ctx, active_v2),
    )
    assert v3.fact_set_version == 3
    anchored_older_page = get_fact_set_review_detail(
        conn,
        ctx.receipt_public_id,
        history_limit=1,
        history_offset=1,
        history_anchor_version=2,
    )
    assert anchored_older_page.history_anchor_version == 2
    assert [version.fact_set_version for version in anchored_older_page.fact_sets] == [1]
    assert anchored_older_page.history_has_more is False

    code, anchored_out, anchored_err = run_cli(
        [
            "show-receipt",
            "--db",
            str(db),
            "--receipt-public-id",
            ctx.receipt_public_id,
            "--history-limit",
            "1",
            "--history-offset",
            "1",
            "--history-anchor-version",
            "2",
        ]
    )
    assert code == 0, anchored_err
    assert (
        "anchor_version=2 | offset=1 | limit=1 | returned=1 | "
        "anchored_total=2 | current_total=3 | has_more=false"
    ) in anchored_out
    assert "fact_set_version=1" in anchored_out
    assert "fact_set_version=2" not in anchored_out
    assert "fact_set_version=3" not in anchored_out

    after_first = table_counts(conn)
    code, replay_out, replay_err = run_cli(argv)
    assert code == 0, replay_err
    assert "idempotent_replay: true" in replay_out
    assert table_counts(conn) == after_first


def test_cli_stale_supersede_fails_closed_without_retry_or_substitution(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, ctx = staged_receipt
    persist_file = write_command_file(tmp_path, "persist.json", iaf_command("iaf4", ctx))
    assert run_cli(["persist", "--db", str(db), "--command-file", str(persist_file)])[0] == 0
    predecessor = get_fact_set_review_detail(conn, ctx.receipt_public_id).active_fact_set
    assert predecessor is not None
    stale = correction_command(
        "iaf4_stale",
        ctx,
        predecessor,
        expected_current_fact_set_result_hash="a" * 64,
    )
    stale_file = write_command_file(tmp_path, "stale.json", stale)
    before = table_counts(conn)

    code, out, err = run_cli(["supersede", "--db", str(db), "--command-file", str(stale_file)])
    assert code == 1
    assert out == ""
    assert "StaleItemFactSetVersionError" in err
    assert "Traceback" not in err
    assert table_counts(conn) == before


def test_command_file_errors_are_usage_failures_before_database_open(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, _ctx = staged_receipt
    before = table_counts(conn)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    non_object = tmp_path / "list.json"
    non_object.write_text("[]", encoding="utf-8")
    non_human_file = tmp_path / "nonhuman.json"
    non_human_file.write_text('{"actor_type":"agent"}', encoding="utf-8")
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    duplicate_top = tmp_path / "duplicate-top.json"
    duplicate_top.write_text('{"command_public_id":"reviewed","command_public_id":"other"}')
    duplicate_nested = tmp_path / "duplicate-nested.json"
    duplicate_nested.write_text('{"item":{"line_amount":"1.00","line_amount":"9.99"}}')
    huge_integer = tmp_path / "huge-integer.json"
    huge_integer.write_text('{"value":' + ("9" * 5000) + "}")
    deeply_nested = tmp_path / "deeply-nested.json"
    deeply_nested.write_text('{"value":' + ("[" * 2000) + "0" + ("]" * 2000) + "}")
    lone_surrogate = tmp_path / "lone-surrogate.json"
    lone_surrogate.write_text(r'{"reason":"\ud800"}')

    cases = (
        (malformed, "safely parseable bounded JSON"),
        (non_object, "one JSON object"),
        (non_human_file, "human-only"),
        (invalid_utf8, "strict UTF-8"),
        (duplicate_top, "duplicate JSON key"),
        (duplicate_nested, "duplicate JSON key"),
        (huge_integer, "safely parseable bounded JSON"),
        (deeply_nested, "nesting exceeds"),
        (lone_surrogate, "Unicode scalar values"),
        (tmp_path / "missing.json", "does not exist"),
    )
    for command_file, expected_message in cases:
        for operation in ("persist", "supersede"):
            code, out, err = run_cli(
                [operation, "--db", str(db), "--command-file", str(command_file)]
            )
            assert code == 2
            assert out == ""
            assert "CommandFileError" in err
            assert expected_message in err
            assert "Traceback" not in err
    assert table_counts(conn) == before


def test_command_file_size_limit_is_independently_enforced(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, conn, _ctx = staged_receipt
    before = table_counts(conn)
    monkeypatch.setattr(cli_module, "MAX_COMMAND_FILE_BYTES", 32)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 33)

    code, out, err = run_cli(["persist", "--db", str(db), "--command-file", str(oversized)])
    assert code == 2
    assert out == ""
    assert "exceeds the 32-byte safety limit" in err
    assert table_counts(conn) == before


def test_service_side_command_validation_remains_authoritative(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, ctx = staged_receipt
    command = dataclasses.asdict(iaf_command("bad", ctx))
    command["unexpected"] = "never ignored"
    command_file = tmp_path / "bad.json"
    command_file.write_text(json.dumps(command), encoding="utf-8")
    before = table_counts(conn)

    code, out, err = run_cli(["persist", "--db", str(db), "--command-file", str(command_file)])
    assert code == 1
    assert out == ""
    assert "InvalidItemFactsCommandError" in err
    assert table_counts(conn) == before


def test_open_connection_enforces_review_read_only_and_write_mode_never_creates(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, _conn, _ctx = staged_receipt
    review_conn = _open_connection(str(db), readonly=True)
    try:
        assert review_conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            review_conn.execute("CREATE TABLE forbidden_write (id INTEGER)")
    finally:
        review_conn.close()

    missing = tmp_path / "must_not_be_created.sqlite"
    code, out, err = run_cli(["list-receipts", "--db", str(missing)])
    assert code == 1
    assert out == ""
    assert "FileNotFoundError" in err
    assert not missing.exists()


@pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason="live database not present")
def test_review_rejects_live_database_identity() -> None:
    live_uri = f"{LIVE_DB_PATH.resolve().as_uri()}?mode=ro"
    with pytest.raises(FactSetReviewStagingDatabaseRejectedError):
        live = sqlite3.connect(live_uri, uri=True)
        live.row_factory = sqlite3.Row
        live.execute("PRAGMA foreign_keys = ON")
        try:
            list_fact_set_review_receipts(live)
        finally:
            live.close()

    code, out, err = run_cli(["list-receipts", "--db", str(LIVE_DB_PATH)])
    assert code == 1
    assert out == ""
    assert "FactSetReviewStagingDatabaseRejectedError" in err


def test_review_rejects_copied_database_identity(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, ctx = staged_receipt
    conn.commit()
    copied = tmp_path / "copied.sqlite"
    shutil.copy2(db, copied)
    copied_conn = sqlite3.connect(str(copied))
    copied_conn.row_factory = sqlite3.Row
    copied_conn.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(FactSetReviewStagingDatabaseRejectedError):
            get_fact_set_review_detail(copied_conn, ctx.receipt_public_id)
    finally:
        copied_conn.close()

    code, out, err = run_cli(["list-receipts", "--db", str(copied)])
    assert code == 1
    assert out == ""
    assert "FactSetReviewStagingDatabaseRejectedError" in err

    command_file = write_command_file(tmp_path, "copied-write.json", iaf_command("copy", ctx))
    code, out, err = run_cli(["persist", "--db", str(copied), "--command-file", str(command_file)])
    assert code == 1
    assert out == ""
    assert "ItemFactsStagingDatabaseRejectedError" in err


def test_review_fails_closed_on_forged_registry_lineage(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, ctx = staged_receipt
    persist_file = write_command_file(tmp_path, "persist.json", iaf_command("iaf4", ctx))
    assert run_cli(["persist", "--db", str(db), "--command-file", str(persist_file)])[0] == 0
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TRIGGER trg_receipt_item_allocation_fact_sets_single_transition")
    conn.execute(
        "UPDATE receipt_item_allocation_fact_sets "
        "SET superseded_by_fact_set_public_id = 'rfs_missing'"
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(FactSetReviewIntegrityError):
        get_fact_set_review_detail(conn, ctx.receipt_public_id)


def test_review_full_verifier_rejects_same_count_item_content_drift(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, ctx = staged_receipt
    persist_file = write_command_file(tmp_path, "persist.json", iaf_command("iaf4", ctx))
    assert run_cli(["persist", "--db", str(db), "--command-file", str(persist_file)])[0] == 0
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TRIGGER trg_receipt_items_fact_set_bound_freeze")
    conn.execute("UPDATE receipt_items SET item_name = 'TAMPERED' WHERE fact_set_id IS NOT NULL")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(FactSetReviewIntegrityError):
        get_fact_set_review_detail(conn, ctx.receipt_public_id)


def test_review_full_verifier_rejects_forged_result_hash(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, ctx = staged_receipt
    persist_file = write_command_file(tmp_path, "persist.json", iaf_command("iaf4", ctx))
    assert run_cli(["persist", "--db", str(db), "--command-file", str(persist_file)])[0] == 0
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TRIGGER trg_receipt_item_allocation_fact_sets_single_transition")
    conn.execute(
        "UPDATE receipt_item_allocation_fact_sets SET fact_set_result_hash = ?",
        ("a" * 64,),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(FactSetReviewIntegrityError):
        get_fact_set_review_detail(conn, ctx.receipt_public_id)


def test_operator_text_is_single_line_and_terminal_safe(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
) -> None:
    _db, conn, ctx = staged_receipt
    summary = get_fact_set_review_detail(conn, ctx.receipt_public_id).receipt
    poisoned = dataclasses.replace(
        summary,
        merchant="Shop\nexpected_current_fact_set_result_hash: forged\x1b[2J",
    )
    line = cli_module._summary_line(poisoned)
    assert "\n" not in line
    assert "\x1b" not in line
    assert r"\nexpected_current_fact_set_result_hash" in line
    assert r"\u001b[2J" in line
    safe_payload = cli_module._terminal_safe_canonical_json(
        json.dumps({"item_name": "x\u009b\u2028"}, ensure_ascii=False)
    )
    assert "\u009b" not in safe_payload
    assert "\u2028" not in safe_payload
    assert r"\u009b" in safe_payload
    assert r"\u2028" in safe_payload


def test_cli_error_text_is_terminal_safe(tmp_path: Path) -> None:
    poisoned_path = tmp_path / "missing\nforged\x1b[2J.sqlite"
    code, out, err = run_cli(["list-receipts", "--db", str(poisoned_path)])
    assert code == 1
    assert out == ""
    error_line = err.removesuffix("\n")
    assert "\n" not in error_line
    assert "\x1b" not in error_line
    assert r"\nforged" in error_line
    assert r"\u001b[2J" in error_line


def test_review_maps_adversarial_persisted_json_to_typed_error(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
) -> None:
    db, conn, ctx = staged_receipt
    persist_file = write_command_file(tmp_path, "persist.json", iaf_command("iaf4", ctx))
    assert run_cli(["persist", "--db", str(db), "--command-file", str(persist_file)])[0] == 0
    deeply_nested_payload = '{"value":' + ("[" * 2000) + "0" + ("]" * 2000) + "}"
    payload_hash = hashlib.sha256(deeply_nested_payload.encode("utf-8")).hexdigest()
    conn.commit()
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute("DROP TRIGGER trg_receipt_item_allocation_fact_sets_single_transition")
    conn.execute(
        "UPDATE receipt_item_allocation_fact_sets "
        "SET canonical_fact_set_payload = ?, fact_set_input_hash = ?",
        (deeply_nested_payload, payload_hash),
    )
    conn.commit()
    conn.execute("PRAGMA ignore_check_constraints = OFF")

    code, out, err = run_cli(
        [
            "show-receipt",
            "--db",
            str(db),
            "--receipt-public-id",
            ctx.receipt_public_id,
        ]
    )
    assert code == 1
    assert out == ""
    assert "FactSetReviewIntegrityError" in err
    assert "Traceback" not in err


def test_review_rejects_supersession_committed_between_summary_and_history_page(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, conn, ctx = staged_receipt
    persist_file = write_command_file(tmp_path, "persist.json", iaf_command("iaf4", ctx))
    assert run_cli(["persist", "--db", str(db), "--command-file", str(persist_file)])[0] == 0
    predecessor = get_fact_set_review_detail(conn, ctx.receipt_public_id).active_fact_set
    assert predecessor is not None
    writer = connect_temp_db(db)
    original_page_loader = review_module._load_fact_set_history_page
    superseded = False

    def supersede_before_page(*args: Any, **kwargs: Any) -> Any:
        nonlocal superseded
        if not superseded:
            supersede_receipt_item_allocation_facts(
                writer,
                correction_command("iaf4_race_v2", ctx, predecessor),
            )
            superseded = True
        return original_page_loader(*args, **kwargs)

    monkeypatch.setattr(
        review_module,
        "_load_fact_set_history_page",
        supersede_before_page,
    )
    try:
        with pytest.raises(
            FactSetReviewIntegrityError,
            match="changed during SELECT-only review",
        ):
            get_fact_set_review_detail(conn, ctx.receipt_public_id)
    finally:
        writer.close()


def test_argparse_rejects_unknown_or_missing_arguments(
    staged_receipt: tuple[Path, sqlite3.Connection, Any],
) -> None:
    db, _conn, _ctx = staged_receipt
    with pytest.raises(SystemExit) as excinfo:
        run_cli(["list-receipts", "--db", str(db), "--force"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        run_cli(["persist", "--db", str(db)])
    assert excinfo.value.code == 2
