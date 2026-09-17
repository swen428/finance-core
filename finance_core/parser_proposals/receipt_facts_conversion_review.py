"""Read-only B4.3 conversion-review candidate listing and detail reporting.

SELECT-only staging review boundary for the guarded B4.1 receipt
proposal-to-facts conversion service.  A listed row means only:

    Persisted proposal-side state currently qualifies this proposal for
    human B4 conversion review.  Final conversion still requires a complete
    explicit command and full B4.1 service revalidation.

This module never claims a row is guaranteed to be convertible: the
caller-owned ``rpfc_`` command ID, payer, complete participant membership,
every participant's explicit ``is_included``, authenticated actor, channel,
and the human's explicit expected content hash only exist once the human
supplies the conversion command.  The candidate filter is a conservative
review prefilter over provable proposal-side facts; the B4.1 conversion
service (`convert_confirmed_receipt_proposal_to_facts`) remains the sole
final authority and revalidates everything at execution time.

Transaction contract: every function is SELECT-only.  Nothing here begins,
commits, or rolls back a write transaction, mutates a caller-owned
transaction, or repairs stale rows.  All functions work on read-only
connections and under ``PRAGMA query_only=ON``.  Connections must use the
``sqlite3.Row`` row factory (the ``connect_sqlite`` default).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    verify_ai_fallback_child,
)
from finance_core.parser_proposals.content_hash import (
    ProposalContentHashError,
    compute_effective_proposal_content_hash,
)
from finance_core.parser_proposals.conversion_state import (
    has_legacy_transaction_conversion,
    receipt_conversion_registry_exists,
)
from finance_core.parser_proposals.effective_payload import (
    EffectivePayloadError,
    resolve_effective_payload,
)
from finance_core.parser_proposals.lifecycle import CONFIRMED, SUPERSEDED
from finance_core.parser_proposals.receipt_facts_conversion import (
    _KNOWN_AMBIGUITY_FLAGS,
    _walk_supersession_chain,
)
from finance_core.staging_guard import StagingDatabaseError, require_staging_database

# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class ReceiptFactsConversionReviewError(ValueError):
    """Base error for the read-only conversion-review boundary."""


class ReviewStagingDatabaseRejectedError(ReceiptFactsConversionReviewError):
    """The staging guard rejected the database (live database, copies)."""


class InvalidReviewRequestError(ReceiptFactsConversionReviewError):
    """The review request itself is malformed (bad limit, blank ID)."""


class ReviewProposalNotFoundError(ReceiptFactsConversionReviewError):
    """The referenced proposal public ID does not exist."""


class UnsupportedReviewProposalTypeError(ReceiptFactsConversionReviewError):
    """The proposal is not a receipt OCR total proposal (no OCR link)."""


class InconsistentReviewStateError(ReceiptFactsConversionReviewError):
    """Persisted proposal-side state is contradictory; review fails closed."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CANDIDATE_LIMIT = 20
MAX_CANDIDATE_LIMIT = 100

CANDIDATE_REVIEW_LABEL = "candidate_for_conversion_review"
"""A listed row is a conversion-review candidate only, never a guarantee."""

HUMAN_REQUIRED_COMMAND_INPUTS: tuple[str, ...] = (
    "command_public_id (caller-owned, 'rpfc_' prefix; never generated here)",
    "expected_content_hash (the exact hash the human reviewed)",
    "payer_participant_public_id (never inferred, never defaulted)",
    "participants (explicit membership entries, each with is_included)",
    "authenticated_actor_id (human-only)",
    "channel",
)
"""Command inputs that do not exist proposal-side and must be supplied by
the human with the conversion command.  Listing or showing a proposal never
authorizes conversion."""

