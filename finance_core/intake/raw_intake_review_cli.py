from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import TextIO

from finance_core.intake.raw_text_repository import get_parser_proposal, get_raw_intake_record
from finance_core.parser_proposals.confirmation import (
    CLI_ACTOR_TYPE,
    confirm_proposal,
    get_proposal_history,
    reject_proposal,
)
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite

DEFAULT_STATUS = "parsed_pending_confirmation"
SAFETY_NOTICE = (
    "Safety: proposal status is parsed_pending_confirmation; this CLI is review-only, "
    "does not create final transactions, and does not mark proposals confirmed."
)
CONFIRMATION_SAFETY_NOTICE = (
    "Safety: this CLI records parser proposal lifecycle decisions only; it does not "
    "create final transactions, calculation results, settlement obligations, "
    "or reconciliation records."
)


def main(
    argv: list[str] | None = None,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        conn = _connect(
            args.db, readonly=args.command in {"list", "show", "show-proposal", "history"}
        )
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=err)
        return 2

    conn.row_factory = sqlite3.Row
    try:
        if args.command == "list":
            _list_records(conn, status=args.status, limit=args.limit, out=out)
        elif args.command == "show":
            _show_record(conn, intake_id=args.id, out=out)
        elif args.command == "show-proposal":
            _show_proposal(conn, parser_output_id=args.parser_output_id, out=out)
        elif args.command == "confirm":
            _confirm_or_reject(
                conn,
                parser_output_id=args.parser_output_id,
                actor=args.actor,
                actor_type=args.actor_type,
                reason=args.reason,
                action="confirm",
                out=out,
            )
        elif args.command == "reject":
            _confirm_or_reject(
                conn,
                parser_output_id=args.parser_output_id,
                actor=args.actor,
                actor_type=args.actor_type,
                reason=args.reason,
                action="reject",
                out=out,
            )
        elif args.command == "history":
            _show_history(conn, parser_output_id=args.parser_output_id, out=out)
        else:
            parser.error("unknown command")
    except (sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}", file=err)
        return 1
    finally:
        conn.close()

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Review raw intake records and parser proposals without finalizing transactions."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List pending raw intake records")
    list_parser.add_argument("--db", required=True, help="Path to SQLite database")
    list_parser.add_argument("--status", default=DEFAULT_STATUS, help="Status filter")
    list_parser.add_argument("--limit", type=int, default=20, help="Maximum records to show")

    show_parser = subparsers.add_parser("show", help="Show one raw intake record")
    show_parser.add_argument("--db", required=True, help="Path to SQLite database")
    show_parser.add_argument("--id", type=int, required=True, help="Raw intake record id")

    show_proposal_parser = subparsers.add_parser(
        "show-proposal",
        help="Show one parser proposal by parser output id",
    )
    show_proposal_parser.add_argument("--db", required=True, help="Path to SQLite database")
    show_proposal_parser.add_argument(
        "--parser-output-id",
        type=int,
        required=True,
        help="Parser output id",
    )

    confirm_parser = subparsers.add_parser("confirm", help="Confirm one parser proposal")
    _add_decision_arguments(confirm_parser)

    reject_parser = subparsers.add_parser("reject", help="Reject one parser proposal")
    _add_decision_arguments(reject_parser)

    history_parser = subparsers.add_parser("history", help="Show parser proposal lifecycle history")
    history_parser.add_argument("--db", required=True, help="Path to SQLite database")
    history_parser.add_argument(
        "--parser-output-id",
        type=int,
        required=True,
        help="Parser output id",
    )

    return parser


def _add_decision_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="Path to SQLite database")
    parser.add_argument(
        "--parser-output-id",
        type=int,
        required=True,
        help="Parser output id",
    )
    parser.add_argument("--actor", default=CLI_ACTOR_TYPE, help="Decision actor identifier")
    parser.add_argument(
        "--actor-type",
        default=CLI_ACTOR_TYPE,
        help="Decision actor type",
    )
    parser.add_argument("--reason", help="Decision reason")


def _connect(db_path: str, *, readonly: bool) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    mode = "ro" if readonly else "rw"
    return connect_sqlite(
        f"file:{path}?mode={mode}",
        mode=ConnectionMode.READ_ONLY if readonly else ConnectionMode.APPLICATION,
    )


def _list_records(
    conn: sqlite3.Connection,
    *,
    status: str,
    limit: int,
    out: TextIO,
) -> None:
    if limit < 1:
        raise ValueError("--limit must be at least 1")

    rows = conn.execute(
        """
        SELECT
          id,
          public_id,
          source_type,
          received_at,
          status,
          raw_input,
          parser_output_id
        FROM raw_intake_records
        WHERE status = ?
        ORDER BY received_at DESC, id DESC
        LIMIT ?
        """,
        (status, limit),
    ).fetchall()

    print(SAFETY_NOTICE, file=out)
    print(f"Filter status: {status}", file=out)
    print("", file=out)
    if not rows:
        print("No raw intake records found.", file=out)
        return

    for row in rows:
        print(
            " | ".join(
                [
                    f"id={row['id']}",
                    f"public_id={row['public_id']}",
                    f"source_type={row['source_type']}",
                    f"received_at={row['received_at']}",
                    f"status={row['status']}",
                    f"parser_output_id={row['parser_output_id']}",
                    f"raw_input_preview={_preview(row['raw_input'])}",
                ]
            ),
            file=out,
        )


