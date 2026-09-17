"""Authenticated human receipt total-proposal monetary-correction boundary.

Non-monetary edits continue through :func:`finance_core.parser_proposals.completion.
complete_proposal`.  This module provides the sole public entry point for
monetary (amount/currency) corrections of receipt total proposals.  A
successful correction never mutates the original proposal payload: it creates
an append-only revision record (migration 034), a new child proposal row that
supersedes the parent, a ``superseding_correction`` OCR link (migration 033),
and explicit human correction evidence.  The replacement returns to
``parsed_pending_confirmation`` and requires a fresh confirmation through the
existing ``confirm_proposal()`` boundary; nothing here confirms, converts,
finalizes, calculates, or creates final financial facts.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.money import MoneyValidationError, money_decimal, normalize_currency
from finance_core.parser_proposals.content_hash import (
    canonicalize_proposal_money,
    compute_effective_proposal_content_hash,
)
from finance_core.parser_proposals.conversion_state import (
    has_legacy_transaction_conversion,
    has_receipt_registry_conversion,
)
from finance_core.parser_proposals.effective_payload import (
    EffectivePayloadError,
    resolve_effective_payload,
)
from finance_core.parser_proposals.human_drafts import HumanReasonContributor
from finance_core.parser_proposals.lifecycle import (
    CONFIRMED,
    PARSED_PENDING_CONFIRMATION,
    SUPERSEDED,
    TERMINAL_STATUSES,
    raw_intake_status_for_proposal_status,
    validate_transition,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.staging_guard import require_staging_database

# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class ReceiptSupersessionError(ValueError):
    """Base error for receipt proposal monetary-correction failures."""


class UnauthorizedSupersessionActorError(ReceiptSupersessionError):
    """The supplied command did not identify an authenticated human actor."""


class InvalidCorrectionIdError(ReceiptSupersessionError):
    """The correction_public_id is missing, malformed, or invalid."""


class UnsupportedSupersessionProposalError(ReceiptSupersessionError):
    """The proposal is not a receipt total proposal with OCR link evidence."""


class InvalidSupersessionStatusError(ReceiptSupersessionError):
    """The proposal status or conversion state does not permit correction."""


class StaleSupersessionTargetError(ReceiptSupersessionError):
    """The proposal is no longer the current proposal for its raw intake."""


class RawIntakeBindingError(ReceiptSupersessionError):
    """The raw-intake binding for the proposal is ambiguous or inconsistent."""


class StaleSupersessionContentError(ReceiptSupersessionError):
    """The expected content hash no longer matches the current effective proposal."""


class NonMonetarySupersessionError(ReceiptSupersessionError):
    """The correction carries no monetary field; use complete_proposal()."""


class UnknownSupersessionFieldError(ReceiptSupersessionError):
    """A requested field cannot be corrected through this boundary."""


class InvalidSupersessionFieldValueError(ReceiptSupersessionError):
    """A supplied field value failed canonical (Money Contract) validation."""


class NoMaterialSupersessionChangeError(ReceiptSupersessionError):
    """The field updates produce no material change to the effective content."""


class SupersessionConflictError(ReceiptSupersessionError):
    """A revision with the same correction identity has different material."""


class SupersessionPersistenceError(ReceiptSupersessionError):
    """Persisted supersession evidence failed atomic write or verification."""


@dataclass(frozen=True)
class ReceiptRevisionMaterial:
    """Canonical material for a transaction-internal receipt child revision."""

    source_parser_output_id: int
    expected_content_hash: str
    canonical_supplied_fields: dict[str, object]
    canonical_payload: dict[str, object]
    changed_fields: tuple[str, ...]
    authenticated_actor_id: str
    correction_public_id: str
    correction_channel: str
    reason: str | None
    timestamp: str
    d1_operation_public_id: str | None
    d1_draft_public_id: str | None
    d1_draft_version: int | None
    d1_draft_content_hash: str | None
    human_reply_evidence_public_id: str | None
    explicit_clears: dict[str, tuple[object, object]]
    reason_contributors: tuple[HumanReasonContributor, ...]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_ACTOR_TYPES = frozenset({"human", "user"})
_PERSISTED_ACTOR_TYPE = "human"

_MONETARY_FIELDS = frozenset({"amount", "currency"})
_NON_MONETARY_FIELDS = frozenset({"transaction_date", "merchant", "description", "category"})
_ALLOWED_FIELDS = _MONETARY_FIELDS | _NON_MONETARY_FIELDS

_CORRECTION_ID_RE = re.compile(r"^rcor_[A-Za-z0-9_-]{1,195}$")

LINK_ROLE_SUPERSEDING_CORRECTION = "superseding_correction"
_EVIDENCE_SOURCE_HUMAN = "user_message"
_EVIDENCE_SOURCE_OCR = "ocr"

_DATE_LEN = 10

# ---------------------------------------------------------------------------
# Test-only failure seam
# ---------------------------------------------------------------------------

_failure_injection_hook: Callable[[str], None] | None = None
"""Private test-only failure seam at real supersession write boundaries."""


def _inject_failure(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def supersede_receipt_total_proposal(
    conn: sqlite3.Connection,
    parser_output_id: int,
    *,
    actor: str,
    expected_content_hash: str,
    field_updates: dict[str, Any],
    correction_public_id: str,
    actor_type: str = "human",
    correction_channel: str = "cli",
    reason: str | None = None,
    clock: Callable[[], str] | None = None,
    transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
) -> dict[str, Any]:
    """Supersede a receipt total proposal with an authenticated monetary correction.

    The original proposal, its payload, completions, OCR evidence, and audit
    rows are never overwritten.  A successful correction atomically creates a
    replacement child proposal (``parsed_pending_confirmation``), marks the
    parent ``superseded``, repoints the raw-intake current-proposal pointer,
    and records append-only revision, link, lifecycle, and audit evidence.
    The replacement has no authorization of its own and requires a fresh
    ``confirm_proposal()`` decision.
    """
    _validate_command(
        actor,
        actor_type,
        expected_content_hash,
        field_updates,
        correction_channel,
        correction_public_id,
    )
    require_staging_database(conn)

    _acquire_write_transaction(conn)
    try:
        parent = _require_full_proposal(conn, parser_output_id)

        existing = _get_revision_by_correction_id(conn, correction_public_id)
        if existing is not None:
            _verify_existing_d1_revision_lineage(conn, existing)
            result = _handle_existing_revision(
                conn,
                existing,
                parser_output_id=parser_output_id,
                expected_content_hash=expected_content_hash,
                field_updates=field_updates,
                actor=actor,
                correction_channel=correction_channel,
                correction_public_id=correction_public_id,
            )
            conn.commit()
            return result

        if transaction_guard is not None:
            transaction_guard(conn)

        parent_link = _require_receipt_link(conn, parser_output_id)
        _check_no_conversion(conn, parser_output_id)
        raw_intake = _require_current_raw_intake(conn, parent)
        from_status = str(parent["parse_status"])
        if from_status in TERMINAL_STATUSES and from_status != CONFIRMED:
            raise InvalidSupersessionStatusError(
                f"Proposal in terminal status '{from_status}' cannot be superseded"
            )

        current_hash = compute_effective_proposal_content_hash(conn, parent)
        if expected_content_hash != current_hash:
            raise StaleSupersessionContentError(
                f"Expected hash {expected_content_hash[:16]}... "
                f"!= current effective hash {current_hash[:16]}..."
            )

        from finance_core.parser_proposals.human_revision import (
            HumanRevisionLineageError,
            verify_human_revision_descendant,
        )

        _verified_payload, _verified_completion_id, verified_version = resolve_effective_payload(
            conn, parent
        )
        try:
            verify_human_revision_descendant(
                conn,
                parent,
                content_hash=current_hash,
                proposal_version=verified_version,
            )
        except HumanRevisionLineageError as exc:
            raise ReceiptSupersessionError(
                "Inherited D1 human revision lineage does not verify"
            ) from exc

        effective_payload = _resolve_effective(conn, parent)
        canonical_updates, canonical_amount, canonical_currency = _canonicalize_field_updates(
            field_updates, effective_payload
        )
        # Supplied command fields (canonical_updates) are the deterministic
        # replay/conflict identity; applied_updates are the strictly material
        # changes that alone drive provenance, evidence and changed_fields;
        # the resulting canonical pair is the replacement's monetary
        # representation at the target currency's minor-unit scale.
        applied_updates = _material_field_updates(canonical_updates, effective_payload)
        if not applied_updates:
            raise NoMaterialSupersessionChangeError(
                "Field updates produce no material change to the effective proposal content"
            )
        if not (_MONETARY_FIELDS & applied_updates.keys()):
            raise NonMonetarySupersessionError(
                "Correction carries no material amount or currency change; "
                "non-monetary edits must use complete_proposal()"
            )

        replacement_public_id = _derive_replacement_public_id(correction_public_id)
        link_public_id = _derive_link_public_id(correction_public_id)
        completion_provenance = _latest_completion_provenance(conn, parent)
        child_payload = _build_replacement_payload(
            effective_payload,
            applied_updates,
            parent,
            correction_public_id,
            completion_provenance,
            canonical_amount=canonical_amount,
            canonical_currency=canonical_currency,
        )
        child_payload_json = _canonical_json(child_payload)
        now = _now(clock)

        _inject_failure("before_child_insert")
        replacement_id = _insert_replacement_proposal(
            conn, parent, replacement_public_id, child_payload_json
        )

        _inject_failure("before_field_evidence_insert")
        _insert_replacement_field_evidence(conn, replacement_id, child_payload)

        _inject_failure("before_link_insert")
        _insert_superseding_link(
            conn,
            link_public_id=link_public_id,
            extraction_id=int(parent_link["extraction_id"]),
            parser_output_id=replacement_id,
            parser_contract_version=str(parent_link["parser_contract_version"]),
            input_hash=_correction_input_hash(
                correction_public_id,
                parent,
                current_hash,
                canonical_updates,
                actor,
                correction_channel,
            ),
            result_hash=_sha256_hex(child_payload_json),
            created_at=now,
        )

        replacement_hash = compute_effective_proposal_content_hash(conn, {"id": replacement_id})

        _inject_failure("before_revision_insert")
        _insert_revision(
            conn,
            correction_public_id=correction_public_id,
            superseded_parser_output_id=parser_output_id,
            replacement_parser_output_id=replacement_id,
            superseded_content_hash=current_hash,
            replacement_content_hash=replacement_hash,
            superseded_from_status=from_status,
            field_updates=canonical_updates,
            applied_field_updates=applied_updates,
            replacement_payload_json=child_payload_json,
            actor=actor,
            correction_channel=correction_channel,
            reason=reason,
            created_at=now,
        )

        _inject_failure("before_parent_status_update")
        if from_status != CONFIRMED:
            validate_transition(from_status, SUPERSEDED)
        # else: narrowly guarded receipt-correction exception — a confirmed
        # but not yet converted receipt total proposal may be superseded.
        # Conversion was excluded above; unrelated terminal proposals stay
        # protected by the checks before this point.
        proposals = ParserProposalRepository(conn)
        proposals.update_status(parser_output_id, SUPERSEDED)

        _insert_parent_superseded_event(
            conn,
            parser_output_id=parser_output_id,
            from_status=from_status,
            actor=actor,
            correction_public_id=correction_public_id,
            replacement_public_id=replacement_public_id,
            current_hash=current_hash,
            replacement_hash=replacement_hash,
            changed_fields=sorted(applied_updates.keys()),
            created_at=now,
        )
        _insert_replacement_created_event(
            conn,
            replacement_id=replacement_id,
            actor=actor,
            correction_public_id=correction_public_id,
            parent_public_id=str(parent["public_id"]),
            created_at=now,
        )

        _inject_failure("before_raw_intake_repoint")
        _repoint_raw_intake(conn, int(raw_intake["id"]), replacement_id)

        _inject_failure("before_audit_append")
        _append_supersession_audit(
            conn,
            parent=parent,
            from_status=from_status,
            correction_public_id=correction_public_id,
            replacement_public_id=replacement_public_id,
            current_hash=current_hash,
            replacement_hash=replacement_hash,
            actor=actor,
            changed_fields=sorted(applied_updates.keys()),
            created_at=now,
        )

        _verify_persisted(
            conn,
            replacement_id=replacement_id,
            replacement_public_id=replacement_public_id,
            child_payload_json=child_payload_json,
            correction_public_id=correction_public_id,
            raw_intake_id=int(raw_intake["id"]),
        )

        _inject_failure("before_commit")
        conn.commit()
        return {
            "correction_public_id": correction_public_id,
            "superseded_parser_output_id": parser_output_id,
            "replacement_parser_output_id": replacement_id,
            "replacement_proposal_public_id": replacement_public_id,
            "superseded_content_hash": current_hash,
            "replacement_content_hash": replacement_hash,
            "link_public_id": link_public_id,
            "parent_from_status": from_status,
            "parent_to_status": SUPERSEDED,
            "replacement_parse_status": PARSED_PENDING_CONFIRMATION,
            "changed_fields": sorted(applied_updates.keys()),
            "actor_type": _PERSISTED_ACTOR_TYPE,
            "idempotent": False,
        }
    except ReceiptSupersessionError:
        _rollback_if_needed(conn)
        raise
    except sqlite3.Error as exc:
        _rollback_if_needed(conn)
        raise SupersessionPersistenceError(
            "Receipt proposal supersession could not be persisted atomically"
        ) from exc
    except Exception:
        _rollback_if_needed(conn)
        raise


def supersede_receipt_total_proposal_in_transaction(
    conn: sqlite3.Connection,
    *,
    material: ReceiptRevisionMaterial,
) -> dict[str, object]:
    """Persist a canonical D1 monetary receipt child without owning commit/rollback."""
    if not conn.in_transaction:
        raise ReceiptSupersessionError(
            "Transaction-internal supersession requires a caller-owned transaction"
        )
    if not isinstance(material, ReceiptRevisionMaterial):
        raise ReceiptSupersessionError("Receipt revision material is invalid")
    if (
        material.d1_operation_public_id is None
        or material.d1_draft_public_id is None
        or material.d1_draft_version is None
        or material.d1_draft_content_hash is None
        or material.human_reply_evidence_public_id is None
    ):
        raise ReceiptSupersessionError("D1 receipt revision binding is incomplete")
    _validate_command(
        material.authenticated_actor_id,
        "human",
        material.expected_content_hash,
        {
            field: material.canonical_payload.get(field)
            for field in material.changed_fields
            if material.canonical_payload.get(field) is not None
        },
        material.correction_channel,
        material.correction_public_id,
    )
    parent = _require_full_proposal(conn, material.source_parser_output_id)
    if _get_revision_by_correction_id(conn, material.correction_public_id) is not None:
        raise SupersessionConflictError(
            f"Correction {material.correction_public_id} already exists"
        )
    parent_link = _require_receipt_link(conn, material.source_parser_output_id)
    _check_no_conversion(conn, material.source_parser_output_id)
    _require_current_raw_intake(conn, parent)
    from_status = str(parent["parse_status"])
    if from_status in TERMINAL_STATUSES:
        raise InvalidSupersessionStatusError(
            f"Proposal in terminal status '{from_status}' cannot be superseded"
        )
    current_hash = compute_effective_proposal_content_hash(conn, parent)
    if material.expected_content_hash != current_hash:
        raise StaleSupersessionContentError("D1 receipt revision targets stale content")
    effective_payload = _resolve_effective(conn, parent)
    canonical_amount, canonical_currency = _canonical_monetary_pair(
        {
            "amount": material.canonical_payload.get("amount"),
            "currency": material.canonical_payload.get("currency"),
        },
        effective_payload,
    )
    if (
        material.canonical_payload.get("amount") != canonical_amount
        or material.canonical_payload.get("currency") != canonical_currency
    ):
        raise InvalidSupersessionFieldValueError("D1 canonical payload violates the Money Contract")
    canonical_updates: dict[str, Any] = {}
    for field in material.changed_fields:
        if field not in _ALLOWED_FIELDS:
            raise UnknownSupersessionFieldError(
                f"Field {field!r} cannot be corrected through receipt supersession"
            )
        value = material.canonical_payload.get(field)
        if field == "transaction_date":
            value = _validate_transaction_date(value)
        elif field == "merchant":
            value = _validate_trimmed_text(value, field)
        elif field in {"description", "category"} and value is None:
            clear = material.explicit_clears.get(field)
            if clear is None or clear != (effective_payload.get(field), None):
                raise InvalidSupersessionFieldValueError(
                    f"D1 {field} clear is not bound to its exact before/after values"
                )
        elif field in {"description", "category"}:
            value = _validate_trimmed_text(value, field)
        elif field == "amount":
            value = canonical_amount
        elif field == "currency":
            value = canonical_currency
        canonical_updates[field] = value
    applied_updates = _material_field_updates(canonical_updates, effective_payload)
    if tuple(sorted(applied_updates)) != tuple(sorted(material.changed_fields)):
        raise NoMaterialSupersessionChangeError(
            "D1 receipt revision contains a copied or broadened field patch"
        )
    if not (_MONETARY_FIELDS & applied_updates.keys()):
        raise NonMonetarySupersessionError(
            "D1 monetary supersession requires an actual amount or currency change"
        )
    for field in _ALLOWED_FIELDS:
        if material.canonical_payload.get(field) != (
            applied_updates.get(field, effective_payload.get(field))
        ):
            raise ReceiptSupersessionError(
                "D1 canonical payload disagrees with its exact material changes"
            )
    evidence = conn.execute(
        """
        SELECT evidence.*, drafts.draft_public_id
        FROM parser_human_draft_reply_evidence AS evidence
        JOIN parser_human_drafts AS drafts ON drafts.id = evidence.draft_id
        WHERE evidence.evidence_public_id = ?
        """,
        (material.human_reply_evidence_public_id,),
    ).fetchone()
    if (
        evidence is None
        or evidence["draft_public_id"] != material.d1_draft_public_id
        or evidence["authenticated_actor_id"] != material.authenticated_actor_id
        or not isinstance(evidence["raw_utf8"], bytes)
        or hashlib.sha256(bytes(evidence["raw_utf8"])).hexdigest() != evidence["sha256"]
    ):
        raise ReceiptSupersessionError("D1 receipt reply evidence does not verify")

    replacement_public_id = _derive_replacement_public_id(material.correction_public_id)
    link_public_id = _derive_link_public_id(material.correction_public_id)
    from finance_core.parser_proposals.human_revision import (
        _build_d1_receipt_payload,
        _insert_receipt_human_field_evidence,
    )

    child_payload = _build_d1_receipt_payload(
        conn,
        parent=parent,
        effective_payload=effective_payload,
        applied_updates=applied_updates,
        material=material,
    )
    child_payload_json = _canonical_json(child_payload)
    _inject_failure("before_child_insert")
    replacement_id = _insert_replacement_proposal(
        conn, parent, replacement_public_id, child_payload_json
    )
    conn.execute(
        """
        UPDATE parser_outputs
        SET parser_name = 'human_revision', parser_version = 'd1-human-revision-v1',
            ai_provider = NULL, ai_model = NULL, prompt_version = NULL,
            confidence_score = NULL
        WHERE id = ?
        """,
        (replacement_id,),
    )
    evidence_reference = _canonical_json(
        {
            "correction_public_id": material.correction_public_id,
            "draft_content_hash": material.d1_draft_content_hash,
            "draft_public_id": material.d1_draft_public_id,
            "draft_version": material.d1_draft_version,
            "human_reply_evidence_public_id": material.human_reply_evidence_public_id,
            "operation_public_id": material.d1_operation_public_id,
            "superseded_proposal_public_id": parent["public_id"],
        }
    )
    _inject_failure("before_field_evidence_insert")
    _insert_receipt_human_field_evidence(
        conn,
        child_id=replacement_id,
        parent_id=int(parent["id"]),
        canonical_payload=child_payload,
        changed_fields=tuple(sorted(applied_updates)),
        evidence_reference=evidence_reference,
    )
    _inject_failure("before_link_insert")
    _insert_superseding_link(
        conn,
        link_public_id=link_public_id,
        extraction_id=int(parent_link["extraction_id"]),
        parser_output_id=replacement_id,
        parser_contract_version=str(parent_link["parser_contract_version"]),
        input_hash=_correction_input_hash(
            material.correction_public_id,
            parent,
            current_hash,
            canonical_updates,
            material.authenticated_actor_id,
            material.correction_channel,
        ),
        result_hash=_sha256_hex(child_payload_json),
        created_at=material.timestamp,
    )
    replacement_hash = compute_effective_proposal_content_hash(conn, {"id": replacement_id})
    _inject_failure("before_revision_insert")
    _insert_revision(
        conn,
        correction_public_id=material.correction_public_id,
        superseded_parser_output_id=material.source_parser_output_id,
        replacement_parser_output_id=replacement_id,
        superseded_content_hash=current_hash,
        replacement_content_hash=replacement_hash,
        superseded_from_status=from_status,
        field_updates=canonical_updates,
        applied_field_updates=applied_updates,
        replacement_payload_json=child_payload_json,
        actor=material.authenticated_actor_id,
        correction_channel=material.correction_channel,
        reason=material.reason,
        created_at=material.timestamp,
    )
    return {
        "correction_public_id": material.correction_public_id,
        "superseded_parser_output_id": material.source_parser_output_id,
        "replacement_parser_output_id": replacement_id,
        "replacement_proposal_public_id": replacement_public_id,
        "superseded_content_hash": current_hash,
        "replacement_content_hash": replacement_hash,
        "link_public_id": link_public_id,
        "parent_from_status": from_status,
        "parent_to_status": SUPERSEDED,
        "replacement_parse_status": PARSED_PENDING_CONFIRMATION,
        "changed_fields": sorted(applied_updates),
        "actor_type": _PERSISTED_ACTOR_TYPE,
        "idempotent": False,
    }


# ---------------------------------------------------------------------------
# Command validation
# ---------------------------------------------------------------------------


def _validate_command(
    actor: str,
    actor_type: str,
    expected_content_hash: str,
    field_updates: dict[str, Any],
    correction_channel: str,
    correction_public_id: str,
) -> None:
    if actor_type not in _ALLOWED_ACTOR_TYPES:
        raise UnauthorizedSupersessionActorError(
            f"Only authenticated human actors may correct receipt proposals, got: {actor_type}"
        )
    if not isinstance(actor, str) or not actor:
        raise UnauthorizedSupersessionActorError("Authenticated actor must be a non-empty string")
    if actor != actor.strip():
        raise UnauthorizedSupersessionActorError(
            "Authenticated actor must not have leading or trailing whitespace"
        )
    if not isinstance(expected_content_hash, str) or len(expected_content_hash) != 64:
        raise ReceiptSupersessionError("expected_content_hash must be a 64-character hex string")
    if not all(c in "0123456789abcdef" for c in expected_content_hash):
        raise ReceiptSupersessionError("expected_content_hash must be lowercase hex (0-9, a-f)")
    if not isinstance(correction_public_id, str) or not _CORRECTION_ID_RE.match(
        correction_public_id
    ):
        raise InvalidCorrectionIdError(
            "correction_public_id must match 'rcor_' plus 1-195 characters of [A-Za-z0-9_-]"
        )
    if not isinstance(correction_channel, str) or not correction_channel:
        raise ReceiptSupersessionError("correction_channel must be a non-empty string")
    if correction_channel != correction_channel.strip():
        raise ReceiptSupersessionError(
            "correction_channel must not have leading or trailing whitespace"
        )
    if not isinstance(field_updates, dict) or not field_updates:
        raise ReceiptSupersessionError("field_updates must be a non-empty dict")
    for key, value in field_updates.items():
        if not isinstance(key, str) or key not in _ALLOWED_FIELDS:
            raise UnknownSupersessionFieldError(
                f"Field {key!r} cannot be corrected through receipt supersession"
            )
        if isinstance(value, (dict, list, tuple, set)):
            raise UnknownSupersessionFieldError(
                f"Field {key!r} must be a scalar value, nested structures are rejected"
            )
    if not (_MONETARY_FIELDS & field_updates.keys()):
        raise NonMonetarySupersessionError(
            "Correction carries no amount or currency change; "
            "non-monetary edits must use complete_proposal()"
        )


def _require_full_proposal(conn: sqlite3.Connection, parser_output_id: int) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT * FROM parser_outputs WHERE id = ?",
        (parser_output_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ReceiptSupersessionError(f"parser output not found: {parser_output_id}")
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((col[0] for col in cursor.description), row, strict=True))


def _require_receipt_link(conn: sqlite3.Connection, parser_output_id: int) -> dict[str, Any]:
    cursor = conn.execute(
        """
        SELECT id, public_id, extraction_id, parser_contract_version, link_role
        FROM receipt_ocr_proposal_links
        WHERE parser_output_id = ?
        """,
        (parser_output_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise UnsupportedSupersessionProposalError(
            "Only receipt total proposals with persisted OCR link evidence can be superseded"
        )
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((col[0] for col in cursor.description), row, strict=True))


def _check_no_conversion(conn: sqlite3.Connection, parser_output_id: int) -> None:
    if has_legacy_transaction_conversion(conn, parser_output_id):
        raise InvalidSupersessionStatusError(
            f"Proposal {parser_output_id} has already been converted; "
            "historical final-fact correction is outside this boundary"
        )
    # B4.0 mutual exclusion: a proposal recorded in the future receipt
    # conversion registry has produced canonical receipt facts and can no
    # longer be superseded at the proposal layer.
    if has_receipt_registry_conversion(conn, parser_output_id):
        raise InvalidSupersessionStatusError(
            f"Proposal {parser_output_id} is already recorded in the receipt "
            "conversion registry; historical final-fact correction is "
            "outside this boundary"
        )


def _require_current_raw_intake(conn: sqlite3.Connection, parent: dict[str, Any]) -> dict[str, Any]:
    """Resolve the exactly-one raw-intake row bound to the parent, fail closed.

    ``raw_intake_records.parser_output_id`` is not unique, so cardinality is
    checked explicitly before any write: zero rows is a stale target, more
    than one row is an ambiguous binding, and the single row must agree with
    the proposal's expected source identity (raw-intake public id, attachment
    identity, and lifecycle status) before it may be repointed.
    """
    parser_output_id = int(parent["id"])
    cursor = conn.execute(
        """
        SELECT id, public_id, attachment_id, status
        FROM raw_intake_records
        WHERE parser_output_id = ?
        ORDER BY id
        """,
        (parser_output_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise StaleSupersessionTargetError(
            "Only the current proposal for a raw intake record can be superseded"
        )
    if len(rows) > 1:
        raise RawIntakeBindingError(
            f"Proposal {parser_output_id} is bound to {len(rows)} raw intake records; "
            "ambiguous source binding fails closed with zero writes"
        )
    row = rows[0]
    if isinstance(row, sqlite3.Row):
        intake = dict(row)
    else:
        intake = dict(zip((col[0] for col in cursor.description), row, strict=True))

    source_public_id = parent.get("source_public_id")
    if source_public_id is not None and intake["public_id"] != source_public_id:
        raise RawIntakeBindingError(
            f"Raw intake record {intake['public_id']!r} does not match the proposal's "
            f"source identity {source_public_id!r}"
        )
    parent_attachment_id = parent.get("attachment_id")
    if (
        parent_attachment_id is not None
        and intake["attachment_id"] is not None
        and intake["attachment_id"] != parent_attachment_id
    ):
        raise RawIntakeBindingError(
            "Raw intake attachment identity does not match the proposal's attachment"
        )
    expected_status = raw_intake_status_for_proposal_status(str(parent["parse_status"]))
    if intake["status"] != expected_status:
        raise RawIntakeBindingError(
            f"Raw intake status {intake['status']!r} disagrees with the proposal "
            f"lifecycle status (expected {expected_status!r})"
        )
    return intake


def _resolve_effective(conn: sqlite3.Connection, proposal: dict[str, Any]) -> dict[str, Any]:
    try:
        effective, _cid, _version = resolve_effective_payload(conn, proposal)
    except EffectivePayloadError as exc:
        raise ReceiptSupersessionError(str(exc)) from exc
    return effective


# ---------------------------------------------------------------------------
# Field canonicalization (Money Contract)
# ---------------------------------------------------------------------------


def _canonicalize_field_updates(
    field_updates: dict[str, Any],
    effective_payload: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    """Canonicalize the supplied command fields and the resulting monetary pair.

    Returns ``(canonical_updates, canonical_amount, canonical_currency)``:
    the canonical caller-supplied fields (deterministic replay/conflict
    identity, echoed values included) plus the *resulting* effective
    amount/currency pair canonicalized at the target currency's minor-unit
    scale.  The pair is validated fail closed regardless of which monetary
    field the caller supplied, so a currency change whose existing amount
    violates the target currency's Money Contract is rejected here.
    """
    canonical: dict[str, Any] = {}
    canonical_amount, canonical_currency = _canonical_monetary_pair(
        field_updates, effective_payload
    )
    if "amount" in field_updates:
        canonical["amount"] = canonical_amount
    if "currency" in field_updates:
        canonical["currency"] = canonical_currency
    if "transaction_date" in field_updates:
        canonical["transaction_date"] = _validate_transaction_date(
            field_updates["transaction_date"]
        )
    if "merchant" in field_updates:
        canonical["merchant"] = _validate_trimmed_text(field_updates["merchant"], "merchant")
    if "description" in field_updates:
        canonical["description"] = _validate_trimmed_text(
            field_updates["description"], "description"
        )
    if "category" in field_updates:
        canonical["category"] = _validate_trimmed_text(field_updates["category"], "category")
    return canonical, canonical_amount, canonical_currency


def _canonical_monetary_pair(
    field_updates: dict[str, Any],
    effective_payload: dict[str, Any],
) -> tuple[str, str]:
    """Validate the *resulting* effective amount/currency pair, fail closed."""
    amount_value = (
        field_updates["amount"] if "amount" in field_updates else effective_payload.get("amount")
    )
    currency_value = (
        field_updates["currency"]
        if "currency" in field_updates
        else effective_payload.get("currency")
    )
    if amount_value is None:
        raise InvalidSupersessionFieldValueError(
            "No effective amount is available to form a valid monetary pair"
        )
    if currency_value is None:
        raise InvalidSupersessionFieldValueError(
            "No effective currency is available to form a valid monetary pair"
        )
    try:
        canonical_currency = (
            normalize_currency(currency_value) if isinstance(currency_value, str) else None
        )
        if canonical_currency is None:
            raise MoneyValidationError(
                f"currency must be a string, got {type(currency_value).__name__}"
            )
        canonical_amount = canonicalize_proposal_money(amount_value, canonical_currency)
    except MoneyValidationError as exc:
        raise InvalidSupersessionFieldValueError(
            f"Resulting monetary pair failed the Money Contract: {exc}"
        ) from exc
    return canonical_amount, canonical_currency


def _validate_transaction_date(value: Any) -> str:
    if not isinstance(value, str) or len(value) != _DATE_LEN:
        raise InvalidSupersessionFieldValueError(
            f"transaction_date must be ISO 8601 YYYY-MM-DD, got: {value!r}"
        )
    if value[4] != "-" or value[7] != "-":
        raise InvalidSupersessionFieldValueError(
            f"transaction_date must be ISO 8601 YYYY-MM-DD, got: {value!r}"
        )
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (ValueError, TypeError):
        raise InvalidSupersessionFieldValueError(
            f"transaction_date is not a valid calendar date: {value!r}"
        )
    return value


def _validate_trimmed_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidSupersessionFieldValueError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    trimmed = value.strip()
    if not trimmed:
        raise InvalidSupersessionFieldValueError(f"{field_name} must not be empty")
    return trimmed


def _material_field_updates(
    canonical_updates: dict[str, Any],
    effective_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return only the supplied fields whose canonical value materially differs.

    Echoed no-op values (an unchanged canonical amount, currency, or
    non-monetary field) are excluded: they must never be classified as newly
    human-corrected, have OCR confidence cleared, or gain fresh provenance.
    Amount materiality compares safe Decimal numeric values, never canonical
    strings, so a representation change caused by a different target
    currency's minor-unit scale (for example ``"12"`` versus ``"12.00"``) is
    not a human amount correction.  No FX conversion is ever performed.
    """
    material: dict[str, Any] = {}
    for field, new_value in canonical_updates.items():
        old_value = effective_payload.get(field)
        if field == "amount":
            old_decimal = _try_money_decimal(old_value)
            new_decimal = _try_money_decimal(new_value)
            if old_decimal is None or new_decimal is None or old_decimal != new_decimal:
                material[field] = new_value
        elif field == "currency":
            old_canonical = _try_normalize_currency(old_value)
            if old_canonical != new_value:
                material[field] = new_value
        elif field in ("merchant", "description", "category"):
            old_compare = old_value.strip() if isinstance(old_value, str) else old_value
            if old_compare != new_value:
                material[field] = new_value
        elif old_value != new_value:
            material[field] = new_value
    return material


