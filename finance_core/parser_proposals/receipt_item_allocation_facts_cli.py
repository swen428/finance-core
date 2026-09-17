"""Staging-only IAF.4 fact-set review/operations CLI.

Run as::

    python -m finance_core.parser_proposals.receipt_item_allocation_facts_cli <command> ...

``list-receipts`` and ``show-receipt`` are SELECT-only review commands.
``persist`` and ``supersede`` load an explicit JSON command file and
delegate entirely to the guarded IAF.2/IAF.3 services.  The CLI never
generates command IDs or hashes, substitutes a current predecessor, retries
stale commands, begins a transaction, or performs preparatory writes.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, TextIO

from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ReceiptItemAllocationFactsCommand,
    ReceiptItemAllocationFactsError,
    ReceiptItemAllocationFactsResult,
    ReceiptItemAllocationFactsSupersessionCommand,
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
)
from finance_core.parser_proposals.receipt_item_allocation_facts_review import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_REVIEW_LIMIT,
    FACT_SET_REVIEW_LABEL,
    MAX_HISTORY_LIMIT,
    MAX_REVIEW_LIMIT,
    FactSetReviewReceipt,
    InvalidFactSetReviewRequestError,
    ReceiptItemAllocationFactsReviewError,
    get_fact_set_review_detail,
    list_fact_set_review_receipts,
)
from finance_core.sqlite_connection import ConnectionMode, SQLiteConnectionError, connect_sqlite

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

# Covers the service's bounded 200-item / 200-adjustment shapes with large
# explicit participant lists while still preventing unbounded local input.
MAX_COMMAND_FILE_BYTES = 32 * 1024 * 1024
MAX_COMMAND_JSON_DEPTH = 64

REVIEW_SAFETY_NOTICE = (
    "Safety: SELECT-only staging fact-set review snapshot; a listed receipt "
    f"is a {FACT_SET_REVIEW_LABEL} only, never a persistence or supersession "
    "eligibility guarantee. The guarded service revalidates the explicit "
    "command and current database state at invocation time."
)
WRITE_SAFETY_NOTICE = (
    "Safety: this command records authoritative human-authored "
    "item/allocation facts through the guarded IAF service; it does not run "
    "readiness, calculation, snapshot, finalization, settlement, "
    "reconciliation, or transaction creation."
)


class CommandFileError(ValueError):
    """The explicit JSON command file cannot be used as CLI input."""


def _reject_duplicate_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys at every JSON object depth, fail-closed."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CommandFileError(f"command file contains duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _require_bounded_json_depth(value: Any) -> None:
    """Iteratively reject adversarial nesting without recursive traversal."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_COMMAND_JSON_DEPTH:
            raise CommandFileError(
                f"command JSON nesting exceeds the approved depth of {MAX_COMMAND_JSON_DEPTH}"
            )
        if isinstance(current, str):
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise CommandFileError(
                    "command JSON strings must contain only valid Unicode scalar values"
                ) from exc
        elif isinstance(current, dict):
            for key in current:
                try:
                    key.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise CommandFileError(
                        "command JSON keys must contain only valid Unicode scalar values"
                    ) from exc
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def main(
    argv: list[str] | None = None,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    command_data: dict[str, Any] | None = None
    if args.command in {"persist", "supersede"}:
        try:
            command_data = _load_command_file(args.command_file)
        except CommandFileError as exc:
            print(f"error: CommandFileError: {_fmt(str(exc))}", file=err)
            return EXIT_USAGE

    try:
        conn = _open_connection(
            args.db,
            readonly=args.command in {"list-receipts", "show-receipt"},
        )
    except (FileNotFoundError, SQLiteConnectionError, sqlite3.Error) as exc:
        print(f"error: {type(exc).__name__}: {_fmt(str(exc))}", file=err)
        return EXIT_FAILURE

    try:
        if args.command == "list-receipts":
            _run_list(conn, limit=args.limit, out=out)
        elif args.command == "show-receipt":
            _run_show(
                conn,
                receipt_public_id=args.receipt_public_id,
                history_limit=args.history_limit,
                history_offset=args.history_offset,
                history_anchor_version=args.history_anchor_version,
                out=out,
            )
        elif args.command == "persist":
            assert command_data is not None
            _run_persist(conn, command_data, out=out)
        else:
            assert args.command == "supersede"
            assert command_data is not None
            _run_supersede(conn, command_data, out=out)
        return EXIT_OK
    except InvalidFactSetReviewRequestError as exc:
        print(f"error: {type(exc).__name__}: {_fmt(str(exc))}", file=err)
        return EXIT_USAGE
    except (ReceiptItemAllocationFactsReviewError, ReceiptItemAllocationFactsError) as exc:
        print(f"error: {type(exc).__name__}: {_fmt(str(exc))}", file=err)
        return EXIT_FAILURE
    except sqlite3.Error as exc:
        print(f"error: {type(exc).__name__}: {_fmt(str(exc))}", file=err)
        return EXIT_FAILURE
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m finance_core.parser_proposals.receipt_item_allocation_facts_cli",
        description=(
            "Staging-only IAF.4 SELECT-only fact-set review plus guarded "
            "persist/supersede service invocation."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list-receipts",
        help="List B4.1 conversion-created receipts for fact-set review (SELECT-only)",
    )
    list_parser.add_argument("--db", required=True, help="Path to staging SQLite database")
    list_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_REVIEW_LIMIT,
        help=(
            f"Maximum receipts to list (default {DEFAULT_REVIEW_LIMIT}, maximum {MAX_REVIEW_LIMIT})"
        ),
    )

    show_parser = subparsers.add_parser(
        "show-receipt",
        help="Show conversion, membership, and immutable fact-set history (SELECT-only)",
    )
    show_parser.add_argument("--db", required=True, help="Path to staging SQLite database")
    show_parser.add_argument(
        "--receipt-public-id",
        required=True,
        help="Canonical B4.1 conversion-created receipt public ID",
    )
    show_parser.add_argument(
        "--history-limit",
        type=int,
        default=DEFAULT_HISTORY_LIMIT,
        help=(
            f"Maximum verified history versions to show (default {DEFAULT_HISTORY_LIMIT}, "
            f"maximum {MAX_HISTORY_LIMIT})"
        ),
    )
    show_parser.add_argument(
        "--history-offset",
        type=int,
        default=0,
        help="Newest-first history offset for deterministic pagination (default 0)",
    )
    show_parser.add_argument(
        "--history-anchor-version",
        type=int,
        help=(
            "Stable version anchor emitted by the first page; reuse it on "
            "later pages to prevent duplicate/skip if supersession advances"
        ),
    )

    persist_parser = subparsers.add_parser(
        "persist",
        help="Invoke the guarded IAF.2 first-fact-set persistence service",
    )
    _add_write_arguments(persist_parser)

    supersede_parser = subparsers.add_parser(
        "supersede",
        help="Invoke the guarded IAF.3 complete-replacement supersession service",
    )
    _add_write_arguments(supersede_parser)
    return parser