def _show_record(
    conn: sqlite3.Connection,
    *,
    intake_id: int,
    out: TextIO,
) -> None:
    intake = get_raw_intake_record(conn, intake_id)
    if intake is None:
        raise ValueError(f"raw intake record not found: {intake_id}")

    parser_output = get_parser_proposal(conn, intake_record_id=intake_id)

    print(SAFETY_NOTICE, file=out)
    print("", file=out)
    print("Raw intake record", file=out)
    print(f"id: {intake['id']}", file=out)
    print(f"public_id: {intake['public_id']}", file=out)
    print(f"source_type: {intake['source_type']}", file=out)
    print(f"received_at: {intake['received_at']}", file=out)
    print(f"status: {intake['status']}", file=out)
    print(f"parser_output_id: {intake['parser_output_id']}", file=out)
    print(f"raw_input_exact: >>>{intake['raw_input']}<<<", file=out)
    print("", file=out)

    if parser_output is None:
        print("No linked parser proposal.", file=out)
        return

    proposal = parser_output["proposal"]
    print("Parser proposal", file=out)
    print(f"parse_status: {parser_output['parse_status']}", file=out)
    print(f"confidence_score: {parser_output['confidence_score']}", file=out)
    print(f"intent: {proposal.get('intent')}", file=out)
    print(f"transaction_type: {proposal.get('transaction_type')}", file=out)
    print(f"merchant: {proposal.get('merchant')}", file=out)
    print(f"description: {proposal.get('description')}", file=out)
    print(f"amount: {proposal.get('amount')}", file=out)
    print(f"currency: {proposal.get('currency')}", file=out)
    print(f"paid_by: {proposal.get('paid_by')}", file=out)
    print(f"participants: {proposal.get('participants')}", file=out)
    print(f"split_type: {proposal.get('split_type')}", file=out)
    print(f"missing_fields: {proposal.get('missing_fields')}", file=out)
    print(f"confirmation_required: {str(proposal.get('confirmation_required')).lower()}", file=out)
    print(f"is_final: {str(proposal.get('is_final')).lower()}", file=out)
    print("", file=out)
    print("Proposal JSON", file=out)
    print(json.dumps(proposal, indent=2, sort_keys=True), file=out)


def _show_proposal(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    out: TextIO,
) -> None:
    parser_output = get_parser_proposal(conn, parser_output_id=parser_output_id)
    if parser_output is None:
        raise ValueError(f"parser output not found: {parser_output_id}")

    proposal = parser_output["proposal"]
    print(CONFIRMATION_SAFETY_NOTICE, file=out)
    print("", file=out)
    print("Parser proposal", file=out)
    print(f"parser_output_id: {parser_output['id']}", file=out)
    print(f"public_id: {parser_output['public_id']}", file=out)
    print(f"source_type: {parser_output['source_type']}", file=out)
    print(f"source_public_id: {parser_output['source_public_id']}", file=out)
    print(f"parse_status: {parser_output['parse_status']}", file=out)
    print(f"raw_text_exact: >>>{parser_output['raw_text']}<<<", file=out)
    print(f"intent: {proposal.get('intent')}", file=out)
    print(f"transaction_type: {proposal.get('transaction_type')}", file=out)
    print(f"merchant: {proposal.get('merchant')}", file=out)
    print(f"amount: {proposal.get('amount')}", file=out)
    print(f"currency: {proposal.get('currency')}", file=out)
    print(f"confirmation_required: {str(proposal.get('confirmation_required')).lower()}", file=out)
    print(f"is_final: {str(proposal.get('is_final')).lower()}", file=out)
    print("", file=out)
    print("Proposal JSON", file=out)
    print(json.dumps(proposal, indent=2, sort_keys=True), file=out)


def _confirm_or_reject(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    actor: str,
    actor_type: str,
    reason: str | None,
    action: str,
    out: TextIO,
) -> None:
    if action == "confirm":
        result = confirm_proposal(
            conn,
            parser_output_id,
            actor=actor,
            actor_type=actor_type,
            reason=reason,
        )
    else:
        result = reject_proposal(
            conn,
            parser_output_id,
            actor=actor,
            actor_type=actor_type,
            reason=reason,
        )

    print(CONFIRMATION_SAFETY_NOTICE, file=out)
    print(f"parser_output_id: {result['parser_output_id']}", file=out)
    print(f"from_status: {result['from_status']}", file=out)
    print(f"to_status: {result['to_status']}", file=out)
    print(f"actor_type: {result['actor_type']}", file=out)
    print(f"raw_intake_status: {result['raw_intake_status']}", file=out)
    print(f"event_id: {result['event_id']}", file=out)
    print(f"confirmation_id: {result['confirmation_id']}", file=out)
    print("final_transaction_created: false", file=out)


def _show_history(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    out: TextIO,
) -> None:
    events = get_proposal_history(conn, parser_output_id)

    print(CONFIRMATION_SAFETY_NOTICE, file=out)
    print(f"parser_output_id: {parser_output_id}", file=out)
    print("", file=out)
    if not events:
        print("No lifecycle events found.", file=out)
        return

    for event in events:
        print(
            " | ".join(
                [
                    f"id={event['id']}",
                    f"event_type={event['event_type']}",
                    f"from_status={event['from_status']}",
                    f"to_status={event['to_status']}",
                    f"actor_type={event['actor_type']}",
                    f"actor_identifier={event['actor_identifier']}",
                    f"created_at={event['created_at']}",
                    f"reason={event['event_reason']}",
                    f"payload={event['event_payload']}",
                ]
            ),
            file=out,
        )


def _preview(raw_input: str, max_length: int = 60) -> str:
    collapsed = " ".join(raw_input.split())
    if len(collapsed) <= max_length:
        return collapsed
    return f"{collapsed[: max_length - 3]}..."


if __name__ == "__main__":
    raise SystemExit(main())
