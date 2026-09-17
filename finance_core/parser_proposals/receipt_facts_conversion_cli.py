"""Staging-only B4.3 review/operations CLI for receipt facts conversion.

Runs as::

    python -m finance_core.parser_proposals.receipt_facts_conversion_cli <command> ...

Commands:

* ``list-candidates`` — SELECT-only listing of proposals whose persisted
  proposal-side state qualifies them for human B4 conversion review.  A
  listed row is a ``candidate_for_conversion_review`` only; it is never a
  claim that conversion will succeed.
* ``show-candidate`` — SELECT-only bounded review report for one proposal,
  containing the material needed to construct and verify a conversion
  command.  Payer and participant membership are never shown or inferred:
  they are explicit human command inputs.
* ``convert`` — constructs the explicit human-authenticated B4.1 command
  and invokes the existing guarded conversion service
  ``convert_confirmed_receipt_proposal_to_facts``.  The CLI never begins a
  transaction, never pre-writes rows, never substitutes hashes or IDs, and
  never retries; the B4.1 service owns ``BEGIN IMMEDIATE``, commit,
  rollback, idempotent replay, staging checks, and audit append.

Exit codes: ``0`` success (including idempotent replay); ``2`` malformed
CLI input (argparse errors, malformed participants JSON, invalid limits);
``1`` every database, staging, state, or service failure.  Typed error
distinctions are preserved in stable operator-facing stderr messages
without tracebacks.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, TextIO

from finance_core.parser_proposals.receipt_facts_conversion import (
    ReceiptFactsConversionCommand,
    ReceiptFactsConversionError,
    ReceiptFactsConversionResult,
    convert_confirmed_receipt_proposal_to_facts,
)
from finance_core.parser_proposals.receipt_facts_conversion_review import (
    CANDIDATE_REVIEW_LABEL,
    DEFAULT_CANDIDATE_LIMIT,
    MAX_CANDIDATE_LIMIT,
    ConversionReviewCandidateDetail,
    InvalidReviewRequestError,
    ReceiptFactsConversionReviewError,
    get_conversion_review_candidate_detail,
    list_conversion_review_candidates,
)
from finance_core.sqlite_connection import (
    ConnectionMode,
    SQLiteConnectionError,
    connect_sqlite,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

REVIEW_SAFETY_NOTICE = (
    "Safety: read-only conversion review; a listed row is a "
    f"{CANDIDATE_REVIEW_LABEL} only - persisted proposal-side state "
    "currently qualifies it for human B4 conversion review; final "
    "conversion requires a complete explicit command and full B4.1 "
    "service revalidation."
)
CONVERT_SAFETY_NOTICE = (
    "Safety: conversion creates receipt facts only; the receipt is not "
    "calculated, finalized, settled, reconciled, or converted to a "
    "transaction, and calculator readiness must be checked separately."
)
HUMAN_INPUT_REMINDER = (
    "Reminder: payer and participant membership are not persisted "
    "proposal-side facts; they must be supplied explicitly with the "
    "convert command."
)

_PARTICIPANT_ENTRY_KEYS = frozenset({"participant_public_id", "is_included"})


class ParticipantsJsonError(ValueError):
    """The --participants-json value is structurally invalid."""


def main(
    argv: list[str] | None = None,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    participants: list[dict[str, Any]] | None = None
    if args.command == "convert":
        try:
            participants = _parse_participants_json(args.participants_json)
        except ParticipantsJsonError as exc:
            print(f"error: ParticipantsJsonError: {exc}", file=err)
            return EXIT_USAGE

    try:
        conn = _open_connection(args.db, readonly=args.command != "convert")
    except (FileNotFoundError, SQLiteConnectionError, sqlite3.Error) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=err)
        return EXIT_FAILURE

    try:
        if args.command == "list-candidates":
            _run_list(conn, limit=args.limit, out=out)
        elif args.command == "show-candidate":
            _run_show(conn, proposal_public_id=args.proposal_public_id, out=out)
        else:
            assert participants is not None
            _run_convert(conn, args, participants, out=out)
        return EXIT_OK
    except InvalidReviewRequestError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=err)
        return EXIT_USAGE
    except (ReceiptFactsConversionReviewError, ReceiptFactsConversionError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=err)
        return EXIT_FAILURE
    except sqlite3.Error as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=err)
        return EXIT_FAILURE
    finally:
        # Deterministic close that never conceals a primary error above.
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m finance_core.parser_proposals.receipt_facts_conversion_cli",
        description=(
            "Staging-only review and guarded conversion of confirmed "
            "receipt OCR proposals to receipt facts (B4.3). Listing and "
            "showing are SELECT-only; conversion delegates entirely to the "
            "guarded B4.1 service."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list-candidates",
        help="List conversion-review candidates (SELECT-only, never a convertibility claim)",
    )
    list_parser.add_argument("--db", required=True, help="Path to staging SQLite database")
    list_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CANDIDATE_LIMIT,
        help=(
            f"Maximum candidates to list (default {DEFAULT_CANDIDATE_LIMIT}, "
            f"maximum {MAX_CANDIDATE_LIMIT})"
        ),
    )

    show_parser = subparsers.add_parser(
        "show-candidate",
        help="Show the bounded review report for one proposal (SELECT-only)",
    )
    show_parser.add_argument("--db", required=True, help="Path to staging SQLite database")
    show_parser.add_argument(
        "--proposal-public-id",
        required=True,
        help="Canonical parser proposal public ID",
    )

    convert_parser = subparsers.add_parser(
        "convert",
        help="Invoke the guarded B4.1 proposal-to-facts conversion service",
    )
    convert_parser.add_argument("--db", required=True, help="Path to staging SQLite database")
    convert_parser.add_argument(
        "--command-public-id",
        required=True,
        help="Caller-owned conversion command ID ('rpfc_' prefix; never generated)",
    )
    convert_parser.add_argument(
        "--proposal-public-id",
        required=True,
        help="Canonical parser proposal public ID",
    )
    convert_parser.add_argument(
        "--expected-content-hash",
        required=True,
        help="The exact effective proposal content hash the human reviewed",
    )
    convert_parser.add_argument(
        "--payer-participant-public-id",
        required=True,
        help="Explicit payer participant public ID (never inferred)",
    )
    convert_parser.add_argument(
        "--participants-json",
        required=True,
        help=(
            "Strict JSON list of membership entries, each exactly "
            '{"participant_public_id": ..., "is_included": ...}'
        ),
    )
    convert_parser.add_argument(
        "--authenticated-actor-id",
        required=True,
        help="Authenticated human actor identity",
    )
    convert_parser.add_argument("--channel", required=True, help="Conversion channel")
    convert_parser.add_argument("--reason", help="Optional non-authoritative reason")

    return parser


def _parse_participants_json(text: str) -> list[dict[str, Any]]:
    """Lossless structural parsing of the membership JSON, fail-closed.

    Order is preserved and entries are never deduplicated, reordered, or
    rewritten: the B4.1 service remains authoritative for duplicates,
    contradictions, unknown IDs, payer membership, canonical sorting, and
    command hashing.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParticipantsJsonError(f"--participants-json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ParticipantsJsonError(
            "--participants-json must be a JSON list of membership entry objects"
        )
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise ParticipantsJsonError(f"membership entry {index} must be a JSON object")
        unknown = sorted(set(entry) - _PARTICIPANT_ENTRY_KEYS)
        if unknown:
            raise ParticipantsJsonError(f"membership entry {index} carries unknown keys: {unknown}")
        missing = sorted(_PARTICIPANT_ENTRY_KEYS - set(entry))
        if missing:
            raise ParticipantsJsonError(
                f"membership entry {index} is missing required keys: {missing}"
            )
        pid = entry["participant_public_id"]
        if not isinstance(pid, str) or not pid.strip():
            raise ParticipantsJsonError(
                f"membership entry {index} participant_public_id must be a non-blank string"
            )
        included = entry["is_included"]
        if not isinstance(included, bool) and not (
            isinstance(included, int) and included in (0, 1)
        ):
            raise ParticipantsJsonError(
                f"membership entry {index} is_included must be an explicit "
                "boolean or integer 0/1; no default is supplied"
            )
        entries.append({"participant_public_id": pid, "is_included": included})
    return entries


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