def _try_money_decimal(value: Any) -> Decimal | None:
    try:
        return money_decimal(value)
    except MoneyValidationError:
        return None


def _try_normalize_currency(currency: Any) -> str | None:
    if not isinstance(currency, str):
        return None
    try:
        return normalize_currency(currency)
    except MoneyValidationError:
        return None


# ---------------------------------------------------------------------------
# Replacement payload and evidence
# ---------------------------------------------------------------------------


def _latest_completion_provenance(
    conn: sqlite3.Connection, parent: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Resolve, per field, which parent completion most recently supplied it.

    Reads the parent's append-only ``parser_proposal_completions`` versions in
    ascending order so later versions win field-by-field.  Returns a mapping
    of completed field name to the binding completion identity (public id,
    version number, authenticated actor, value, and content hashes).  The
    parent's completion records are never mutated.
    """
    cursor = conn.execute(
        """
        SELECT completion_public_id, version_number, field_updates_json,
               authenticated_actor_id, base_content_hash, completed_content_hash
        FROM parser_proposal_completions
        WHERE parser_output_id = ?
        ORDER BY version_number ASC
        """,
        (parent["id"],),
    )
    provenance: dict[str, dict[str, Any]] = {}
    for row in cursor.fetchall():
        if not isinstance(row, sqlite3.Row):
            row = dict(
                zip(
                    (
                        "completion_public_id",
                        "version_number",
                        "field_updates_json",
                        "authenticated_actor_id",
                        "base_content_hash",
                        "completed_content_hash",
                    ),
                    row,
                    strict=True,
                )
            )
        try:
            updates = json.loads(row["field_updates_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ReceiptSupersessionError(
                "Parent completion field updates are not valid JSON"
            ) from exc
        if not isinstance(updates, dict):
            raise ReceiptSupersessionError("Parent completion field updates must be an object")
        for field, value in updates.items():
            provenance[field] = {
                "completion_public_id": row["completion_public_id"],
                "completion_version": int(row["version_number"]),
                "authenticated_actor_id": row["authenticated_actor_id"],
                "value": value,
                "base_content_hash": row["base_content_hash"],
                "completed_content_hash": row["completed_content_hash"],
            }
    return provenance


def _has_correction_provenance(item: dict[str, Any]) -> bool:
    cid = item.get("correction_public_id")
    source_pid = item.get("superseded_proposal_public_id")
    return isinstance(cid, str) and bool(cid) and isinstance(source_pid, str) and bool(source_pid)


def _has_completion_provenance(item: dict[str, Any]) -> bool:
    cid = item.get("completion_public_id")
    version = item.get("completion_version")
    actor = item.get("authenticated_actor_id")
    return (
        isinstance(cid, str)
        and bool(cid)
        and isinstance(version, int)
        and not isinstance(version, bool)
        and isinstance(actor, str)
        and bool(actor)
    )


def _require_inherited_human_provenance(item: dict[str, Any]) -> None:
    """Fail closed unless inherited human evidence carries its own provenance."""
    if _has_correction_provenance(item) or _has_completion_provenance(item):
        return
    raise ReceiptSupersessionError(
        f"Inherited human evidence for field {item.get('field_name')!r} is missing "
        "its original correction or completion provenance"
    )


def _build_replacement_payload(
    effective_payload: dict[str, Any],
    applied_updates: dict[str, Any],
    parent: dict[str, Any],
    correction_public_id: str,
    completion_provenance: dict[str, dict[str, Any]],
    *,
    canonical_amount: str,
    canonical_currency: str,
) -> dict[str, Any]:
    corrected_fields = sorted(applied_updates.keys())
    completed_fields = {
        name: provenance
        for name, provenance in completion_provenance.items()
        if name not in applied_updates
    }
    payload = dict(effective_payload)
    payload.update(applied_updates)
    # The replacement's monetary pair is always represented at the target
    # currency's Money Contract canonical minor-unit scale.  When the amount
    # was not materially corrected this is a system normalization only: it
    # never joins applied updates or changed_fields, never clears the
    # amount's confidence, and never re-attributes the amount to the current
    # human correction.
    amount_normalized_only = "amount" not in applied_updates
    payload["amount"] = canonical_amount
    payload["currency"] = canonical_currency
    payload["status"] = PARSED_PENDING_CONFIRMATION
    payload["confirmation_required"] = True
    payload["is_final"] = False

    field_confidence = payload.get("field_confidence")
    if isinstance(field_confidence, dict):
        updated_confidence = dict(field_confidence)
        for name in (*corrected_fields, *sorted(completed_fields)):
            if name in updated_confidence:
                # Human-supplied values are not OCR parses; the OCR
                # confidence no longer describes them.
                updated_confidence[name] = None
        payload["field_confidence"] = updated_confidence

    inherited_evidence = payload.get("field_evidence")
    evidence: list[dict[str, Any]] = []
    historical_ocr: dict[str, list[dict[str, Any]]] = {}
    if isinstance(inherited_evidence, list):
        for item in inherited_evidence:
            if not isinstance(item, dict):
                evidence.append(item)
                continue
            field_name = item.get("field_name")
            if field_name in applied_updates:
                # Replaced below by the current correction's own evidence.
                continue
            if field_name in completed_fields:
                # Stale current-value evidence for a completed field is
                # replaced by explicit human-completion evidence below; the
                # original OCR material survives only as historical source
                # evidence attached to that completion item.
                if item.get("evidence_source_type") == _EVIDENCE_SOURCE_OCR:
                    historical_ocr.setdefault(str(field_name), []).append(dict(item))
                continue
            if item.get("evidence_source_type") == _EVIDENCE_SOURCE_HUMAN:
                # Inherited human evidence keeps its original provenance and
                # is never re-attributed to the current correction.
                _require_inherited_human_provenance(item)
            if field_name == "amount" and amount_normalized_only:
                # Keep the inherited item's provenance and confidence intact
                # while representing its value at the target currency's
                # canonical minor-unit scale, so payload evidence and
                # relational evidence agree exactly with the child amount.
                item = dict(item)
                item["proposed_value"] = canonical_amount
            evidence.append(item)

    for name in sorted(completed_fields):
        provenance = completed_fields[name]
        if payload.get(name) != provenance["value"]:
            raise ReceiptSupersessionError(
                f"Completion provenance for field {name!r} disagrees with the "
                "current effective payload"
            )
        completion_item: dict[str, Any] = {
            "field_name": name,
            "proposed_value": provenance["value"],
            "confidence": None,
            "evidence_source_type": _EVIDENCE_SOURCE_HUMAN,
            "completion_public_id": provenance["completion_public_id"],
            "completion_version": provenance["completion_version"],
            "authenticated_actor_id": provenance["authenticated_actor_id"],
            "source_proposal_public_id": parent["public_id"],
            "base_content_hash": provenance["base_content_hash"],
            "completed_content_hash": provenance["completed_content_hash"],
        }
        if name in historical_ocr:
            completion_item["historical_ocr_evidence"] = historical_ocr[name]
        evidence.append(completion_item)

    for name in corrected_fields:
        evidence.append(
            {
                "field_name": name,
                "proposed_value": applied_updates[name],
                "confidence": None,
                "evidence_source_type": _EVIDENCE_SOURCE_HUMAN,
                "correction_public_id": correction_public_id,
                "superseded_proposal_public_id": parent["public_id"],
            }
        )
    payload["field_evidence"] = evidence

    payload["correction"] = {
        "correction_public_id": correction_public_id,
        "corrected_fields": corrected_fields,
        "actor_type": _PERSISTED_ACTOR_TYPE,
        "superseded_proposal_public_id": parent["public_id"],
    }
    return payload


def _insert_replacement_proposal(
    conn: sqlite3.Connection,
    parent: dict[str, Any],
    replacement_public_id: str,
    child_payload_json: str,
) -> int:
    try:
        cursor = conn.execute(
            """
            INSERT INTO parser_outputs (
                public_id, source_type, source_public_id, statement_batch_id,
                attachment_id, parser_name, parser_version, raw_text,
                parsed_payload, normalized_payload, confidence_score,
                parse_status, parent_parser_output_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                replacement_public_id,
                parent["source_type"],
                parent["source_public_id"],
                parent["statement_batch_id"],
                parent["attachment_id"],
                parent["parser_name"],
                parent["parser_version"],
                parent["raw_text"],
                child_payload_json,
                child_payload_json,
                parent["confidence_score"],
                PARSED_PENDING_CONFIRMATION,
                parent["id"],
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise SupersessionConflictError(
            "The derived replacement proposal identity already exists"
        ) from exc
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        raise SupersessionPersistenceError("Replacement proposal insert returned no identity")
    return int(lastrowid)


def _insert_replacement_field_evidence(
    conn: sqlite3.Connection,
    replacement_id: int,
    child_payload: dict[str, Any],
) -> None:
    """Persist relational evidence rows that agree exactly with the payload.

    Every human item's reference carries the item's own correction or
    completion identity and source proposal identity — never the current
    command's identity for inherited evidence.  Missing or malformed human
    provenance fails closed before any row is written.
    """
    evidence = child_payload.get("field_evidence")
    if not isinstance(evidence, list):
        return
    rows: list[tuple[Any, ...]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source_type = item.get("evidence_source_type")
        if source_type == _EVIDENCE_SOURCE_HUMAN:
            if _has_correction_provenance(item):
                reference = _canonical_json(_correction_evidence_reference(item))
            elif _has_completion_provenance(item):
                reference = _canonical_json(
                    {
                        "completion_public_id": item["completion_public_id"],
                        "completion_version": item["completion_version"],
                        "authenticated_actor_id": item["authenticated_actor_id"],
                        "source_proposal_public_id": item.get("source_proposal_public_id"),
                        "completed_content_hash": item.get("completed_content_hash"),
                    }
                )
            else:
                raise ReceiptSupersessionError(
                    f"Human evidence for field {item.get('field_name')!r} is missing "
                    "its correction or completion provenance"
                )
            notes = None
        else:
            reference = _canonical_json(
                {
                    "extraction_public_id": item.get("extraction_public_id"),
                    "normalized_result_hash": item.get("normalized_result_hash"),
                    "block_sequence_indexes": item.get("block_sequence_indexes"),
                }
            )
            notes = item.get("excerpt")
        rows.append(
            (
                replacement_id,
                item.get("field_name"),
                _scalar_text(item.get("proposed_value")),
                item.get("confidence"),
                source_type,
                reference,
                notes,
            )
        )
    if rows:
        conn.executemany(
            """
            INSERT INTO parser_proposal_field_evidence (
                parser_output_id, field_name, proposed_value, confidence_score,
                evidence_source_type, evidence_reference, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _correction_evidence_reference(item: dict[str, Any]) -> dict[str, Any]:
    """Keep inherited D1 identities in the relational evidence mirror."""
    reference = {
        "correction_public_id": item["correction_public_id"],
        "superseded_proposal_public_id": item["superseded_proposal_public_id"],
    }
    for key in (
        "draft_content_hash",
        "draft_public_id",
        "draft_version",
        "human_reply_evidence_public_id",
        "operation_public_id",
    ):
        if key in item:
            reference[key] = item[key]
    return reference


def _insert_superseding_link(
    conn: sqlite3.Connection,
    *,
    link_public_id: str,
    extraction_id: int,
    parser_output_id: int,
    parser_contract_version: str,
    input_hash: str,
    result_hash: str,
    created_at: str,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO receipt_ocr_proposal_links (
                public_id, extraction_id, parser_output_id,
                proposal_input_hash, proposal_result_hash,
                parser_contract_version, link_role, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link_public_id,
                extraction_id,
                parser_output_id,
                input_hash,
                result_hash,
                parser_contract_version,
                LINK_ROLE_SUPERSEDING_CORRECTION,
                created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise SupersessionConflictError(
            "A conflicting OCR link already exists for this correction"
        ) from exc


def _insert_revision(
    conn: sqlite3.Connection,
    *,
    correction_public_id: str,
    superseded_parser_output_id: int,
    replacement_parser_output_id: int,
    superseded_content_hash: str,
    replacement_content_hash: str,
    superseded_from_status: str,
    field_updates: dict[str, Any],
    applied_field_updates: dict[str, Any],
    replacement_payload_json: str,
    actor: str,
    correction_channel: str,
    reason: str | None,
    created_at: str,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO receipt_proposal_revisions (
                correction_public_id, superseded_parser_output_id,
                replacement_parser_output_id, superseded_content_hash,
                replacement_content_hash, superseded_from_status,
                field_updates_json, applied_field_updates_json,
                replacement_payload_json, actor_type, authenticated_actor_id,
                correction_channel, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'human', ?, ?, ?, ?)
            """,
            (
                correction_public_id,
                superseded_parser_output_id,
                replacement_parser_output_id,
                superseded_content_hash,
                replacement_content_hash,
                superseded_from_status,
                json.dumps(field_updates, sort_keys=True),
                json.dumps(applied_field_updates, sort_keys=True),
                replacement_payload_json,
                actor,
                correction_channel,
                reason,
                created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise SupersessionConflictError(
            "A conflicting receipt proposal revision already exists"
        ) from exc


# ---------------------------------------------------------------------------
# Lifecycle events, raw intake pointer, audit
# ---------------------------------------------------------------------------


def _insert_parent_superseded_event(
    conn: sqlite3.Connection,
    *,
    parser_output_id: int,
    from_status: str,
    actor: str,
    correction_public_id: str,
    replacement_public_id: str,
    current_hash: str,
    replacement_hash: str,
    changed_fields: list[str],
    created_at: str,
) -> None:
    payload = json.dumps(
        {
            "correction_public_id": correction_public_id,
            "replacement_proposal_public_id": replacement_public_id,
            "previous_content_hash": current_hash,
            "replacement_content_hash": replacement_hash,
            "changed_fields": changed_fields,
            "actor_type": _PERSISTED_ACTOR_TYPE,
        },
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT INTO parser_proposal_events (
          parser_output_id, from_status, to_status, event_type, event_reason,
          actor_type, actor_identifier, event_payload, created_at
        ) VALUES (?, ?, ?, 'superseded', ?, 'user', ?, ?, ?)
        """,
        (
            parser_output_id,
            from_status,
            SUPERSEDED,
            f"superseding correction {correction_public_id}",
            actor,
            payload,
            created_at,
        ),
    )


def _insert_replacement_created_event(
    conn: sqlite3.Connection,
    *,
    replacement_id: int,
    actor: str,
    correction_public_id: str,
    parent_public_id: str,
    created_at: str,
) -> None:
    payload = json.dumps(
        {
            "correction_public_id": correction_public_id,
            "superseded_proposal_public_id": parent_public_id,
            "actor_type": _PERSISTED_ACTOR_TYPE,
        },
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT INTO parser_proposal_events (
          parser_output_id, from_status, to_status, event_type, event_reason,
          actor_type, actor_identifier, event_payload, created_at
        ) VALUES (?, NULL, ?, 'created', ?, 'user', ?, ?, ?)
        """,
        (
            replacement_id,
            PARSED_PENDING_CONFIRMATION,
            f"replacement for correction {correction_public_id}",
            actor,
            payload,
            created_at,
        ),
    )


def _repoint_raw_intake(conn: sqlite3.Connection, raw_intake_id: int, replacement_id: int) -> None:
    conn.execute(
        """
        UPDATE raw_intake_records
        SET parser_output_id = ?, status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (replacement_id, PARSED_PENDING_CONFIRMATION, raw_intake_id),
    )


def _append_supersession_audit(
    conn: sqlite3.Connection,
    *,
    parent: dict[str, Any],
    from_status: str,
    correction_public_id: str,
    replacement_public_id: str,
    current_hash: str,
    replacement_hash: str,
    actor: str,
    changed_fields: list[str],
    created_at: str,
) -> None:
    event_type = "receipt_proposal_superseded"
    aggregate_id = str(parent["public_id"])
    event_id = derive_audit_event_public_id(
        aggregate_type="parser_proposal",
        aggregate_public_id=aggregate_id,
        event_type=event_type,
        causation_public_id=correction_public_id,
    )
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="parser_proposal",
            aggregate_public_id=aggregate_id,
            event_type=event_type,
            event_payload={
                "correction_public_id": correction_public_id,
                "replacement_proposal_public_id": replacement_public_id,
                "previous_content_hash": current_hash,
                "replacement_content_hash": replacement_hash,
                "changed_fields": changed_fields,
            },
            previous_state={
                "parse_status": from_status,
                "raw_intake_status": raw_intake_status_for_proposal_status(from_status),
                "proposal_content_hash": current_hash,
                "conversion_status": "not_converted",
            },
            new_state={
                "parse_status": SUPERSEDED,
                "raw_intake_status": PARSED_PENDING_CONFIRMATION,
                "proposal_content_hash": current_hash,
                "replacement_proposal_public_id": replacement_public_id,
                "replacement_content_hash": replacement_hash,
                "conversion_status": "not_converted",
            },
            actor_type="human",
            actor_public_id=actor,
            authorization_public_id=correction_public_id,
            source_evidence_references=_parser_source_references(parent),
            correlation_public_id=aggregate_id,
            causation_public_id=correction_public_id,
            created_at=created_at,
        ),
    )


def _parser_source_references(proposal: dict[str, Any]) -> tuple[str, ...]:
    references = [f"parser-output:{proposal['public_id']}"]
    if proposal.get("source_public_id"):
        references.append(f"source:{proposal['source_public_id']}")
    if proposal.get("attachment_id") is not None:
        references.append(f"attachment-id:{proposal['attachment_id']}")
    if proposal.get("statement_batch_id") is not None:
        references.append(f"statement-batch-id:{proposal['statement_batch_id']}")
    return tuple(references)


# ---------------------------------------------------------------------------
# Idempotent replay / conflict handling
# ---------------------------------------------------------------------------


def get_receipt_proposal_revision_by_correction_id(
    conn: sqlite3.Connection, correction_public_id: str
) -> dict[str, Any] | None:
    """Read-only lookup of one persisted receipt revision by correction identity.

    Exposes the replay/conflict material (superseded/replacement ids and
    content hashes, canonical field updates, actor, channel) to callers
    outside this module without granting write access.
    """
    return _get_revision_by_correction_id(conn, correction_public_id)


def _get_revision_by_correction_id(
    conn: sqlite3.Connection, correction_public_id: str
) -> dict[str, Any] | None:
    cursor = conn.execute(
        """
        SELECT rev.id, rev.correction_public_id, rev.superseded_parser_output_id,
               rev.replacement_parser_output_id, rev.superseded_content_hash,
               rev.replacement_content_hash, rev.superseded_from_status,
               rev.field_updates_json, rev.applied_field_updates_json,
               rev.actor_type, rev.authenticated_actor_id, rev.correction_channel,
               rev.reason, rev.created_at,
               po.public_id AS replacement_proposal_public_id,
               ropl.public_id AS link_public_id
        FROM receipt_proposal_revisions AS rev
        JOIN parser_outputs AS po ON po.id = rev.replacement_parser_output_id
        LEFT JOIN receipt_ocr_proposal_links AS ropl
            ON ropl.parser_output_id = rev.replacement_parser_output_id
        WHERE rev.correction_public_id = ?
        """,
        (correction_public_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((col[0] for col in cursor.description), row, strict=True))


def _handle_existing_revision(
    conn: sqlite3.Connection,
    existing: dict[str, Any],
    *,
    parser_output_id: int,
    expected_content_hash: str,
    field_updates: dict[str, Any],
    actor: str,
    correction_channel: str,
    correction_public_id: str,
) -> dict[str, Any]:
    # Compare incoming canonical command material against the persisted
    # revision row; the persisted row is the authoritative record.  ``reason``
    # is non-authoritative metadata and excluded from the comparison.
    same_proposal = existing["superseded_parser_output_id"] == parser_output_id
    same_base_hash = existing["superseded_content_hash"] == expected_content_hash
    same_actor = existing["authenticated_actor_id"] == actor
    same_channel = existing["correction_channel"] == correction_channel

    same_updates = False
    if same_proposal:
        parent = _require_full_proposal(conn, parser_output_id)
        effective_payload = _resolve_effective(conn, parent)
        canonical_updates, _amount, _currency = _canonicalize_field_updates(
            field_updates, effective_payload
        )
        persisted_updates = json.loads(existing["field_updates_json"])
        same_updates = canonical_updates == persisted_updates

    if same_proposal and same_base_hash and same_actor and same_channel and same_updates:
        # The replay result is the original durable creation-time command
        # result: the parent's persisted original from_status, the constant
        # creation statuses, and the originally applied changed fields.  It
        # never reflects later confirmation, completion, or supersession of
        # the replacement.
        return {
            "correction_public_id": correction_public_id,
            "superseded_parser_output_id": parser_output_id,
            "replacement_parser_output_id": existing["replacement_parser_output_id"],
            "replacement_proposal_public_id": existing["replacement_proposal_public_id"],
            "superseded_content_hash": existing["superseded_content_hash"],
            "replacement_content_hash": existing["replacement_content_hash"],
            "link_public_id": existing["link_public_id"],
            "parent_from_status": existing["superseded_from_status"],
            "parent_to_status": SUPERSEDED,
            "replacement_parse_status": PARSED_PENDING_CONFIRMATION,
            "changed_fields": sorted(json.loads(existing["applied_field_updates_json"]).keys()),
            "actor_type": _PERSISTED_ACTOR_TYPE,
            "idempotent": True,
        }

    raise SupersessionConflictError(
        f"Correction {correction_public_id} already exists with different material. "
        f"same_proposal={same_proposal}, same_base_hash={same_base_hash}, "
        f"same_actor={same_actor}, same_channel={same_channel}, "
        f"same_updates={same_updates}"
    )


def _verify_existing_d1_revision_lineage(
    conn: sqlite3.Connection, existing: dict[str, Any]
) -> None:
    """Fail closed on replay when an inherited D1 chain no longer verifies."""
    current = _require_full_proposal(conn, int(existing["replacement_parser_output_id"]))
    seen = {int(current["id"])}
    while True:
        children = conn.execute(
            "SELECT * FROM parser_outputs WHERE parent_parser_output_id = ? ORDER BY id",
            (current["id"],),
        ).fetchall()
        if not children:
            break
        if len(children) != 1 or int(children[0]["id"]) in seen:
            raise ReceiptSupersessionError("Inherited D1 human revision lineage is invalid")
        current = dict(children[0])
        seen.add(int(current["id"]))

    from finance_core.parser_proposals.human_revision import (
        HumanRevisionLineageError,
        verify_human_revision_descendant,
    )

    current_hash = compute_effective_proposal_content_hash(conn, current)
    _payload, _completion_id, current_version = resolve_effective_payload(conn, current)
    try:
        verify_human_revision_descendant(
            conn,
            current,
            content_hash=current_hash,
            proposal_version=current_version,
        )
    except HumanRevisionLineageError as exc:
        raise ReceiptSupersessionError(
            "Inherited D1 human revision lineage does not verify"
        ) from exc


# ---------------------------------------------------------------------------
# Deterministic identity derivation and hashing
# ---------------------------------------------------------------------------


def _derive_replacement_public_id(correction_public_id: str) -> str:
    digest = _sha256_hex(f"replacement-proposal:{correction_public_id}")
    return f"po_rev_{digest[:32]}"


def _derive_link_public_id(correction_public_id: str) -> str:
    digest = _sha256_hex(f"superseding-link:{correction_public_id}")
    return f"ropl_rev_{digest[:32]}"


def _correction_input_hash(
    correction_public_id: str,
    parent: dict[str, Any],
    current_hash: str,
    canonical_updates: dict[str, Any],
    actor: str,
    correction_channel: str,
) -> str:
    material = {
        "correction_public_id": correction_public_id,
        "superseded_proposal_public_id": parent["public_id"],
        "superseded_content_hash": current_hash,
        "field_updates": canonical_updates,
        "authenticated_actor_id": actor,
        "correction_channel": correction_channel,
        "link_role": LINK_ROLE_SUPERSEDING_CORRECTION,
    }
    return _sha256_hex(_canonical_json(material))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scalar_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# Post-write verification and transaction helpers
# ---------------------------------------------------------------------------


def _verify_persisted(
    conn: sqlite3.Connection,
    *,
    replacement_id: int,
    replacement_public_id: str,
    child_payload_json: str,
    correction_public_id: str,
    raw_intake_id: int,
) -> None:
    child = conn.execute(
        "SELECT public_id, parse_status, parsed_payload FROM parser_outputs WHERE id = ?",
        (replacement_id,),
    ).fetchone()
    if (
        child is None
        or child[0] != replacement_public_id
        or child[1] != PARSED_PENDING_CONFIRMATION
        or child[2] != child_payload_json
    ):
        raise SupersessionPersistenceError(
            "Persisted replacement proposal does not match the correction command"
        )
    revision = conn.execute(
        "SELECT replacement_parser_output_id FROM receipt_proposal_revisions "
        "WHERE correction_public_id = ?",
        (correction_public_id,),
    ).fetchone()
    if revision is None or revision[0] != replacement_id:
        raise SupersessionPersistenceError("Persisted revision does not match the correction")
    pointer = conn.execute(
        "SELECT parser_output_id, status FROM raw_intake_records WHERE id = ?",
        (raw_intake_id,),
    ).fetchone()
    if pointer is None or pointer[0] != replacement_id or pointer[1] != PARSED_PENDING_CONFIRMATION:
        raise SupersessionPersistenceError(
            "Raw-intake current proposal pointer was not repointed to the replacement"
        )


def _acquire_write_transaction(conn: sqlite3.Connection) -> None:
    """Safely acquire the service-owned ``BEGIN IMMEDIATE`` unit of work.

    A connection with caller-owned pending work is rejected with a typed
    error without modifying or rolling back the caller's transaction.  Once
    the connection is known to be transaction-free, acquisition failures
    (busy/locked/any SQLite error) are translated to the stable supersession
    taxonomy with the original exception preserved as ``__cause__``, and no
    transaction or write is leaked.
    """
    if conn.in_transaction:
        # Caller-owned transaction: reject without touching its state.
        raise ReceiptSupersessionError(
            "Supersession unit of work requires a connection without pending work"
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        # No caller transaction existed, so any residual state is ours.
        _rollback_if_needed(conn)
        raise SupersessionPersistenceError(
            "Could not acquire the supersession write transaction"
        ) from exc


def _rollback_if_needed(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.rollback()


def _now(clock: Callable[[], str] | None) -> str:
    return clock() if clock is not None else datetime.now(timezone.utc).isoformat()


__all__ = [
    "InvalidCorrectionIdError",
    "InvalidSupersessionFieldValueError",
    "InvalidSupersessionStatusError",
    "NoMaterialSupersessionChangeError",
    "NonMonetarySupersessionError",
    "RawIntakeBindingError",
    "ReceiptRevisionMaterial",
    "ReceiptSupersessionError",
    "StaleSupersessionContentError",
    "StaleSupersessionTargetError",
    "SupersessionConflictError",
    "SupersessionPersistenceError",
    "UnauthorizedSupersessionActorError",
    "UnknownSupersessionFieldError",
    "UnsupportedSupersessionProposalError",
    "get_receipt_proposal_revision_by_correction_id",
    "supersede_receipt_total_proposal",
    "supersede_receipt_total_proposal_in_transaction",
]