def _add_write_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="Path to staging SQLite database")
    parser.add_argument(
        "--command-file",
        required=True,
        help=(
            "UTF-8 JSON object containing the complete explicit human command; "
            "IDs, hashes, monetary strings, and predecessor expectations are "
            "never generated or substituted"
        ),
    )


def _load_command_file(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    if not path.exists():
        raise CommandFileError(f"command file does not exist: {path_text}")
    if not path.is_file():
        raise CommandFileError(f"command file is not a regular file: {path_text}")
    try:
        with path.open("rb") as command_stream:
            raw = command_stream.read(MAX_COMMAND_FILE_BYTES + 1)
        if len(raw) > MAX_COMMAND_FILE_BYTES:
            raise CommandFileError(
                f"command file exceeds the {MAX_COMMAND_FILE_BYTES}-byte safety limit"
            )
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise CommandFileError(f"command file is not readable strict UTF-8: {exc}") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_json_object)
    except CommandFileError:
        raise
    except (ValueError, RecursionError, MemoryError) as exc:
        raise CommandFileError(f"command file is not safely parseable bounded JSON: {exc}") from exc
    _require_bounded_json_depth(parsed)
    if not isinstance(parsed, dict):
        raise CommandFileError("command file must contain one JSON object")

    actor_type = parsed.get("actor_type", "human")
    if actor_type != "human":
        raise CommandFileError(
            "IAF.4 operations are human-only; actor_type must be omitted or exactly 'human'"
        )
    result = dict(parsed)
    result["actor_type"] = "human"
    return result