# Deterministic conservative SQL prefilter.  Every condition is a provable
# proposal-side fact; command-specific B4.1 guards (payer, membership,
# expected hash, actor, channel, command identity) cannot be evaluated here
# and are never pretended to be evaluated.  Ordering is deterministic by
# proposal public ID (schema UNIQUE) so it is stable across insertion order
# and reconnects.
_CANDIDATE_PREFILTER_SQL = """
SELECT po.*
FROM parser_outputs AS po
WHERE po.parse_status = ?
  AND (SELECT COUNT(*) FROM receipt_ocr_proposal_links AS l
       WHERE l.parser_output_id = po.id) = 1
  AND NOT EXISTS (SELECT 1 FROM parser_outputs AS child
                  WHERE child.parent_parser_output_id = po.id)
  AND (SELECT COUNT(*) FROM raw_intake_records AS r
       WHERE r.parser_output_id = po.id) = 1
  AND EXISTS (SELECT 1 FROM parser_proposal_authorizations AS a
              WHERE a.parser_output_id = po.id
                AND a.confirmation_state = 'confirmed'
                AND a.actor_type = 'human')
ORDER BY po.public_id ASC, po.id ASC
"""

# Effective-payload fields the B4.1 service requires from the proposal side
# (guard 14).  Presence only is checked here — no format validation, no
# Money Contract evaluation, no inference, no defaulting.  A confirmed
# proposal missing one of these cannot be converted by any command, so it
# is not a useful review candidate.
_REQUIRED_PROPOSAL_FIELDS = ("merchant", "transaction_date", "amount", "currency")


# ---------------------------------------------------------------------------
# Public result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversionReviewCandidate:
    """One conversion-review candidate row (never a convertibility claim)."""

    proposal_public_id: str
    parser_output_id: int
    merchant: Any
    transaction_date: Any
    amount: Any
    currency: Any
    effective_content_hash: str
    confirmation_public_id: str
    review_label: str = CANDIDATE_REVIEW_LABEL


