"""Immutable, versioned source identity for reconciliation review queues.

This module only constructs and reads source evidence. It never repairs an old
queue row or changes the caller's transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from finance_core.application.correction_schema import verify_correction_schema
from finance_core.reconciliation.models import AppTransaction, ReviewQueueItem

_APP_KEYS = frozenset(
    {
        "app_txn_id",
        "transaction_date",
        "merchant",
        "amount",
        "currency",
        "source_type",
        "source_channel",
        "normalized_merchant",
        "posted_date",
        "transaction_type",
    }
)
_BINDING_KEYS = frozenset(
    {
        "version",
        "queue_item_id",
        "candidate_id",
        "statement_transaction_ref",
        "app_transaction_ref",
        "app_transaction_count",
        "app_transactions",
        "sha256",
    }
)


@dataclass(frozen=True)
class BoundQueue:
    queue_item_id: str
    candidate_id: str
    statement_transaction_ref: str | None
    app_transaction_ref: str | None
    app_transactions: tuple[AppTransaction, ...]
    sha256: str


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _app_snapshot(app: AppTransaction) -> dict[str, str | None]:
    txn_type = app.transaction_type
    if isinstance(txn_type, Enum):
        txn_type = txn_type.value
    return {
        "app_txn_id": app.app_txn_id,
        "transaction_date": app.transaction_date.isoformat(),
        "merchant": app.merchant,
        "amount": str(app.amount),
        "currency": app.currency,
        "source_type": app.source_type,
        "source_channel": app.source_channel,
        "normalized_merchant": app.normalized_merchant,
        "posted_date": app.posted_date.isoformat() if app.posted_date else None,
        "transaction_type": txn_type,
    }


def _restore_app(raw: object) -> AppTransaction:
    if not isinstance(raw, dict) or set(raw) != _APP_KEYS:
        raise ValueError("bound queue app snapshot is incomplete")
    for key in ("app_txn_id", "transaction_date", "merchant", "amount", "currency"):
        if not isinstance(raw[key], str) or not raw[key]:
            raise ValueError(f"bound queue app snapshot has invalid {key}")
    if not raw["app_txn_id"].strip():
        raise ValueError("bound queue app identity is empty")
    for key in ("source_type", "source_channel", "normalized_merchant", "transaction_type"):
        if raw[key] is not None and not isinstance(raw[key], str):
            raise ValueError(f"bound queue app snapshot has invalid {key}")
    if raw["posted_date"] is not None and not isinstance(raw["posted_date"], str):
        raise ValueError("bound queue app snapshot has invalid posted_date")
    try:
        txn_date = date.fromisoformat(raw["transaction_date"])
        posted_date = date.fromisoformat(raw["posted_date"]) if raw["posted_date"] else None
        amount = Decimal(raw["amount"])
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("bound queue app snapshot has invalid date or amount") from exc
    if (
        txn_date.isoformat() != raw["transaction_date"]
        or (posted_date is not None and posted_date.isoformat() != raw["posted_date"])
        or not amount.is_finite()
        or amount <= 0
    ):
        raise ValueError("bound queue app snapshot has invalid date or amount")
    return AppTransaction(
        app_txn_id=raw["app_txn_id"],
        transaction_date=txn_date,
        merchant=raw["merchant"],
        amount=amount,
        currency=raw["currency"],
        source_type=raw["source_type"],
        source_channel=raw["source_channel"],
        normalized_merchant=raw["normalized_merchant"],
        posted_date=posted_date,
        transaction_type=raw["transaction_type"],
    )


def _source_material(item: ReviewQueueItem) -> dict[str, Any]:
    candidate = item.candidate
    if (
        not isinstance(item.queue_item_id, str)
        or not item.queue_item_id.strip()
        or not isinstance(candidate.candidate_id, str)
        or not candidate.candidate_id.strip()
    ):
        raise ValueError("bound queue identity is empty")
    statement = candidate.statement
    statement_ref = statement.statement_row_reference or statement.merchant_raw
    if not isinstance(statement_ref, str) or not statement_ref.strip():
        raise ValueError("bound queue statement identity is empty")
    best = candidate.best_app_transaction
    app_ref = best.app_txn_id if best else None
    apps = tuple(candidate.all_app_transactions)
    snapshots = [_app_snapshot(app) for app in apps]
    # Use the same strict decoder for new and stored evidence.
    restored = tuple(_restore_app(raw) for raw in snapshots)
    ids = [app.app_txn_id for app in restored]
    if len(ids) != len(set(ids)):
        raise ValueError("bound queue app IDs must be unique")
    if best is None:
        if app_ref is not None:
            raise ValueError("bound queue canonical app identity conflicts")
    elif not any(_app_snapshot(app) == _app_snapshot(best) for app in restored):
        raise ValueError("bound queue canonical app is absent from matcher output")
    return {
        "version": 1,
        "queue_item_id": item.queue_item_id,
        "candidate_id": candidate.candidate_id,
        "statement_transaction_ref": statement_ref,
        "app_transaction_ref": app_ref,
        "app_transaction_count": len(snapshots),
        "app_transactions": snapshots,
    }


def build_source_binding(item: ReviewQueueItem) -> dict[str, Any]:
    """Capture the complete candidate set at first queue registration."""
    material = _source_material(item)
    return {**material, "sha256": hashlib.sha256(_json(material).encode()).hexdigest()}


def load_bound_queue(conn: sqlite3.Connection, queue_id: str) -> BoundQueue | None:
    """Read a validated 051 binding; None means migration 051 is absent only."""
    if not verify_correction_schema(conn):
        return None
    row = conn.execute(
        "SELECT public_id, candidate_id, statement_transaction_ref, app_transaction_ref, "
        "evidence_json FROM reconciliation_review_queue WHERE public_id = ?",
        (queue_id,),
    ).fetchone()
    if row is None:
        raise ValueError("bound queue row is missing")
    try:
        evidence = json.loads(row[4])
    except (TypeError, ValueError) as exc:
        raise ValueError("bound queue evidence is invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("bound queue evidence is invalid")
    raw = evidence.get("source_binding")
    if (
        not isinstance(raw, dict)
        or set(raw) != _BINDING_KEYS
        or type(raw["version"]) is not int
        or raw["version"] != 1
    ):
        raise ValueError("bound queue source binding is missing or invalid")
    for key, row_index in (
        ("queue_item_id", 0),
        ("candidate_id", 1),
        ("statement_transaction_ref", 2),
        ("app_transaction_ref", 3),
    ):
        if raw[key] != row[row_index]:
            raise ValueError(f"bound queue {key} conflicts with canonical row")
    if not isinstance(raw["queue_item_id"], str) or not raw["queue_item_id"].strip():
        raise ValueError("bound queue identity is invalid")
    if not isinstance(raw["candidate_id"], str) or not raw["candidate_id"].strip():
        raise ValueError("bound queue candidate identity is invalid")
    for key in ("statement_transaction_ref", "app_transaction_ref"):
        if raw[key] is not None and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise ValueError(f"bound queue {key} is invalid")
    if raw["statement_transaction_ref"] is None:
        raise ValueError("bound queue statement identity is missing")
    apps_raw = raw["app_transactions"]
    if (
        not isinstance(apps_raw, list)
        or not isinstance(raw["app_transaction_count"], int)
        or isinstance(raw["app_transaction_count"], bool)
        or raw["app_transaction_count"] != len(apps_raw)
    ):
        raise ValueError("bound queue app member count is invalid")
    apps = tuple(_restore_app(app) for app in apps_raw)
    ids = [app.app_txn_id for app in apps]
    if len(ids) != len(set(ids)):
        raise ValueError("bound queue app IDs must be unique")
    if raw["app_transaction_ref"] is not None and raw["app_transaction_ref"] not in ids:
        raise ValueError("bound queue canonical app is absent")
    if raw["app_transaction_ref"] is None and evidence.get("app_txn_id") is not None:
        raise ValueError("bound queue canonical app conflicts with evidence")
    if "all_app_transactions" not in evidence or evidence["all_app_transactions"] != apps_raw:
        raise ValueError("bound queue app snapshot conflicts with queue evidence")
    if evidence.get("candidate_id") != raw["candidate_id"]:
        raise ValueError("bound queue candidate conflicts with queue evidence")
    if evidence.get("app_txn_id") != raw["app_transaction_ref"]:
        raise ValueError("bound queue app conflicts with queue evidence")
    digest = raw["sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("bound queue digest is invalid")
    material = {k: v for k, v in raw.items() if k != "sha256"}
    expected = hashlib.sha256(_json(material).encode()).hexdigest()
    if digest != expected:
        raise ValueError("bound queue digest mismatch")
    return BoundQueue(
        queue_item_id=raw["queue_item_id"],
        candidate_id=raw["candidate_id"],
        statement_transaction_ref=raw["statement_transaction_ref"],
        app_transaction_ref=raw["app_transaction_ref"],
        app_transactions=apps,
        sha256=digest,
    )


def assert_candidate_matches_bound(bound: BoundQueue, item: ReviewQueueItem) -> None:
    """Reject a caller candidate that differs from the persisted source."""
    proposed = build_source_binding(item)
    if proposed["sha256"] != bound.sha256:
        raise ValueError("candidate differs from bound queue source")