def _open_connection(db_path: str, *, readonly: bool) -> sqlite3.Connection:
    """Open the explicit --db path; never create a database file.

    Review commands open read-only (``mode=ro``) and additionally set
    ``PRAGMA query_only=ON``.  The convert command opens read-write
    (``mode=rw``, never ``rwc``) with foreign keys enabled by the shared
    connection policy; the B4.1 service re-verifies staging identity,
    foreign keys, and transaction ownership itself.
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    if readonly:
        conn = connect_sqlite(str(path), mode=ConnectionMode.READ_ONLY)
        conn.execute("PRAGMA query_only = ON")
        return conn
    return connect_sqlite(
        f"{path.resolve().as_uri()}?mode=rw",
        mode=ConnectionMode.APPLICATION,
    )


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def _run_list(conn: sqlite3.Connection, *, limit: int, out: TextIO) -> None:
    candidates = list_conversion_review_candidates(conn, limit=limit)
    print(REVIEW_SAFETY_NOTICE, file=out)
    print(HUMAN_INPUT_REMINDER, file=out)
    print("", file=out)
    if not candidates:
        print("No conversion-review candidates found.", file=out)
        return
    for candidate in candidates:
        print(
            " | ".join(
                [
                    f"review_status={candidate.review_label}",
                    f"proposal_public_id={candidate.proposal_public_id}",
                    f"parser_output_id={candidate.parser_output_id}",
                    f"merchant={_fmt(candidate.merchant)}",
                    f"transaction_date={_fmt(candidate.transaction_date)}",
                    f"amount={_fmt(candidate.amount)}",
                    f"currency={_fmt(candidate.currency)}",
                    f"effective_content_hash={candidate.effective_content_hash}",
                    f"confirmation_public_id={candidate.confirmation_public_id}",
                ]
            ),
            file=out,
        )


def _run_show(conn: sqlite3.Connection, *, proposal_public_id: str, out: TextIO) -> None:
    detail = get_conversion_review_candidate_detail(conn, proposal_public_id)
    print(REVIEW_SAFETY_NOTICE, file=out)
    print("", file=out)
    for name, value in _detail_lines(detail):
        print(f"{name}: {value}", file=out)
    print("", file=out)
    print(HUMAN_INPUT_REMINDER, file=out)
    print("Human-required command inputs (absent until command time):", file=out)
    for item in detail.human_required_command_inputs:
        print(f"  - {item}", file=out)


def _detail_lines(detail: ConversionReviewCandidateDetail) -> list[tuple[str, str]]:
    return [
        ("proposal_public_id", str(detail.proposal_public_id)),
        ("parser_output_id (diagnostic only)", str(detail.parser_output_id)),
        ("parse_status", detail.parse_status),
        ("confirmation_public_id", _fmt(detail.confirmation_public_id)),
        ("confirmation_state", _fmt(detail.confirmation_state)),
        ("confirmation_actor_type", _fmt(detail.confirmation_actor_type)),
        ("current_effective_content_hash", detail.current_effective_content_hash),
        (
            "confirmation_bound_content_hash",
            _fmt(detail.confirmation_bound_content_hash),
        ),
        ("is_current_leaf", _fmt(detail.is_current_leaf)),
        ("is_superseded", _fmt(detail.is_superseded)),
        ("supersession_contributes", _fmt(detail.supersession_contributes)),
        ("completion_contributes", _fmt(detail.completion_contributes)),
        ("completion_public_id", _fmt(detail.completion_public_id)),
        ("completion_version", str(detail.completion_version)),
        ("merchant", _fmt(detail.merchant)),
        ("transaction_date", _fmt(detail.transaction_date)),
        ("amount (proposal-stage authoritative value, verbatim)", _fmt(detail.amount)),
        ("currency", _fmt(detail.currency)),
        ("ambiguity_flags", _fmt(detail.ambiguity_flags)),
        ("ambiguity_flags_wellformed", _fmt(detail.ambiguity_flags_wellformed)),
        ("extraction_public_id", detail.extraction_public_id),
        (
            "extraction_source_attachment_hash",
            detail.extraction_source_attachment_hash,
        ),
        ("attachment_public_id", detail.attachment_public_id),
        ("attachment_content_hash", _fmt(detail.attachment_content_hash)),
        ("raw_intake_public_id", detail.raw_intake_public_id),
        (
            "raw_intake_source_content_hash",
            _fmt(detail.raw_intake_source_content_hash),
        ),
        ("source_channel", _fmt(detail.source_channel)),
        (
            "b4_conversion_registry_row_exists",
            _fmt(detail.b4_conversion_registry_row_exists),
        ),
        (
            "legacy_transaction_conversion_exists",
            _fmt(detail.legacy_transaction_conversion_exists),
        ),
        (
            "candidate_for_conversion_review",
            _fmt(detail.candidate_for_conversion_review),
        ),
    ]


def _run_convert(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    participants: list[dict[str, Any]],
    *,
    out: TextIO,
) -> None:
    # CLI conversion is human-only: actor_type is constructed explicitly
    # and is not a CLI input, so no agent/system/model actor can be passed.
    command = ReceiptFactsConversionCommand(
        command_public_id=args.command_public_id,
        proposal_public_id=args.proposal_public_id,
        expected_content_hash=args.expected_content_hash,
        payer_participant_public_id=args.payer_participant_public_id,
        participants=tuple(participants),
        authenticated_actor_id=args.authenticated_actor_id,
        channel=args.channel,
        actor_type="human",
        reason=args.reason,
    )
    result = convert_confirmed_receipt_proposal_to_facts(conn, command)
    _print_conversion_result(result, out=out)


def _print_conversion_result(result: ReceiptFactsConversionResult, *, out: TextIO) -> None:
    print(CONVERT_SAFETY_NOTICE, file=out)
    print(f"command_public_id: {result.command_public_id}", file=out)
    print(f"proposal_public_id: {result.proposal_public_id}", file=out)
    print(f"receipt_public_id: {result.receipt_public_id}", file=out)
    print(f"conversion_result_hash: {result.conversion_result_hash}", file=out)
    print(f"idempotent_replay: {_fmt(result.idempotent)}", file=out)
    print(
        "receipt_facts_note: facts-only receipt created; check calculator "
        "readiness separately (B4.2); no calculation, snapshot, settlement, "
        "reconciliation, or transaction was created.",
        file=out,
    )


def _fmt(value: Any) -> str:
    """Deterministic bounded scalar formatting; authority-bearing values
    (hashes, amounts, IDs) are never truncated."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        return json.dumps(list(value), sort_keys=True, ensure_ascii=True)
    return json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)


if __name__ == "__main__":
    raise SystemExit(main())