@dataclass(frozen=True)
class ConversionReviewCandidateDetail:
    """Bounded deterministic review report for one receipt OCR proposal.

    Contains only the persisted proposal-side material needed to construct
    and verify a conversion command.  ``parser_output_id`` is diagnostic
    metadata only; the canonical reference is ``proposal_public_id``.
    Payer and participant membership are intentionally absent: they are
    human command inputs, never proposal-side facts.
    """

    proposal_public_id: str
    parser_output_id: int
    parse_status: str
    confirmation_public_id: str | None
    confirmation_state: str | None
    confirmation_actor_type: str | None
    current_effective_content_hash: str
    confirmation_bound_content_hash: str | None
    is_current_leaf: bool
    is_superseded: bool
    supersession_contributes: bool
    completion_contributes: bool
    completion_public_id: str | None
    completion_version: int
    merchant: Any
    transaction_date: Any
    amount: Any
    currency: Any
    ambiguity_flags: tuple[str, ...] | None
    ambiguity_flags_wellformed: bool
    extraction_public_id: str
    extraction_source_attachment_hash: str
    attachment_public_id: str
    attachment_content_hash: str | None
    raw_intake_public_id: str
    raw_intake_source_content_hash: str | None
    source_channel: str | None
    b4_conversion_registry_row_exists: bool
    legacy_transaction_conversion_exists: bool
    candidate_for_conversion_review: bool
    human_required_command_inputs: tuple[str, ...] = HUMAN_REQUIRED_COMMAND_INPUTS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_conversion_review_candidates(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[ConversionReviewCandidate]:
    """List proposals whose persisted state qualifies them for review.

    SELECT-only with deterministic ordering (proposal public ID ascending)
    and a bounded limit.  Rows whose persisted state is provably
    inconsistent or provably unconvertible are conservatively excluded;
    nothing is repaired, inferred, defaulted, or converted here.
    """
    _require_staging_readable(conn)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise InvalidReviewRequestError("limit must be a positive integer")
    if limit > MAX_CANDIDATE_LIMIT:
        raise InvalidReviewRequestError(f"limit must not exceed {MAX_CANDIDATE_LIMIT}")

    cursor = conn.execute(_CANDIDATE_PREFILTER_SQL, (CONFIRMED,))
    columns = [column[0] for column in cursor.description]
    candidates: list[ConversionReviewCandidate] = []
    for row in cursor.fetchall():
        proposal = _as_dict(columns, row)
        try:
            state = _review_state(conn, proposal)
        except ReceiptFactsConversionReviewError:
            # Conservative exclusion: contradictory or provably stale
            # persisted state is not a review candidate.  The row is left
            # untouched; the B4.1 service remains the final authority.
            continue
        if not state["candidate"]:
            continue
        candidates.append(
            ConversionReviewCandidate(
                proposal_public_id=str(proposal["public_id"]),
                parser_output_id=int(proposal["id"]),
                merchant=state["effective"].get("merchant"),
                transaction_date=state["effective"].get("transaction_date"),
                amount=state["effective"].get("amount"),
                currency=state["effective"].get("currency"),
                effective_content_hash=state["effective_hash"],
                confirmation_public_id=str(state["authorization"]["confirmation_public_id"]),
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def get_conversion_review_candidate_detail(
    conn: sqlite3.Connection,
    proposal_public_id: str,
) -> ConversionReviewCandidateDetail:
    """Return the bounded review report for one receipt OCR proposal.

    Fails closed on unknown IDs, non-receipt proposals, and contradictory
    persisted state.  Never dumps unbounded OCR text, never recalculates
    monetary values, and never infers payer or participant membership.
    """
    _require_staging_readable(conn)
    if not isinstance(proposal_public_id, str) or not proposal_public_id.strip():
        raise InvalidReviewRequestError("proposal_public_id must be a non-empty string")

    cursor = conn.execute(
        "SELECT * FROM parser_outputs WHERE public_id = ?",
        (proposal_public_id,),
    )
    columns = [column[0] for column in cursor.description]
    row = cursor.fetchone()
    if row is None:
        raise ReviewProposalNotFoundError(f"Parser proposal not found: {proposal_public_id!r}")
    proposal = _as_dict(columns, row)
    state = _review_state(conn, proposal)
    authorization = state["authorization"]
    effective = state["effective"]
    flags = state["flags"]
    return ConversionReviewCandidateDetail(
        proposal_public_id=str(proposal["public_id"]),
        parser_output_id=int(proposal["id"]),
        parse_status=str(proposal["parse_status"]),
        confirmation_public_id=(
            None if authorization is None else str(authorization["confirmation_public_id"])
        ),
        confirmation_state=(
            None if authorization is None else str(authorization["confirmation_state"])
        ),
        confirmation_actor_type=(
            None if authorization is None else str(authorization["actor_type"])
        ),
        current_effective_content_hash=state["effective_hash"],
        confirmation_bound_content_hash=(
            None if authorization is None else str(authorization["proposal_content_hash"])
        ),
        is_current_leaf=state["is_leaf"],
        is_superseded=str(proposal["parse_status"]) == SUPERSEDED,
        supersession_contributes=proposal.get("parent_parser_output_id") is not None,
        completion_contributes=state["completion_version"] > 0,
        completion_public_id=state["completion_public_id"],
        completion_version=state["completion_version"],
        merchant=effective.get("merchant"),
        transaction_date=effective.get("transaction_date"),
        amount=effective.get("amount"),
        currency=effective.get("currency"),
        ambiguity_flags=flags,
        ambiguity_flags_wellformed=state["flags_wellformed"],
        extraction_public_id=str(state["extraction"]["public_id"]),
        extraction_source_attachment_hash=str(state["extraction"]["source_attachment_hash"]),
        attachment_public_id=str(state["attachment"]["public_id"]),
        attachment_content_hash=(
            None
            if state["attachment"]["file_hash"] is None
            else str(state["attachment"]["file_hash"])
        ),
        raw_intake_public_id=str(state["raw_intake"]["public_id"]),
        raw_intake_source_content_hash=(
            None
            if state["raw_intake"]["source_content_hash"] is None
            else str(state["raw_intake"]["source_content_hash"])
        ),
        source_channel=(
            None
            if state["raw_intake"]["source_channel"] is None
            else str(state["raw_intake"]["source_channel"])
        ),
        b4_conversion_registry_row_exists=state["registry_exists"],
        legacy_transaction_conversion_exists=state["legacy_exists"],
        candidate_for_conversion_review=state["candidate"],
    )


# ---------------------------------------------------------------------------
# Internal read-only state collection
# ---------------------------------------------------------------------------


def _require_staging_readable(conn: sqlite3.Connection) -> None:
    """Reject the live database and copied/renamed identities, read-only."""
    try:
        require_staging_database(conn)
    except StagingDatabaseError as exc:
        raise ReviewStagingDatabaseRejectedError(str(exc)) from exc


def _as_dict(columns: list[str], row: sqlite3.Row | tuple) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return dict(zip(columns, row, strict=True))


def _review_state(conn: sqlite3.Connection, proposal: dict[str, Any]) -> dict[str, Any]:
    """Collect the provable read-only review state for one proposal.

    Raises typed review errors when the persisted state is contradictory.
    Performs no writes and evaluates no command-specific B4.1 guard.
    """
    parser_output_id = int(proposal["id"])

    link_cursor = conn.execute(
        "SELECT id, extraction_id, link_role FROM receipt_ocr_proposal_links "
        "WHERE parser_output_id = ? ORDER BY id",
        (parser_output_id,),
    )
    link_rows = link_cursor.fetchall()
    if len(link_rows) == 0:
        raise UnsupportedReviewProposalTypeError(
            "Only receipt OCR total proposals are reviewable for B4 conversion"
        )
    if len(link_rows) > 1:
        raise InconsistentReviewStateError(
            f"Proposal {parser_output_id} has {len(link_rows)} OCR links; "
            "contradictory lineage fails closed"
        )
    # sqlite3.Row and plain tuples both support positional access.
    extraction_id = link_rows[0][1]
    link_role = str(link_rows[0][2])

    extraction_cursor = conn.execute(
        "SELECT id, public_id, attachment_id, source_attachment_hash "
        "FROM receipt_ocr_extractions WHERE id = ?",
        (extraction_id,),
    )
    extraction_columns = [column[0] for column in extraction_cursor.description]
    extraction_row = extraction_cursor.fetchone()
    if extraction_row is None:
        raise InconsistentReviewStateError(
            "The proposal's OCR link references a missing extraction row"
        )
    extraction = _as_dict(extraction_columns, extraction_row)

    proposal_attachment_id = proposal.get("attachment_id")
    if proposal_attachment_id is None or int(extraction["attachment_id"]) != int(
        proposal_attachment_id
    ):
        raise InconsistentReviewStateError(
            "Proposal and OCR extraction attachment identities are missing or disagree"
        )
    attachment_cursor = conn.execute(
        "SELECT id, public_id, file_hash FROM attachments WHERE id = ?",
        (proposal_attachment_id,),
    )
    attachment_columns = [column[0] for column in attachment_cursor.description]
    attachment_row = attachment_cursor.fetchone()
    if attachment_row is None:
        raise InconsistentReviewStateError(
            "The proposal's attachment row is missing from the evidence chain"
        )
    attachment = _as_dict(attachment_columns, attachment_row)

    intake_cursor = conn.execute(
        "SELECT id, public_id, source_channel, source_content_hash "
        "FROM raw_intake_records WHERE parser_output_id = ? ORDER BY id",
        (parser_output_id,),
    )
    intake_columns = [column[0] for column in intake_cursor.description]
    intake_rows = intake_cursor.fetchall()
    if len(intake_rows) != 1:
        raise InconsistentReviewStateError(
            f"Proposal {parser_output_id} is bound to {len(intake_rows)} raw "
            "intake records; review requires exactly one"
        )
    raw_intake = _as_dict(intake_columns, intake_rows[0])

    auth_cursor = conn.execute(
        "SELECT confirmation_public_id, proposal_content_hash, actor_type, "
        "confirmation_state FROM parser_proposal_authorizations "
        "WHERE parser_output_id = ?",
        (parser_output_id,),
    )
    auth_columns = [column[0] for column in auth_cursor.description]
    auth_row = auth_cursor.fetchone()
    authorization = None if auth_row is None else _as_dict(auth_columns, auth_row)

    try:
        effective, completion_public_id, completion_version = resolve_effective_payload(
            conn, proposal
        )
    except EffectivePayloadError as exc:
        raise InconsistentReviewStateError(str(exc)) from exc
    try:
        effective_hash = compute_effective_proposal_content_hash(conn, proposal)
    except ProposalContentHashError as exc:
        raise InconsistentReviewStateError(str(exc)) from exc
    if link_role == "ai_fallback":
        try:
            _effective, _completion_public_id, proposal_version = resolve_effective_payload(
                conn, proposal
            )
            verified = verify_ai_fallback_child(
                conn,
                proposal,
                content_hash=effective_hash,
                proposal_version=proposal_version,
                require_resolved=False,
            )
        except (AiFallbackServiceError, EffectivePayloadError) as exc:
            raise InconsistentReviewStateError(
                "The AI fallback proposal lineage could not be verified"
            ) from exc
        if verified is None or verified.get("proposal_origin") != "ai_fallback":
            raise InconsistentReviewStateError(
                "An AI fallback OCR proposal requires a valid immutable AI lineage edge"
            )

    child = conn.execute(
        "SELECT 1 FROM parser_outputs WHERE parent_parser_output_id = ? LIMIT 1",
        (parser_output_id,),
    ).fetchone()
    is_leaf = child is None and str(proposal["parse_status"]) != SUPERSEDED

    chain_ids, _chain_root_id = _walk_supersession_chain(conn, parser_output_id)
    registry_exists = _chain_has_registry_conversion(conn, chain_ids)
    legacy_exists = any(
        has_legacy_transaction_conversion(conn, member_id) for member_id in chain_ids
    )

    raw_flags = effective.get("ambiguity_flags") if "ambiguity_flags" in effective else None
    flags_wellformed = isinstance(raw_flags, list) and all(
        isinstance(flag, str) and flag in _KNOWN_AMBIGUITY_FLAGS for flag in raw_flags
    )
    flags: tuple[str, ...] | None = None
    if flags_wellformed and isinstance(raw_flags, list):
        flags = tuple(str(flag) for flag in raw_flags)

    candidate = (
        str(proposal["parse_status"]) == CONFIRMED
        and is_leaf
        and authorization is not None
        and str(authorization["confirmation_state"]) == "confirmed"
        and str(authorization["actor_type"]) == "human"
        and str(authorization["proposal_content_hash"]) == effective_hash
        and not registry_exists
        and not legacy_exists
        and flags_wellformed
        # Guard 14 unsupported metadata is provably fatal proposal-side.
        and effective.get("description") is None
        and effective.get("category") is None
        # Presence-only completeness: a missing required field cannot be
        # supplied by any conversion command (monetary values are never
        # command inputs), so the row is provably not convertible now.
        and all(effective.get(name) is not None for name in _REQUIRED_PROPOSAL_FIELDS)
    )

    return {
        "authorization": authorization,
        "effective": effective,
        "effective_hash": effective_hash,
        "completion_public_id": completion_public_id,
        "completion_version": completion_version,
        "is_leaf": is_leaf,
        "extraction": extraction,
        "attachment": attachment,
        "raw_intake": raw_intake,
        "registry_exists": registry_exists,
        "legacy_exists": legacy_exists,
        "flags": flags,
        "flags_wellformed": flags_wellformed,
        "candidate": candidate,
    }


def _chain_has_registry_conversion(conn: sqlite3.Connection, chain_ids: list[int]) -> bool:
    """Whether any chain member already has a B4 conversion registry row."""
    if not receipt_conversion_registry_exists(conn):
        return False
    placeholders = ",".join("?" for _ in chain_ids)
    row = conn.execute(
        "SELECT 1 FROM receipt_proposal_conversions "
        f"WHERE parser_output_id IN ({placeholders}) "
        f"OR supersession_root_parser_output_id IN ({placeholders}) LIMIT 1",
        (*chain_ids, *chain_ids),
    ).fetchone()
    return row is not None


__all__ = [
    "CANDIDATE_REVIEW_LABEL",
    "DEFAULT_CANDIDATE_LIMIT",
    "HUMAN_REQUIRED_COMMAND_INPUTS",
    "MAX_CANDIDATE_LIMIT",
    "ConversionReviewCandidate",
    "ConversionReviewCandidateDetail",
    "InconsistentReviewStateError",
    "InvalidReviewRequestError",
    "ReceiptFactsConversionReviewError",
    "ReviewProposalNotFoundError",
    "ReviewStagingDatabaseRejectedError",
    "UnsupportedReviewProposalTypeError",
    "get_conversion_review_candidate_detail",
    "list_conversion_review_candidates",
]
