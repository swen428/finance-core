"""Immutable, versioned source identity for reconciliation review queues.

This module only constructs and reads source evidence. It never repairs an old
queue row or changes the caller's transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import types
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from finance_core.application.correction_schema import verify_correction_schema
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    MatchResult,
    MatchStatus,
    ReviewQueueItem,
    priority_for_issue,
    review_priority_for_issue,
    suggested_action_for_issue,
)

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
        "issue_type",
        "statement_transaction_ref",
        "app_transaction_ref",
        "app_transaction_count",
        "app_transactions",
        "decision_source",
        "sha256",
    }
)


@dataclass(frozen=True)
class BoundQueue:
    queue_item_id: str
    candidate_id: str
    issue_type: IssueType
    statement_transaction_ref: str | None
    app_transaction_ref: str | None
    app_transactions: tuple[AppTransaction, ...]
    sha256: str
    item: ReviewQueueItem


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _strict_json(raw: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("bound queue JSON has duplicate key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError(f"bound queue JSON has invalid constant {value}")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (TypeError, ValueError) as exc:
        raise ValueError("bound queue JSON is invalid") from exc


def _encode(value: Any) -> Any:
    """Encode only the typed domain values that form a review decision."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("bound queue decimal must be finite")
        return str(value)
    if isinstance(value, tuple):
        return [_encode(entry) for entry in value]
    if value is None or type(value) in (str, int, float, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("bound queue float must be finite")
        return value
    raise ValueError(f"unsupported bound queue value: {type(value).__name__}")


def _decode(annotation: Any, raw: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        for member in get_args(annotation):
            try:
                return _decode(member, raw)
            except (TypeError, ValueError):
                continue
        raise ValueError("bound queue source value has invalid union type")
    if annotation is type(None):
        if raw is None:
            return None
        raise ValueError("bound queue source value must be null")
    if origin is tuple:
        args = get_args(annotation)
        if not isinstance(raw, list) or len(args) != 2 or args[1] is not Ellipsis:
            raise ValueError("bound queue source tuple is invalid")
        return tuple(_decode(args[0], entry) for entry in raw)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if not isinstance(raw, str):
            raise ValueError("bound queue source enum is invalid")
        return annotation(raw)
    if annotation is date:
        if not isinstance(raw, str):
            raise ValueError("bound queue source date is invalid")
        date_value = date.fromisoformat(raw)
        if date_value.isoformat() != raw:
            raise ValueError("bound queue source date is not canonical")
        return date_value
    if annotation is Decimal:
        if not isinstance(raw, str):
            raise ValueError("bound queue source decimal is invalid")
        try:
            decimal_value = Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError("bound queue source decimal is invalid") from exc
        if not decimal_value.is_finite() or str(decimal_value) != raw:
            raise ValueError("bound queue source decimal is not canonical")
        return decimal_value
    if annotation in (str, int, float, bool):
        if type(raw) is not annotation:
            raise ValueError("bound queue source scalar has invalid type")
        if annotation is float and not math.isfinite(raw):
            raise ValueError("bound queue source float must be finite")
        return raw
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _decode_dataclass(annotation, raw)
    raise ValueError("bound queue source annotation is unsupported")


def _decode_dataclass(cls: type[Any], raw: Any) -> Any:
    hints = get_type_hints(cls)
    names = {field.name for field in fields(cls)}
    if not isinstance(raw, dict) or set(raw) != names:
        raise ValueError(f"bound queue {cls.__name__} snapshot is incomplete")
    decoded = {name: _decode(hints[name], raw[name]) for name in names}
    try:
        value = cls(**decoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bound queue {cls.__name__} snapshot is invalid") from exc
    if _encode(value) != raw:
        raise ValueError(f"bound queue {cls.__name__} snapshot is not canonical")
    return value


def _assert_runtime_type(annotation: Any, value: Any) -> None:
    """Reject model fields whose runtime type differs from their declared type."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        for member in get_args(annotation):
            try:
                _assert_runtime_type(member, value)
                return
            except ValueError:
                continue
        raise ValueError("bound queue model has invalid union runtime type")
    if annotation is type(None):
        if value is not None:
            raise ValueError("bound queue model value must be null")
        return
    if origin is tuple:
        args = get_args(annotation)
        if type(value) is not tuple or len(args) != 2 or args[1] is not Ellipsis:
            raise ValueError("bound queue model tuple has invalid type")
        for entry in value:
            _assert_runtime_type(args[0], entry)
        return
    if isinstance(annotation, type) and is_dataclass(annotation):
        if type(value) is not annotation:
            raise ValueError("bound queue model dataclass has invalid type")
        hints = get_type_hints(annotation)
        for field in fields(annotation):
            _assert_runtime_type(hints[field.name], getattr(value, field.name))
        return
    if isinstance(annotation, type) and type(value) is annotation:
        return
    raise ValueError("bound queue model scalar has invalid runtime type")


def _decision_source(item: ReviewQueueItem) -> dict[str, Any]:
    return _encode(item)


def _restore_decision_source(raw: Any) -> ReviewQueueItem:
    item = _decode_dataclass(ReviewQueueItem, raw)
    _validate_item_contract(item)
    return item


def _validate_item_contract(item: ReviewQueueItem) -> None:
    candidate = item.candidate
    if item.issue_type != candidate.issue_type:
        raise ValueError("bound queue candidate issue conflicts with queue issue")
    if item.reason_codes != candidate.reason_codes:
        raise ValueError("bound queue candidate reasons conflict with queue reasons")
    if item.suggested_action != suggested_action_for_issue(item.issue_type):
        raise ValueError("bound queue suggested action conflicts with queue issue")
    if item.priority != priority_for_issue(item.issue_type):
        raise ValueError("bound queue priority conflicts with queue issue")
    if candidate.review_priority != review_priority_for_issue(item.issue_type):
        raise ValueError("bound queue candidate review priority conflicts with issue")
    if candidate.is_review_required != (item.issue_type != IssueType.MATCHED):
        raise ValueError("bound queue review-required flag conflicts with issue")
    if not candidate.confidence_score.is_finite() or not (
        Decimal("0") <= candidate.confidence_score <= Decimal("1")
    ):
        raise ValueError("bound queue confidence is out of range")
    from finance_core.reconciliation.matching import _result_to_issue_type

    if item.issue_type == IssueType.MISSING_IN_STATEMENT:
        if candidate.match_status != MatchStatus.NO_MATCH or candidate.best_app_transaction is None:
            raise ValueError("bound queue synthetic missing statement classification is invalid")
    else:
        match_result = MatchResult(
            status=candidate.match_status,
            reasons=candidate.reason_codes,
        )
        if item.issue_type != _result_to_issue_type(match_result):
            raise ValueError("bound queue issue conflicts with matcher status")
    evidence = candidate.evidence
    if evidence is not None:
        statement = candidate.statement
        best = candidate.best_app_transaction
        repeated: tuple[tuple[str, Any, Any], ...] = (
            ("statement_amount", evidence.statement_amount, statement.amount),
            ("statement_currency", evidence.statement_currency, statement.currency),
            ("statement_txn_date", evidence.statement_txn_date, statement.transaction_date),
            ("statement_posted_date", evidence.statement_posted_date, statement.posted_date),
            ("statement_merchant", evidence.statement_merchant, statement.merchant_raw),
            (
                "statement_direction",
                evidence.statement_direction,
                statement.amount_direction.value if statement.amount_direction else None,
            ),
            ("original_amount_text", evidence.original_amount_text, statement.raw_amount),
        )
        for name, recorded, source in repeated:
            if recorded is not None and recorded != source:
                raise ValueError(f"bound queue match evidence {name} conflicts with statement")
        if best is not None:
            app_fields: tuple[tuple[str, Any, Any], ...] = (
                ("candidate_amount", evidence.candidate_amount, best.amount),
                ("candidate_currency", evidence.candidate_currency, best.currency),
                ("candidate_txn_date", evidence.candidate_txn_date, best.transaction_date),
                ("candidate_merchant", evidence.candidate_merchant, best.merchant),
            )
            for name, recorded, source in app_fields:
                if recorded is not None and recorded != source:
                    raise ValueError(f"bound queue match evidence {name} conflicts with app")
    from finance_core.reconciliation.review_queue import generate_review_queue

    generated, _ = generate_review_queue([candidate])
    expected = generated[0]
    if item.evidence_summary != expected.evidence_summary:
        raise ValueError("bound queue summary conflicts with matcher evidence")
    if item.structured_evidence != expected.structured_evidence:
        raise ValueError("bound queue structured evidence conflicts with matcher evidence")


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
    _assert_runtime_type(ReviewQueueItem, item)
    _validate_item_contract(item)
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
    if apps and best is None:
        raise ValueError("bound queue canonical app is missing from candidate set")
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
        "version": 2,
        "queue_item_id": item.queue_item_id,
        "candidate_id": candidate.candidate_id,
        "issue_type": item.issue_type.value,
        "statement_transaction_ref": statement_ref,
        "app_transaction_ref": app_ref,
        "app_transaction_count": len(snapshots),
        "app_transactions": snapshots,
        "decision_source": _decision_source(item),
    }


def _outer_source_evidence(item: ReviewQueueItem) -> dict[str, Any]:
    candidate = item.candidate
    statement = candidate.statement
    result: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "match_status": candidate.match_status.value,
        "issue_type": candidate.issue_type.value,
        "confidence_score": str(candidate.confidence_score),
        "statement_merchant": statement.merchant_raw,
        "statement_amount": str(statement.amount) if statement.amount is not None else None,
        "statement_currency": statement.currency,
    }
    if statement.transaction_date:
        result["statement_txn_date"] = statement.transaction_date.isoformat()
    if statement.posted_date:
        result["statement_posted_date"] = statement.posted_date.isoformat()
    best = candidate.best_app_transaction
    if best is not None:
        result.update(
            app_txn_id=best.app_txn_id,
            app_merchant=best.merchant,
            app_amount=str(best.amount),
            app_currency=best.currency,
            app_txn_date=best.transaction_date.isoformat(),
        )
    result["all_app_transactions"] = [_app_snapshot(app) for app in candidate.all_app_transactions]
    return result


def build_source_binding(item: ReviewQueueItem) -> dict[str, Any]:
    """Capture the complete candidate set at first queue registration."""
    material = _source_material(item)
    return {**material, "sha256": hashlib.sha256(_json(material).encode()).hexdigest()}


def load_bound_queue(conn: sqlite3.Connection, queue_id: str) -> BoundQueue | None:
    """Read a validated 051 binding; None means migration 051 is absent only."""
    if not verify_correction_schema(conn):
        return None
    row = conn.execute(
        "SELECT public_id, candidate_id, issue_type, suggested_action, priority, "
        "statement_transaction_ref, app_transaction_ref, confidence_score, "
        "reason_codes_json, evidence_json "
        "FROM reconciliation_review_queue WHERE public_id = ?",
        (queue_id,),
    ).fetchone()
    if row is None:
        raise ValueError("bound queue row is missing")
    return _load_bound_row(tuple(row))


def restore_bound_review_queue_item(row: sqlite3.Row) -> ReviewQueueItem:
    """Restore an exact 051 source for CLI use, checking the complete stored row."""
    columns = (
        "public_id",
        "candidate_id",
        "issue_type",
        "suggested_action",
        "priority",
        "statement_transaction_ref",
        "app_transaction_ref",
        "confidence_score",
        "reason_codes_json",
        "evidence_json",
    )
    return _load_bound_row(tuple(row[key] for key in columns)).item


def _load_bound_row(row: tuple[Any, ...]) -> BoundQueue:
    evidence = _strict_json(row[9])
    if not isinstance(evidence, dict):
        raise ValueError("bound queue evidence is invalid")
    raw = evidence.get("source_binding")
    if (
        not isinstance(raw, dict)
        or set(raw) != _BINDING_KEYS
        or type(raw["version"]) is not int
        or raw["version"] != 2
    ):
        raise ValueError("bound queue source binding is missing or invalid")
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
    item = _restore_decision_source(raw["decision_source"])
    if material != _source_material(item):
        raise ValueError("bound queue source material conflicts with snapshot")
    row_expected: tuple[Any, ...] = (
        item.queue_item_id,
        item.candidate.candidate_id,
        item.issue_type.value,
        item.suggested_action.value,
        item.priority,
        material["statement_transaction_ref"],
        material["app_transaction_ref"],
        str(item.candidate.confidence_score),
        [code.value for code in item.reason_codes],
    )
    row_actual = tuple(row[:8]) + (_strict_json(row[8]),)
    if row_actual != row_expected:
        raise ValueError("bound queue canonical row conflicts with source snapshot")
    outer_evidence = {key: value for key, value in evidence.items() if key != "source_binding"}
    if outer_evidence != _outer_source_evidence(item):
        raise ValueError("bound queue evidence conflicts with source snapshot")
    return BoundQueue(
        queue_item_id=item.queue_item_id,
        candidate_id=item.candidate.candidate_id,
        issue_type=item.issue_type,
        statement_transaction_ref=material["statement_transaction_ref"],
        app_transaction_ref=material["app_transaction_ref"],
        app_transactions=item.candidate.all_app_transactions,
        sha256=digest,
        item=item,
    )


def bound_source_audit_projection(bound: BoundQueue) -> dict[str, Any]:
    """Canonical source fields required in successful resolution/apply audits."""
    item = bound.item
    candidate = item.candidate
    statement = candidate.statement
    result: dict[str, Any] = {
        "queue_item_id": item.queue_item_id,
        "candidate_id": candidate.candidate_id,
        "issue_type": item.issue_type.value,
        "confidence_score": str(candidate.confidence_score),
        "reason_codes": [code.value for code in item.reason_codes],
        "evidence_summary": item.evidence_summary,
        "statement_merchant": statement.merchant_raw,
        "statement_amount": str(statement.amount) if statement.amount is not None else None,
        "statement_currency": statement.currency,
    }
    if statement.transaction_date is not None:
        result["statement_txn_date"] = statement.transaction_date.isoformat()
    if statement.posted_date is not None:
        result["statement_posted_date"] = statement.posted_date.isoformat()
    app = candidate.best_app_transaction
    if app is not None:
        result.update(
            app_txn_id=app.app_txn_id,
            app_merchant=app.merchant,
            app_amount=str(app.amount),
            app_currency=app.currency,
            app_txn_date=app.transaction_date.isoformat(),
        )
    return result


def assert_candidate_matches_bound(bound: BoundQueue, item: ReviewQueueItem) -> None:
    """Reject a caller candidate that differs from the persisted source."""
    proposed = build_source_binding(item)
    if proposed["sha256"] != bound.sha256:
        raise ValueError("candidate differs from bound queue source")