def _open_connection(db_path: str, *, readonly: bool) -> sqlite3.Connection:
    """Open only an existing database; review is mode=ro plus query_only."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    if readonly:
        conn = connect_sqlite(path, mode=ConnectionMode.READ_ONLY)
        conn.execute("PRAGMA query_only = ON")
        return conn
    return connect_sqlite(
        f"{path.resolve().as_uri()}?mode=rw",
        mode=ConnectionMode.APPLICATION,
    )


def _run_list(conn: sqlite3.Connection, *, limit: int, out: TextIO) -> None:
    receipts = list_fact_set_review_receipts(conn, limit=limit)
    print(REVIEW_SAFETY_NOTICE, file=out)
    print("", file=out)
    if not receipts:
        print("No B4.1 conversion-created receipts found for fact-set review.", file=out)
        return
    for receipt in receipts:
        print(_summary_line(receipt), file=out)


def _summary_line(receipt: FactSetReviewReceipt) -> str:
    return " | ".join(
        [
            f"review_status={receipt.review_label}",
            f"receipt_public_id={receipt.receipt_public_id}",
            f"merchant={_fmt(receipt.merchant)}",
            f"receipt_datetime={_fmt(receipt.receipt_datetime)}",
            f"net_paid_amount_canonical_text={receipt.net_paid_amount_canonical_text}",
            f"currency={receipt.currency}",
            f"receipt_status={receipt.receipt_status}",
            (f"receipt_transaction_id_is_set={_fmt(receipt.receipt_transaction_id_is_set)}"),
            f"receipt_group_binding_count={receipt.receipt_group_binding_count}",
            f"conversion_command_public_id={receipt.conversion_command_public_id}",
            f"conversion_result_hash={receipt.conversion_result_hash}",
            f"fact_set_state={receipt.fact_set_state}",
            f"fact_set_count={receipt.fact_set_count}",
            f"active_fact_set_public_id={_fmt(receipt.active_fact_set_public_id)}",
            f"active_fact_set_version={_fmt(receipt.active_fact_set_version)}",
            f"active_fact_set_result_hash={_fmt(receipt.active_fact_set_result_hash)}",
        ]
    )


def _run_show(
    conn: sqlite3.Connection,
    *,
    receipt_public_id: str,
    history_limit: int,
    history_offset: int,
    history_anchor_version: int | None,
    out: TextIO,
) -> None:
    detail = get_fact_set_review_detail(
        conn,
        receipt_public_id,
        history_limit=history_limit,
        history_offset=history_offset,
        history_anchor_version=history_anchor_version,
    )
    receipt = detail.receipt
    print(REVIEW_SAFETY_NOTICE, file=out)
    print("", file=out)
    for name, value in (
        ("receipt_public_id", receipt.receipt_public_id),
        ("merchant", receipt.merchant),
        ("receipt_datetime", receipt.receipt_datetime),
        ("net_paid_amount_canonical_text", receipt.net_paid_amount_canonical_text),
        ("currency", receipt.currency),
        ("receipt_status", receipt.receipt_status),
        ("receipt_transaction_id_is_set", receipt.receipt_transaction_id_is_set),
        ("receipt_group_binding_count", receipt.receipt_group_binding_count),
        ("payer_participant_public_id", receipt.payer_participant_public_id),
        ("conversion_command_public_id", receipt.conversion_command_public_id),
        ("conversion_result_hash", receipt.conversion_result_hash),
        ("fact_set_count", receipt.fact_set_count),
        ("active_fact_set_public_id", receipt.active_fact_set_public_id),
        ("active_fact_set_version", receipt.active_fact_set_version),
        ("active_fact_set_result_hash", receipt.active_fact_set_result_hash),
    ):
        print(f"{name}: {_fmt(value)}", file=out)

    print("", file=out)
    print("receipt_membership:", file=out)
    for member in detail.membership:
        print(
            "  - "
            + " | ".join(
                [
                    f"participant_public_id={_fmt(member.participant_public_id)}",
                    f"display_name={_fmt(member.display_name)}",
                    f"role={member.role}",
                    f"is_included={_fmt(member.is_included)}",
                ]
            ),
            file=out,
        )

    print("", file=out)
    print("reviewed_command_expectations:", file=out)
    print(
        f"  expected_conversion_command_public_id: {receipt.conversion_command_public_id}",
        file=out,
    )
    print(
        f"  expected_conversion_result_hash: {receipt.conversion_result_hash}",
        file=out,
    )
    if detail.active_fact_set is None:
        print("  operation: persist", file=out)
        print("  expected_current_fact_set: none", file=out)
    else:
        active = detail.active_fact_set
        print("  operation: supersede", file=out)
        print(
            f"  expected_current_fact_set_public_id: {active.fact_set_public_id}",
            file=out,
        )
        print(
            f"  expected_current_fact_set_result_hash: {active.fact_set_result_hash}",
            file=out,
        )

    print("", file=out)
    if receipt.fact_set_count == 0:
        print("No persisted fact-set versions.", file=out)
        return
    print(
        "fact_set_history_page: "
        f"anchor_version={_fmt(detail.history_anchor_version)} | "
        f"offset={detail.history_offset} | limit={detail.history_limit} | "
        f"returned={len(detail.fact_sets)} | "
        f"anchored_total={_fmt(detail.history_anchor_version)} | "
        f"current_total={receipt.fact_set_count} | "
        f"has_more={_fmt(detail.history_has_more)}",
        file=out,
    )
    if not detail.fact_sets:
        print("No fact-set versions in this history page.", file=out)
        return
    for version in detail.fact_sets:
        print(
            "  - "
            + " | ".join(
                [
                    f"fact_set_version={version.fact_set_version}",
                    f"fact_set_status={'active' if version.is_active else 'superseded'}",
                    f"fact_set_public_id={version.fact_set_public_id}",
                    f"fact_set_result_hash={version.fact_set_result_hash}",
                    f"command_public_id={version.command_public_id}",
                    f"supersedes_fact_set_public_id={_fmt(version.supersedes_fact_set_public_id)}",
                    (
                        "superseded_by_fact_set_public_id="
                        f"{_fmt(version.superseded_by_fact_set_public_id)}"
                    ),
                    f"item_count={version.item_count}",
                    f"allocation_count={version.allocation_count}",
                    f"adjustment_count={version.adjustment_count}",
                    f"audit_event_public_id={version.audit_event_public_id}",
                ]
            ),
            file=out,
        )
        print(
            "    canonical_fact_set_payload: "
            f"{_terminal_safe_canonical_json(version.canonical_fact_set_payload)}",
            file=out,
        )


def _run_persist(
    conn: sqlite3.Connection,
    command_data: Mapping[str, Any],
    *,
    out: TextIO,
) -> None:
    command = ReceiptItemAllocationFactsCommand.from_mapping(command_data)
    result = persist_receipt_item_allocation_facts(conn, command)
    _print_result(result, operation="persist", out=out)


def _run_supersede(
    conn: sqlite3.Connection,
    command_data: Mapping[str, Any],
    *,
    out: TextIO,
) -> None:
    command = ReceiptItemAllocationFactsSupersessionCommand.from_mapping(command_data)
    result = supersede_receipt_item_allocation_facts(conn, command)
    _print_result(result, operation="supersede", out=out)


def _print_result(
    result: ReceiptItemAllocationFactsResult,
    *,
    operation: str,
    out: TextIO,
) -> None:
    print(WRITE_SAFETY_NOTICE, file=out)
    for name, value in (
        ("operation", operation),
        ("command_public_id", result.command_public_id),
        ("receipt_public_id", result.receipt_public_id),
        ("fact_set_public_id", result.fact_set_public_id),
        ("fact_set_version", result.fact_set_version),
        ("conversion_command_public_id", result.conversion_command_public_id),
        ("command_material_hash", result.command_material_hash),
        ("fact_set_input_hash", result.fact_set_input_hash),
        ("fact_set_result_hash", result.fact_set_result_hash),
        ("item_count", result.item_count),
        ("allocation_count", result.allocation_count),
        ("adjustment_count", result.adjustment_count),
        ("audit_event_public_id", result.audit_event_public_id),
        ("supersedes_fact_set_public_id", result.supersedes_fact_set_public_id),
        ("superseded_fact_set_result_hash", result.superseded_fact_set_result_hash),
        ("idempotent_replay", result.idempotent),
    ):
        print(f"{name}: {_fmt(value)}", file=out)
    print("calculator_ready: not evaluated", file=out)


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        # Preserve content through an escaped representation while preventing
        # newline, ANSI, and other controls from forging operator lines.
        return json.dumps(value, ensure_ascii=True)[1:-1]
    return json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)


def _terminal_safe_canonical_json(text: str) -> str:
    """Keep JSON structure readable while escaping all non-ASCII/control text."""
    return json.dumps(
        json.loads(text),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
