"""B4.3 receipt facts conversion review/operations CLI tests.

Covers candidate listing, candidate detail, guarded conversion invocation,
replay/conflict passthrough, malformed and unauthorized inputs, failure
behavior, and boundary non-effects for
``finance_core/parser_proposals/receipt_facts_conversion_cli.py`` and
``finance_core/parser_proposals/receipt_facts_conversion_review.py``.

The CLI delegates conversion entirely to the B4.1 service; these tests
prove the delegation and the SELECT-only review contract, not the B4.1
guard matrix (which lives in ``test_receipt_facts_conversion_v1.py`` and
``test_receipt_facts_conversion_concurrency_v1.py``).  All fixtures are
synthetic temporary staging databases; no live or seed data is touched.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from finance_core.parser_proposals import complete_proposal, supersede_receipt_total_proposal
from finance_core.parser_proposals.confirmation import reject_proposal
from finance_core.parser_proposals.receipt_facts_conversion import derive_receipt_public_id
from finance_core.parser_proposals.receipt_facts_conversion_cli import main as cli_main
from finance_core.parser_proposals.receipt_facts_conversion_review import (
    CANDIDATE_REVIEW_LABEL,
    DEFAULT_CANDIDATE_LIMIT,
    MAX_CANDIDATE_LIMIT,
    InvalidReviewRequestError,
    ReviewProposalNotFoundError,
    ReviewStagingDatabaseRejectedError,
    UnsupportedReviewProposalTypeError,
    get_conversion_review_candidate_detail,
    list_conversion_review_candidates,
)
from tests.conftest import LIVE_DB_PATH, connect_temp_db
from tests.test_parser_proposal_conversion import create_confirmed_simple_proposal
from tests.test_receipt_facts_conversion_v1 import (
    command,
    convert,
    count_diff,
    entries,
    evidence_rows,
    expected_conversion_diff,
    forge_legacy_conversion,
    hash_of,
    participant_id,
    seed_confirmed_receipt_proposal,
    seed_people,
    seed_receipt_proposal,
    strip_payload_field,
    table_counts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def staged(migrated_temp_db_path: Path) -> Any:
    conn = connect_temp_db(migrated_temp_db_path)
    seed_people(conn)
    try:
        yield migrated_temp_db_path, conn
    finally:
        conn.close()


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli_main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def membership_json(*pairs: tuple[str, int]) -> str:
    return json.dumps(entries(*pairs))


def convert_argv(
    db: Path | str,
    suffix: str,
    proposal_public_id: str,
    expected_hash: str,
    **overrides: str,
) -> list[str]:
    options: dict[str, str] = {
        "--command-public-id": f"rpfc_{suffix}",
        "--proposal-public-id": proposal_public_id,
        "--expected-content-hash": expected_hash,
        "--payer-participant-public-id": "person_owner",
        "--participants-json": membership_json(("person_owner", 1), ("person_alice", 1)),
        "--authenticated-actor-id": "owner",
        "--channel": "cli",
    }
    options.update(overrides)
    argv = ["convert", "--db", str(db)]
    for name, value in options.items():
        argv.extend([name, value])
    return argv


# ---------------------------------------------------------------------------
# Candidate listing
# ---------------------------------------------------------------------------


def test_list_candidates_labels_eligible_proposal_as_review_candidate(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "cand")
    before = table_counts(conn)

    code, out, err = run_cli(["list-candidates", "--db", str(db)])

    assert code == 0
    assert err == ""
    assert public_id in out
    assert CANDIDATE_REVIEW_LABEL in out
    assert expected in out
    # Never a convertibility claim.
    for forbidden in (
        "convertible=true",
        "convertible: true",
        "conversion_will_succeed",
        "ready_to_convert",
        "guaranteed",
    ):
        assert forbidden not in out
    # Payer and participants are never inferred or displayed as facts.
    assert "payer=" not in out
    assert "participants=" not in out
    assert "must be supplied explicitly" in out
    # SELECT-only: zero table changes.
    assert table_counts(conn) == before


def test_list_candidates_deterministic_ordering_across_reconnect(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    # Insertion order deliberately differs from public-ID order.
    for suffix in ("zz", "aa", "mm"):
        seed_confirmed_receipt_proposal(conn, tmp_path, suffix)

    first = run_cli(["list-candidates", "--db", str(db)])
    second = run_cli(["list-candidates", "--db", str(db)])

    assert first[0] == 0 and second[0] == 0
    assert first[1] == second[1]  # reconnect-stable output
    positions = [first[1].index(f"proposal_public_id=prop_{s}") for s in ("aa", "mm", "zz")]
    assert positions == sorted(positions)


def test_list_candidates_limit_behavior(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    for suffix in ("l1", "l2", "l3"):
        seed_confirmed_receipt_proposal(conn, tmp_path, suffix)

    assert DEFAULT_CANDIDATE_LIMIT == 20
    assert MAX_CANDIDATE_LIMIT == 100

    # Default limit lists all three; an explicit bound truncates
    # deterministically to the first rows of the stable ordering.
    code, out, _ = run_cli(["list-candidates", "--db", str(db)])
    assert code == 0 and out.count("proposal_public_id=") == 3
    code, out, _ = run_cli(["list-candidates", "--db", str(db), "--limit", "2"])
    assert code == 0 and out.count("proposal_public_id=") == 2
    assert "prop_l1" in out and "prop_l2" in out and "prop_l3" not in out

    rows = list_conversion_review_candidates(conn, limit=MAX_CANDIDATE_LIMIT)
    assert [candidate.proposal_public_id for candidate in rows] == [
        "prop_l1",
        "prop_l2",
        "prop_l3",
    ]

    for bad in ("0", "-3", "101"):
        code, out, err = run_cli(["list-candidates", "--db", str(db), "--limit", bad])
        assert code == 2
        assert out == ""
        assert "InvalidReviewRequestError" in err
    with pytest.raises(InvalidReviewRequestError):
        list_conversion_review_candidates(conn, limit=True)  # type: ignore[arg-type]


def test_list_candidates_excludes_ineligible_proposals(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    # Pending (never confirmed).
    seed_receipt_proposal(conn, tmp_path, "pend")
    # Rejected.
    rejected_pid, _ = seed_receipt_proposal(conn, tmp_path, "rej")
    reject_proposal(conn, rejected_pid, actor="owner")
    # Superseded parent (child stays pending → also excluded).
    superseded_pid, _sup_public, sup_hash = seed_confirmed_receipt_proposal(conn, tmp_path, "sup")
    supersede_receipt_total_proposal(
        conn,
        superseded_pid,
        actor="owner",
        expected_content_hash=sup_hash,
        field_updates={"amount": "45.60"},
        correction_public_id="rcor_sup",
    )
    # Already B4-converted.
    _cpid, converted_public, converted_hash = seed_confirmed_receipt_proposal(
        conn, tmp_path, "conv"
    )
    convert(conn, command("conv", converted_public, converted_hash))
    # Legacy-converted (fixture-forged legacy audit row).
    legacy_pid, legacy_public, _legacy_hash = seed_confirmed_receipt_proposal(conn, tmp_path, "leg")
    forge_legacy_conversion(conn, legacy_pid, "leg")
    # Unsupported type: confirmed simple text proposal (no OCR link).
    simple_pid = create_confirmed_simple_proposal(conn)
    simple_public = conn.execute(
        "SELECT public_id FROM parser_outputs WHERE id = ?", (simple_pid,)
    ).fetchone()[0]
    # Completed but not yet re-confirmed (edited_pending_confirmation).
    edited_pid, edited_public = seed_receipt_proposal(conn, tmp_path, "edit")
    complete_proposal(
        conn,
        edited_pid,
        actor="owner",
        expected_content_hash=hash_of(conn, edited_pid),
        field_updates={"merchant": "EDITED MART"},
        completion_public_id="pco_edit",
    )
    # Provably stale lineage: payload drifted after confirmation while the
    # status stayed confirmed (fixture surgery), so the confirmation-bound
    # hash no longer matches the recomputed effective hash.
    stale_pid, stale_public, _stale_hash = seed_confirmed_receipt_proposal(conn, tmp_path, "stale")
    strip_payload_field(conn, stale_pid, "merchant")
    # Revoked confirmation (fixture surgery; schema permits the state).
    revoked_pid, revoked_public, _revoked_hash = seed_confirmed_receipt_proposal(
        conn, tmp_path, "revk"
    )
    conn.execute(
        "UPDATE parser_proposal_authorizations SET confirmation_state = 'revoked', "
        "revoked_at = '2026-07-28T00:00:00Z' WHERE parser_output_id = ?",
        (revoked_pid,),
    )
    conn.commit()
    # The only remaining eligible candidate.
    _epid, eligible_public, _expected = seed_confirmed_receipt_proposal(conn, tmp_path, "ok")

    code, out, err = run_cli(["list-candidates", "--db", str(db)])

    assert code == 0, err
    assert eligible_public in out
    for absent in (
        "prop_pend",
        "prop_rej",
        "prop_sup",
        converted_public,
        legacy_public,
        simple_public,
        edited_public,
        stale_public,
        revoked_public,
    ):
        assert absent not in out, absent


def test_list_and_show_work_read_only_under_query_only_pragma(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    seed_confirmed_receipt_proposal(conn, tmp_path, "ro")
    before = table_counts(conn)

    reader = connect_temp_db(db)
    reader.execute("PRAGMA query_only = ON")
    try:
        rows = list_conversion_review_candidates(reader)
        assert [candidate.proposal_public_id for candidate in rows] == ["prop_ro"]
        assert not reader.in_transaction  # no write transaction opened
        detail = get_conversion_review_candidate_detail(reader, "prop_ro")
        assert detail.candidate_for_conversion_review is True
        assert not reader.in_transaction
    finally:
        reader.close()

    assert table_counts(conn) == before


def test_review_rejects_live_and_copied_database_identities(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, _conn = staged
    copied = tmp_path / "copied_staging_identity.sqlite"
    shutil.copy2(db, copied)

    code, out, err = run_cli(["list-candidates", "--db", str(copied)])
    assert code == 1
    assert out == ""
    assert "ReviewStagingDatabaseRejectedError" in err

    code, out, err = run_cli(
        ["show-candidate", "--db", str(copied), "--proposal-public-id", "prop_x"]
    )
    assert code == 1 and "ReviewStagingDatabaseRejectedError" in err

    # Conversion against the copied identity is rejected by the B4.1 guard.
    code, out, err = run_cli(convert_argv(copied, "copyx", "prop_x", "0" * 64))
    assert code == 1
    assert "ConversionStagingDatabaseRejectedError" in err

    reader = connect_temp_db(copied)
    try:
        with pytest.raises(ReviewStagingDatabaseRejectedError):
            list_conversion_review_candidates(reader)
    finally:
        reader.close()


@pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason="live database not present")
def test_review_rejects_live_database_read_only() -> None:
    code, out, err = run_cli(["list-candidates", "--db", str(LIVE_DB_PATH)])
    assert code == 1
    assert out == ""
    assert "ReviewStagingDatabaseRejectedError" in err


# ---------------------------------------------------------------------------
# Candidate detail
# ---------------------------------------------------------------------------


def test_show_candidate_reports_bounded_review_material(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "show")
    before = table_counts(conn)

    code, out, err = run_cli(["show-candidate", "--db", str(db), "--proposal-public-id", public_id])

    assert code == 0
    assert err == ""
    assert f"proposal_public_id: {public_id}" in out
    assert f"parser_output_id (diagnostic only): {pid}" in out
    assert f"current_effective_content_hash: {expected}" in out
    assert f"confirmation_bound_content_hash: {expected}" in out
    assert "parse_status: confirmed" in out
    assert "confirmation_state: confirmed" in out
    assert "confirmation_actor_type: human" in out
    assert "is_current_leaf: true" in out
    assert "is_superseded: false" in out
    assert "supersession_contributes: false" in out
    assert "completion_contributes: false" in out
    assert "merchant: COLD STORAGE" in out
    assert "transaction_date: 2026-07-20" in out
    assert "12.34" in out  # proposal-stage amount, verbatim, untruncated
    assert "SGD" in out
    assert "ambiguity_flags: []" in out
    assert "extraction_public_id: rocr_show" in out
    assert "extraction_source_attachment_hash: " in out
    assert "attachment_public_id: " in out
    assert "attachment_content_hash: " in out
    assert "raw_intake_public_id: " in out
    assert "source_channel: telegram" in out
    assert "b4_conversion_registry_row_exists: false" in out
    assert "legacy_transaction_conversion_exists: false" in out
    assert "candidate_for_conversion_review: true" in out
    # Human-required inputs are explicitly identified as absent.
    assert "must be supplied explicitly" in out
    assert "payer_participant_public_id" in out
    assert "command_public_id" in out
    # Bounded output: no OCR text dump, no unbounded lines.
    assert "TOTAL" not in out  # raw OCR block text never printed
    assert len(out.splitlines()) < 60
    assert table_counts(conn) == before


def test_show_candidate_after_conversion_reports_registry_row(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "done")
    convert(conn, command("done", public_id, expected))

    code, out, err = run_cli(["show-candidate", "--db", str(db), "--proposal-public-id", public_id])

    assert code == 0, err
    assert "b4_conversion_registry_row_exists: true" in out
    assert "candidate_for_conversion_review: false" in out


def test_show_candidate_reports_stale_hash_binding_without_repair(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    pid, public_id, confirmed_hash = seed_confirmed_receipt_proposal(conn, tmp_path, "drift")
    strip_payload_field(conn, pid, "merchant")  # fixture surgery: payload drift
    drifted_hash = hash_of(conn, pid)
    assert drifted_hash != confirmed_hash

    code, out, err = run_cli(["show-candidate", "--db", str(db), "--proposal-public-id", public_id])

    assert code == 0, err
    # Both hashes are reported exactly; nothing is repaired or substituted.
    assert f"current_effective_content_hash: {drifted_hash}" in out
    assert f"confirmation_bound_content_hash: {confirmed_hash}" in out
    assert "candidate_for_conversion_review: false" in out
    # The stale proposal is also excluded from the candidate listing.
    list_code, list_out, _ = run_cli(["list-candidates", "--db", str(db)])
    assert list_code == 0 and public_id not in list_out


def test_show_candidate_unknown_and_unsupported_and_inconsistent_fail_safely(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged

    code, out, err = run_cli(
        ["show-candidate", "--db", str(db), "--proposal-public-id", "prop_missing"]
    )
    assert code == 1
    assert out == ""
    assert "ReviewProposalNotFoundError" in err
    assert "Traceback" not in err

    simple_pid = create_confirmed_simple_proposal(conn)
    simple_public = conn.execute(
        "SELECT public_id FROM parser_outputs WHERE id = ?", (simple_pid,)
    ).fetchone()[0]
    code, out, err = run_cli(
        ["show-candidate", "--db", str(db), "--proposal-public-id", simple_public]
    )
    assert code == 1 and "UnsupportedReviewProposalTypeError" in err

    # Contradictory persisted state (unparseable payload) fails closed.
    broken_pid, broken_public, _ = seed_confirmed_receipt_proposal(conn, tmp_path, "brk")
    conn.execute(
        "UPDATE parser_outputs SET parsed_payload = 'not json' WHERE id = ?",
        (broken_pid,),
    )
    conn.commit()
    before = table_counts(conn)
    code, out, err = run_cli(
        ["show-candidate", "--db", str(db), "--proposal-public-id", broken_public]
    )
    assert code == 1
    assert "InconsistentReviewStateError" in err
    assert "Traceback" not in err
    assert table_counts(conn) == before
    # The inconsistent row is conservatively excluded from listing.
    list_code, list_out, _ = run_cli(["list-candidates", "--db", str(db)])
    assert list_code == 0 and broken_public not in list_out

    with pytest.raises(ReviewProposalNotFoundError):
        get_conversion_review_candidate_detail(conn, "prop_missing")
    with pytest.raises(UnsupportedReviewProposalTypeError):
        get_conversion_review_candidate_detail(conn, simple_public)
    with pytest.raises(InvalidReviewRequestError):
        get_conversion_review_candidate_detail(conn, "   ")


# ---------------------------------------------------------------------------
# Conversion happy path
# ---------------------------------------------------------------------------


def test_cli_convert_creates_exactly_one_fact_set_via_b41_service(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "chp")
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    code, out, err = run_cli(
        convert_argv(
            db,
            "chp",
            public_id,
            expected,
            **{
                "--participants-json": membership_json(
                    ("person_owner", 1), ("person_alice", 1), ("person_bob", 0)
                )
            },
        )
    )

    assert code == 0
    assert err == ""
    receipt_public_id = derive_receipt_public_id("rpfc_chp")
    assert "command_public_id: rpfc_chp" in out
    assert f"proposal_public_id: {public_id}" in out
    assert f"receipt_public_id: {receipt_public_id}" in out
    assert "conversion_result_hash: " in out
    assert "idempotent_replay: false" in out
    assert "facts-only" in out
    assert "calculator" in out and "separately" in out
    for forbidden in ("calculated", "finalized", "settled", "reconciled"):
        # Only negated wording is allowed; the status labels never appear
        # as claims (the safety notice phrases them with 'not').
        assert f"is {forbidden}" not in out

    # Exactly the B4.1-owned writes and nothing else (no transactions,
    # calculation runs, snapshots, settlement, reconciliation, items,
    # allocations, adjustments, or groups).
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(3)
    assert evidence_rows(conn) == before_evidence

    receipt = conn.execute(
        "SELECT * FROM receipts WHERE public_id = ?", (receipt_public_id,)
    ).fetchone()
    assert receipt is not None
    assert receipt["parser_output_id"] == pid
    assert receipt["payer_participant_id"] == participant_id(conn, "person_owner")
    members = {
        row["participant_id"]: row
        for row in conn.execute(
            "SELECT * FROM receipt_participants WHERE receipt_id = ?", (receipt["id"],)
        ).fetchall()
    }
    assert len(members) == 3
    assert members[participant_id(conn, "person_owner")]["role"] == "payer"
    assert members[participant_id(conn, "person_alice")]["role"] == "participant"
    assert members[participant_id(conn, "person_bob")]["role"] == "excluded"

    registry = conn.execute(
        "SELECT * FROM receipt_proposal_conversions WHERE command_public_id = 'rpfc_chp'"
    ).fetchone()
    assert registry["actor_type"] == "human"
    assert registry["authenticated_actor_id"] == "owner"
    assert registry["proposal_content_hash"] == expected  # exact hash binding
    assert registry["conversion_channel"] == "cli"


def test_cli_convert_exact_replay_and_reordered_json_are_idempotent(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "rply")
    argv = convert_argv(db, "rply", public_id, expected)

    first_code, first_out, _ = run_cli(argv)
    assert first_code == 0 and "idempotent_replay: false" in first_out
    after_first = table_counts(conn)

    second_code, second_out, second_err = run_cli(argv)
    assert second_code == 0, second_err
    assert "idempotent_replay: true" in second_out
    assert table_counts(conn) == after_first  # zero new rows on replay

    # Reordered membership JSON preserves B4.1 canonical replay behavior.
    reordered = convert_argv(
        db,
        "rply",
        public_id,
        expected,
        **{"--participants-json": membership_json(("person_alice", 1), ("person_owner", 1))},
    )
    third_code, third_out, third_err = run_cli(reordered)
    assert third_code == 0, third_err
    assert "idempotent_replay: true" in third_out
    assert table_counts(conn) == after_first


def test_cli_convert_conflicts_and_stale_hash_fail_closed(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "cnfl")
    assert run_cli(convert_argv(db, "cnfl", public_id, expected))[0] == 0
    after_convert = table_counts(conn)

    # Same command ID, changed payer → existing typed conflict.
    changed_payer = convert_argv(
        db,
        "cnfl",
        public_id,
        expected,
        **{
            "--payer-participant-public-id": "person_alice",
            "--participants-json": membership_json(("person_owner", 1), ("person_alice", 1)),
        },
    )
    code, out, err = run_cli(changed_payer)
    assert code == 1
    assert out == ""
    assert "ConversionIdempotencyConflictError" in err
    assert table_counts(conn) == after_convert

    # Same command ID, flipped inclusion → same typed conflict.
    flipped = convert_argv(
        db,
        "cnfl",
        public_id,
        expected,
        **{"--participants-json": membership_json(("person_owner", 1), ("person_alice", 0))},
    )
    code, _out, err = run_cli(flipped)
    assert code == 1 and "ConversionIdempotencyConflictError" in err
    assert table_counts(conn) == after_convert

    # Stale expected content hash fails closed through the B4.1 service;
    # the CLI never fetches or substitutes the current hash and never
    # retries with a newer one.
    _pid2, public_id2, _expected2 = seed_confirmed_receipt_proposal(conn, tmp_path, "stl2")
    before_stale = table_counts(conn)
    stale = convert_argv(db, "stl2", public_id2, "a" * 64)
    code, out, err = run_cli(stale)
    assert code == 1
    assert out == ""
    assert "StaleConfirmationHashError" in err
    assert table_counts(conn) == before_stale


# ---------------------------------------------------------------------------
# Malformed and unauthorized inputs
# ---------------------------------------------------------------------------


def test_cli_convert_rejects_malformed_participants_json_before_touching_db(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "pjson")
    before = table_counts(conn)

    malformed_values = [
        "not json",
        '{"participant_public_id": "person_owner", "is_included": 1}',  # object
        '[{"participant_public_id": "person_owner", "is_included": 1, "extra": true}]',
        '[{"participant_public_id": "person_owner"}]',  # missing is_included
        '[{"is_included": 1}]',  # missing participant id
        '[{"participant_public_id": "", "is_included": 1}]',  # blank id
        '[{"participant_public_id": "   ", "is_included": 1}]',  # blank id
        '[{"participant_public_id": 7, "is_included": 1}]',  # non-string id
        '[{"participant_public_id": "person_owner", "is_included": 2}]',
        '[{"participant_public_id": "person_owner", "is_included": 1.0}]',
        '[{"participant_public_id": "person_owner", "is_included": "yes"}]',
        '[{"participant_public_id": "person_owner", "is_included": null}]',
        '["person_owner"]',  # entry not an object
    ]
    for value in malformed_values:
        argv = convert_argv(db, "pjson", public_id, expected, **{"--participants-json": value})
        code, out, err = run_cli(argv)
        assert code == 2, value
        assert out == "", value
        assert "ParticipantsJsonError" in err, value
        assert "Traceback" not in err, value
    assert table_counts(conn) == before  # zero writes for every rejection


def test_cli_convert_service_side_rejections_map_to_nonzero_without_writes(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "srv")
    before = table_counts(conn)

    cases: list[tuple[dict[str, str], str]] = [
        # Invalid caller-owned command ID pattern.
        ({"--command-public-id": "convert_1"}, "InvalidConversionCommandError"),
        # Invalid expected-hash format.
        ({"--expected-content-hash": "xyz"}, "InvalidConversionCommandError"),
        # Unknown proposal public ID.
        ({"--proposal-public-id": "prop_nope"}, "ConversionProposalNotFoundError"),
        # Empty payer (no default payer is ever inferred).
        ({"--payer-participant-public-id": ""}, "IncompleteReceiptInputsError"),
        # Payer absent from the membership structure (payer never auto-added).
        (
            {"--payer-participant-public-id": "person_bob"},
            "IncompleteReceiptInputsError",
        ),
        # Duplicate membership entries (CLI passes them through unmodified).
        (
            {"--participants-json": membership_json(("person_owner", 1), ("person_owner", 1))},
            "AmbiguousReceiptInputError",
        ),
        # Contradictory membership entries.
        (
            {"--participants-json": membership_json(("person_owner", 1), ("person_owner", 0))},
            "AmbiguousReceiptInputError",
        ),
        # Unknown participant.
        (
            {"--participants-json": membership_json(("person_owner", 1), ("person_zed", 1))},
            "AmbiguousReceiptInputError",
        ),
        # Blank authenticated actor ID (human-only, non-empty required).
        ({"--authenticated-actor-id": ""}, "UnauthorizedConversionActorError"),
        ({"--authenticated-actor-id": "  owner "}, "UnauthorizedConversionActorError"),
        # Blank channel.
        ({"--channel": ""}, "InvalidConversionCommandError"),
    ]
    for index, (overrides, expected_error) in enumerate(cases):
        argv = convert_argv(db, f"srv{index}", public_id, expected, **overrides)
        code, out, err = run_cli(argv)
        assert code == 1, (overrides, err)
        assert out == "", overrides
        assert expected_error in err, (overrides, err)
        assert "Traceback" not in err, overrides
    assert table_counts(conn) == before  # zero partial rows for every rejection


def test_cli_rejects_unexpected_and_missing_arguments(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, _conn = staged
    # Unexpected argument.
    with pytest.raises(SystemExit) as excinfo:
        run_cli(["list-candidates", "--db", str(db), "--force"])
    assert excinfo.value.code == 2
    # Missing required payer argument: no default payer exists.
    with pytest.raises(SystemExit) as excinfo:
        run_cli(
            [
                "convert",
                "--db",
                str(db),
                "--command-public-id",
                "rpfc_x",
                "--proposal-public-id",
                "prop_x",
                "--expected-content-hash",
                "0" * 64,
                "--participants-json",
                "[]",
                "--authenticated-actor-id",
                "owner",
                "--channel",
                "cli",
            ]
        )
    assert excinfo.value.code == 2
    # Missing subcommand.
    with pytest.raises(SystemExit) as excinfo:
        run_cli([])
    assert excinfo.value.code == 2


def test_cli_fails_cleanly_when_database_path_does_not_exist(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"

    code, out, err = run_cli(["list-candidates", "--db", str(missing)])
    assert code == 1
    assert out == ""
    assert "FileNotFoundError" in err

    code, out, err = run_cli(convert_argv(missing, "nofile", "prop_x", "0" * 64))
    assert code == 1
    assert "FileNotFoundError" in err
    # The convert connection mode never creates a database file.
    assert not missing.exists()


# ---------------------------------------------------------------------------
# Boundary non-effects
# ---------------------------------------------------------------------------


def test_cli_never_invokes_calculator_readiness_or_split_calculator(
    staged: tuple[Path, sqlite3.Connection],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, conn = staged
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "spy")

    import finance_core.calculators.receipt_calculator_readiness as readiness_module
    import finance_core.calculators.receipt_split_calculator as split_module

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("B4.3 CLI must never invoke calculator boundaries")

    monkeypatch.setattr(readiness_module, "report_receipt_calculator_readiness", _forbidden)
    monkeypatch.setattr(split_module, "calculate_receipt_split", _forbidden)

    assert run_cli(["list-candidates", "--db", str(db)])[0] == 0
    assert run_cli(["show-candidate", "--db", str(db), "--proposal-public-id", public_id])[0] == 0
    code, out, err = run_cli(convert_argv(db, "spy", public_id, expected))
    assert code == 0, err
    assert "idempotent_replay: false" in out


def test_list_and_show_have_zero_side_effects_and_preserve_convertibility(
    staged: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    db, conn = staged
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "side")
    before = table_counts(conn)
    before_evidence = evidence_rows(conn)

    for _ in range(2):
        assert run_cli(["list-candidates", "--db", str(db)])[0] == 0
        assert (
            run_cli(["show-candidate", "--db", str(db), "--proposal-public-id", public_id])[0] == 0
        )

    # Listing/showing never confirms, rejects, completes, supersedes, or
    # converts: every table, including proposal/evidence tables, is
    # byte-identical.
    assert table_counts(conn) == before
    assert evidence_rows(conn) == before_evidence

    # Listing/showing never authorizes conversion, and the proposal remains
    # fully convertible through the guarded command afterwards.
    code, out, err = run_cli(convert_argv(db, "side", public_id, expected))
    assert code == 0, err
    assert count_diff(before, table_counts(conn)) == expected_conversion_diff(2)
