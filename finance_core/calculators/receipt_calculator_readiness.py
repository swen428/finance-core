"""Read-only receipt calculator-readiness reporting boundary (B4.2).

Reports whether one persisted B4.1 conversion-created receipt is ready for
the deterministic receipt split calculator.  Under approved Decision D2 of
``docs/design/receipt_proposal_to_facts_conversion_v1.md`` (Section 9,
Slice B4.2), a valid B4.1 total-level receipt without authoritative item
and allocation facts is reported as **not calculator-ready** with the
stable reason codes.  The IAF.5 extension (Section 15 of
``docs/design/receipt_item_allocation_facts_boundary_v1.md``, approved
IA-D10) adds the single positive path: a receipt is reported
calculator-ready **only** when exactly one active, human-authorized IAF
fact set exists and the complete Section 15 verification list passes at
this read — full service-depth persisted-state verification (registry
lineage, B4.1 provenance, every row/derived ID/canonical text/NUMERIC
mirror, all three hashes, audit binding) plus direct content
re-validation (item completeness, allocation membership, approved
vocabularies, Money Contract, exact IA-D6 reconciliation).  A fact set
that exists but fails any check is corruption and fails closed
(``ReceiptFactsIntegrityError``); it is never an ordinary ``not ready``.

This boundary is strictly SELECT-only.  It never begins a write
transaction, never commits or rolls back a caller-owned transaction, never
mutates lifecycle status, never appends audit events, never invokes the
conversion or the calculator, and never constructs calculator input.  No
readiness flag is persisted anywhere; readiness is computed per call from
the currently active fact-set version.

Trust rules (design Section 12.2, binding for B4.2):

* only a receipt durably bound through ``receipt_proposal_conversions`` is
  treated as conversion-created; a non-NULL canonical amount text alone
  establishes nothing;
* ``receipts.net_paid_amount_canonical_text`` is the authoritative
  monetary representation and is re-validated against the Money Contract
  before it is trusted;
* the legacy NUMERIC ``net_paid_amount`` mirror must decode Decimal-equal
  under the exact B4.1 mirror-decoding rule
  (``decimal_from_numeric_mirror``);
* registry-to-receipt identity and every persisted lineage field the
  report relies on are re-validated; corrupt, contradictory, or untrusted
  state fails closed and is never downgraded to an ordinary ``not ready``
  report.

Scope notes:

* On the no-fact-set path this boundary re-validates a defined subset of
  the Section 12.6/12.7 replay-verification contract (identity, lineage
  resolution, monetary representation, membership semantics,
  item/allocation state).  On the positive path it additionally runs the
  canonical IAF service-depth verifier over the active fact set, so a
  positive report is a full fact-set integrity attestation for the state
  visible at this read.
* The report is assembled from multiple SELECT statements without opening
  a transaction of its own.  Snapshot atomicity across those statements is
  the caller's responsibility: callers running concurrent writers should
  open their own read transaction first (this boundary preserves it), and
  a report taken inside an uncommitted caller-owned write transaction
  describes that connection's uncommitted, non-durable view.  As a
  backstop, the positive path re-reads the active fact-set identity after
  verification and fails closed if it changed mid-read.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from finance_core.calculators.receipt_calculator_input_mapping import (
    CalculatorInputMappingError,
    build_calculator_receipt,
    build_participant_list,
)
from finance_core.calculators.receipt_split_calculator import compute_exact_receipt_shares
from finance_core.money import (
    MoneyValidationError,
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.parser_proposals.receipt_facts_conversion import (
    CONVERSION_SCHEMA_VERSION,
    NEVER_WRITTEN_RECEIPT_COLUMNS,
    decimal_from_numeric_mirror,
    derive_receipt_public_id,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ADJUSTMENT_ALLOCATION_METHODS,
    ADJUSTMENT_DIRECTIONS,
    ADJUSTMENT_TYPES,
    ITEM_ALLOCATION_METHODS,
    ReceiptItemAllocationFactsError,
    verify_receipt_item_allocation_fact_set_for_review,
)
from finance_core.staging_guard import StagingDatabaseError, require_staging_database

# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class ReceiptCalculatorReadinessError(ValueError):
    """Base error for the calculator-readiness reporting boundary."""


class ReadinessStagingDatabaseRejectedError(ReceiptCalculatorReadinessError):
    """The staging guard rejected the database (live database, copies)."""


class ReceiptNotFoundError(ReceiptCalculatorReadinessError):
    """No receipt row exists for the requested receipt public ID."""


class UnsupportedReceiptProvenanceError(ReceiptCalculatorReadinessError):
    """The receipt is not durably bound through the B4.1 conversion registry.

    Legacy receipts, seed fixtures, and any row created outside the guarded
    conversion boundary are unsupported by B4.2 v1 — including rows whose
    ``net_paid_amount_canonical_text`` happens to be populated, because
    column shape alone never establishes trusted provenance.
    """


class ReceiptFactsIntegrityError(ReceiptCalculatorReadinessError):
    """Persisted conversion facts are corrupt, contradictory, or untrusted.

    This includes Money Contract violations of the canonical amount text,
    NUMERIC compatibility-mirror drift, registry-to-receipt identity or
    lineage drift, contradictory payer/membership facts, and unexpected
    item/allocation/adjustment rows attached to an immutable B4.1 receipt.
    These states fail closed; they are never reported as an ordinary
    ``not ready`` result.
    """


# ---------------------------------------------------------------------------
# Stable readiness reason codes (deterministic, ordered)
# ---------------------------------------------------------------------------

READINESS_SCHEMA_VERSION = "v1"
"""Version of the readiness report contract itself.

Distinct namespace from the conversion registry's ``schema_version``
column, which is owned by ``CONVERSION_SCHEMA_VERSION`` in the B4.1
module; the two must never be conflated.
"""

REASON_NO_AUTHORITATIVE_ITEM_FACTS = "no_authoritative_item_facts"
REASON_NO_AUTHORITATIVE_ALLOCATION_FACTS = "no_authoritative_allocation_facts"

# The canonical emission order for not-ready reasons.  Reports must be
# deterministic and independent of SQLite row order, so reasons are always
# emitted in this fixed order.
_REASON_ORDER = (
    REASON_NO_AUTHORITATIVE_ITEM_FACTS,
    REASON_NO_AUTHORITATIVE_ALLOCATION_FACTS,
)

# The reasons emitted for the valid B4.1 total-only shape (no fact set yet).
_NO_FACT_SET_REASONS = (
    REASON_NO_AUTHORITATIVE_ITEM_FACTS,
    REASON_NO_AUTHORITATIVE_ALLOCATION_FACTS,
)

_COMMAND_ID_RE = re.compile(r"^rpfc_[A-Za-z0-9_-]{1,195}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_QUANTITY_TEXT_RE = re.compile(r"^[1-9][0-9]{0,14}$")

_ROLE_PAYER = "payer"
_ROLE_PARTICIPANT = "participant"
_ROLE_EXCLUDED = "excluded"


# ---------------------------------------------------------------------------
# Public report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptCalculatorReadinessReport:
    """Immutable deterministic calculator-readiness report.

    Exact replay of the same read against the same persisted state — on the
    same connection or a fresh one — produces an equal report.

    The fields after ``not_ready_reasons`` are the additive IA-D10
    positive-readiness extension: they are populated only when
    ``is_calculator_ready`` is ``True`` and default to ``None`` on the
    backward-compatible not-ready report.
    """

    receipt_public_id: str
    conversion_command_public_id: str
    is_calculator_ready: bool
    not_ready_reasons: tuple[str, ...]
    active_fact_set_public_id: str | None = None
    active_fact_set_version: int | None = None
    fact_set_result_hash: str | None = None
    item_count: int | None = None
    allocation_count: int | None = None
    adjustment_count: int | None = None
    currency: str | None = None
    net_paid_amount_canonical_text: str | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def report_receipt_calculator_readiness(
    conn: sqlite3.Connection,
    receipt_public_id: str,
) -> ReceiptCalculatorReadinessReport:
    """Report calculator readiness for one conversion-created receipt.

    SELECT-only: performs no writes, opens no write transaction, and leaves
    any caller-owned transaction untouched.  Works with or without
    ``sqlite3.Row`` and under ``PRAGMA query_only = ON``.

    Raises the typed errors of this module: staging rejection, receipt not
    found, unsupported (non-B4.1) provenance, or persisted integrity /
    monetary-contract corruption.  Ordinary absence of item and allocation
    facts on a valid B4.1 total-level receipt is not an error; it is a
    ``not ready`` report with stable reason codes.  A persisted IAF fact
    set that fails any Section 15 verification is corruption, never an
    ordinary not-ready outcome.
    """
    try:
        require_staging_database(conn)
    except StagingDatabaseError as exc:
        raise ReadinessStagingDatabaseRejectedError(str(exc)) from exc

    if not isinstance(receipt_public_id, str) or not receipt_public_id.strip():
        raise ReceiptNotFoundError(
            f"Receipt public ID must be a non-empty string, got: {receipt_public_id!r}"
        )

    receipt = _fetch_receipt(conn, receipt_public_id)
    registry = _fetch_registry_binding(conn, receipt)
    _verify_registry_receipt_identity(receipt, registry)
    _verify_lineage_resolution(conn, registry)
    _verify_receipt_persisted_state(receipt)
    _verify_authoritative_monetary_representation(receipt)
    members = _verify_membership_facts(conn, receipt)

    fact_set_rows = _fetch_all(
        conn,
        "SELECT * FROM receipt_item_allocation_fact_sets WHERE receipt_id = ?",
        (receipt["id"],),
    )
    if not fact_set_rows:
        reasons = _collect_not_ready_reasons(conn, receipt)
        return ReceiptCalculatorReadinessReport(
            receipt_public_id=str(receipt["public_id"]),
            conversion_command_public_id=str(registry["command_public_id"]),
            is_calculator_ready=False,
            not_ready_reasons=reasons,
        )
    return _report_positive_readiness(conn, receipt, registry, fact_set_rows, members)


# ---------------------------------------------------------------------------
# Lookup (fail closed on absence and on impossible cardinality)
# ---------------------------------------------------------------------------


def _fetch_receipt(conn: sqlite3.Connection, receipt_public_id: str) -> dict[str, Any]:
    rows = _fetch_all(conn, "SELECT * FROM receipts WHERE public_id = ?", (receipt_public_id,))
    if not rows:
        raise ReceiptNotFoundError(f"Receipt not found: {receipt_public_id}")
    if len(rows) != 1:
        raise ReceiptFactsIntegrityError(
            f"Receipt public ID {receipt_public_id!r} matches {len(rows)} rows; "
            "receipt identity is not unique and cannot be trusted"
        )
    return rows[0]


def _fetch_registry_binding(conn: sqlite3.Connection, receipt: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_all(
        conn,
        "SELECT * FROM receipt_proposal_conversions WHERE receipt_id = ?",
        (receipt["id"],),
    )
    if not rows:
        raise UnsupportedReceiptProvenanceError(
            f"Receipt {receipt['public_id']!r} is not bound by a "
            "receipt_proposal_conversions registry row; only B4.1 "
            "conversion-created receipts are supported, and a populated "
            "canonical amount column alone does not establish provenance"
        )
    if len(rows) != 1:
        raise ReceiptFactsIntegrityError(
            f"Receipt {receipt['public_id']!r} is bound by {len(rows)} conversion "
            "registry rows; the one-conversion-per-receipt invariant is violated"
        )
    return rows[0]


# ---------------------------------------------------------------------------
# Registry-to-receipt identity and lineage revalidation
# ---------------------------------------------------------------------------


def _verify_registry_receipt_identity(receipt: dict[str, Any], registry: dict[str, Any]) -> None:
    """Never trust a registry row or receipt row in isolation."""
    command_public_id = registry["command_public_id"]
    if not isinstance(command_public_id, str) or not _COMMAND_ID_RE.match(command_public_id):
        raise ReceiptFactsIntegrityError(
            "Conversion registry command_public_id does not match the frozen "
            f"rpfc_ identity pattern: {command_public_id!r}"
        )
    if receipt["public_id"] != derive_receipt_public_id(command_public_id):
        raise ReceiptFactsIntegrityError(
            f"Receipt public ID {receipt['public_id']!r} does not match the "
            "deterministic derivation from the registry command "
            f"{command_public_id!r}; registry-to-receipt identity has drifted"
        )
    if registry["receipt_id"] != receipt["id"]:
        raise ReceiptFactsIntegrityError(
            "Conversion registry receipt_id does not match the receipt row"
        )
    if receipt["parser_output_id"] is None or (
        registry["parser_output_id"] != receipt["parser_output_id"]
    ):
        raise ReceiptFactsIntegrityError(
            "Conversion registry parser_output_id does not match the receipt's "
            "persisted parser output lineage"
        )
    if registry["schema_version"] != CONVERSION_SCHEMA_VERSION:
        raise ReceiptFactsIntegrityError(
            f"Unsupported conversion registry schema_version: {registry['schema_version']!r}"
        )
    if registry["actor_type"] != "human":
        raise ReceiptFactsIntegrityError(
            f"Conversion registry actor_type must be 'human', got: {registry['actor_type']!r}"
        )
    for field in ("authenticated_actor_id", "conversion_channel", "confirmation_public_id"):
        value = registry[field]
        if not isinstance(value, str) or not value.strip():
            raise ReceiptFactsIntegrityError(f"Conversion registry {field} is missing or empty")
    for field in ("proposal_content_hash", "command_material_hash", "conversion_result_hash"):
        value = registry[field]
        if not isinstance(value, str) or not _HEX64_RE.match(value):
            raise ReceiptFactsIntegrityError(
                f"Conversion registry {field} is not a lowercase 64-hex digest"
            )


def _verify_lineage_resolution(conn: sqlite3.Connection, registry: dict[str, Any]) -> None:
    """Resolve the registry's lineage pointers instead of trusting them.

    The bound parser output row must exist, and the bound confirmation
    record must exist, reference the same parser output, be human-made,
    and carry the registry's recorded proposal content hash.  The
    authorization's *current* lifecycle state is deliberately not
    re-checked here: B4.1 replay verification does not re-check it either,
    and conversion-time state validation is owned by the conversion guard.
    """
    proposal_rows = _fetch_all(
        conn, "SELECT id FROM parser_outputs WHERE id = ?", (registry["parser_output_id"],)
    )
    if len(proposal_rows) != 1:
        raise ReceiptFactsIntegrityError(
            "Conversion registry parser_output_id does not resolve to a "
            "parser_outputs row; the proposal lineage is broken"
        )
    confirmation_rows = _fetch_all(
        conn,
        "SELECT parser_output_id, actor_type, proposal_content_hash "
        "FROM parser_proposal_authorizations WHERE confirmation_public_id = ?",
        (registry["confirmation_public_id"],),
    )
    if len(confirmation_rows) != 1:
        raise ReceiptFactsIntegrityError(
            "Conversion registry confirmation_public_id does not resolve to a "
            "confirmation authorization row; the confirmation lineage is broken"
        )
    confirmation = confirmation_rows[0]
    if (
        confirmation["parser_output_id"] != registry["parser_output_id"]
        or confirmation["actor_type"] != "human"
        or confirmation["proposal_content_hash"] != registry["proposal_content_hash"]
    ):
        raise ReceiptFactsIntegrityError(
            "Conversion registry confirmation binding does not match the "
            "persisted confirmation authorization record"
        )


def _verify_receipt_persisted_state(receipt: dict[str, Any]) -> None:
    """The B4.1 facts contract: explicit status, never-written columns NULL."""
    if receipt["status"] != "confirmed":
        raise ReceiptFactsIntegrityError(
            "Conversion-created receipt status must be 'confirmed' (Decision "
            f"D4); got {receipt['status']!r} — no guarded lifecycle boundary "
            "for conversion-bound receipts exists yet, so drift fails closed"
        )
    drifted = sorted(name for name in NEVER_WRITTEN_RECEIPT_COLUMNS if receipt[name] is not None)
    if drifted:
        raise ReceiptFactsIntegrityError(
            f"Conversion-created receipt carries non-NULL values in {drifted}, "
            "which a B4.1 facts-only conversion never writes; the persisted "
            "state is untrusted"
        )


# ---------------------------------------------------------------------------
# Authoritative monetary representation (Section 12.2)
# ---------------------------------------------------------------------------


def _verify_authoritative_monetary_representation(receipt: dict[str, Any]) -> None:
    canonical_text = receipt["net_paid_amount_canonical_text"]
    if not isinstance(canonical_text, str):
        raise ReceiptFactsIntegrityError(
            "Registry-bound receipt is missing the authoritative canonical "
            f"monetary text, got: {canonical_text!r}"
        )
    currency = receipt["currency"]
    try:
        normalized_currency = normalize_currency(currency)
    except MoneyValidationError as exc:
        raise ReceiptFactsIntegrityError(
            f"Persisted receipt currency failed the Money Contract: {exc}"
        ) from exc
    if normalized_currency != currency:
        raise ReceiptFactsIntegrityError(
            f"Persisted receipt currency {currency!r} is not in canonical form"
        )
    try:
        amount = money_decimal(canonical_text, label="canonical amount text")
        validate_amount_for_currency(amount, currency, label="canonical amount text")
    except MoneyValidationError as exc:
        raise ReceiptFactsIntegrityError(
            f"Persisted canonical amount text failed the Money Contract: {exc}"
        ) from exc
    if amount <= Decimal(0):
        raise ReceiptFactsIntegrityError(
            f"Persisted canonical amount must be strictly positive, got: {canonical_text!r}"
        )
    if canonical_money_str(amount, currency) != canonical_text:
        raise ReceiptFactsIntegrityError(
            f"Persisted canonical amount text {canonical_text!r} is not the "
            f"byte-exact canonical minor-unit form for {currency}"
        )
    mirrored = decimal_from_numeric_mirror(receipt["net_paid_amount"])
    if mirrored is None or mirrored != amount:
        raise ReceiptFactsIntegrityError(
            "Legacy NUMERIC net_paid_amount mirror does not decode "
            "Decimal-equal to the authoritative canonical text under the "
            "B4.1 mirror-decoding contract; the persisted monetary state is lossy"
        )


# ---------------------------------------------------------------------------
# Payer / membership facts (Decision D5 semantics)
# ---------------------------------------------------------------------------


def _verify_membership_facts(
    conn: sqlite3.Connection, receipt: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Verify Decision D5 membership facts; return them keyed by public ID."""
    payer_participant_id = receipt["payer_participant_id"]
    if payer_participant_id is None:
        raise ReceiptFactsIntegrityError("Conversion-created receipt has no payer participant")
    payer_rows = _fetch_all(
        conn, "SELECT id FROM participants WHERE id = ?", (payer_participant_id,)
    )
    if len(payer_rows) != 1:
        raise ReceiptFactsIntegrityError(
            "Receipt payer_participant_id does not resolve to exactly one participants row"
        )

    raw_count_row = _fetch_all(
        conn,
        "SELECT COUNT(*) AS n FROM receipt_participants WHERE receipt_id = ?",
        (receipt["id"],),
    )
    raw_count = int(raw_count_row[0]["n"])
    members = _fetch_all(
        conn,
        "SELECT rp.participant_id AS participant_id, rp.role AS role, "
        "rp.is_included AS is_included, p.public_id AS participant_public_id "
        "FROM receipt_participants rp "
        "JOIN participants p ON p.id = rp.participant_id "
        "WHERE rp.receipt_id = ?",
        (receipt["id"],),
    )
    if raw_count != len(members):
        raise ReceiptFactsIntegrityError(
            "Receipt membership rows reference participants that do not exist"
        )
    if not members:
        raise ReceiptFactsIntegrityError(
            "Conversion-created receipt has no membership rows; B4.1 always "
            "persists at least the payer entry (Decision D5)"
        )
    # Deterministic processing independent of SQLite row order.
    members = sorted(members, key=lambda row: str(row["participant_public_id"]))
    seen_public_ids = {str(row["participant_public_id"]) for row in members}
    if len(seen_public_ids) != len(members):
        raise ReceiptFactsIntegrityError(
            "Receipt membership carries duplicate participant identities"
        )

    payer_membership_rows = [row for row in members if row["role"] == _ROLE_PAYER]
    if len(payer_membership_rows) != 1:
        raise ReceiptFactsIntegrityError(
            f"Receipt membership must contain exactly one payer row, found "
            f"{len(payer_membership_rows)}"
        )
    if payer_membership_rows[0]["participant_id"] != payer_participant_id:
        raise ReceiptFactsIntegrityError(
            "Receipt membership payer row does not match receipts.payer_participant_id"
        )

    for row in members:
        if row["is_included"] not in (0, 1):
            raise ReceiptFactsIntegrityError(
                "Receipt membership is_included must be an explicit 0 or 1, "
                f"got: {row['is_included']!r}"
            )
        role = row["role"]
        if role == _ROLE_PAYER:
            continue
        expected_role = _ROLE_PARTICIPANT if row["is_included"] == 1 else _ROLE_EXCLUDED
        if role != expected_role:
            raise ReceiptFactsIntegrityError(
                "Receipt membership role/inclusion facts are contradictory "
                f"under the Decision D5 mapping: role {role!r} with "
                f"is_included {row['is_included']!r}"
            )
    return {str(row["participant_public_id"]): row for row in members}


# ---------------------------------------------------------------------------
# Item / allocation state (Decision D2, fail-closed v1)
# ---------------------------------------------------------------------------


def _collect_not_ready_reasons(
    conn: sqlite3.Connection, receipt: dict[str, Any]
) -> tuple[str, ...]:
    item_count = _count(
        conn, "SELECT COUNT(*) AS n FROM receipt_items WHERE receipt_id = ?", receipt
    )
    allocation_count = _count(
        conn,
        "SELECT COUNT(*) AS n FROM receipt_item_allocations ria "
        "JOIN receipt_items ri ON ri.id = ria.receipt_item_id "
        "WHERE ri.receipt_id = ?",
        receipt,
    )
    adjustment_count = _count(
        conn, "SELECT COUNT(*) AS n FROM receipt_adjustments WHERE receipt_id = ?", receipt
    )
    if item_count or allocation_count or adjustment_count:
        # B4.1 contains no guarded item/allocation/adjustment creation or
        # correction boundary, so such rows on a conversion-bound receipt are
        # unauthorized ad-hoc state.  They are never sufficient authority for
        # readiness and never downgrade to an ordinary not-ready report.
        raise ReceiptFactsIntegrityError(
            f"Receipt {receipt['public_id']!r} carries unexpected persisted "
            f"state ({item_count} item, {allocation_count} allocation, "
            f"{adjustment_count} adjustment rows) that no guarded boundary "
            "created; refusing a speculative readiness result"
        )
    # The valid B4.1 total-only shape: zero authoritative item and
    # allocation facts.  Deterministic fixed-order reason codes.
    return _NO_FACT_SET_REASONS


# ---------------------------------------------------------------------------
# Positive readiness (IAF.5, design Section 15, approved IA-D10)
# ---------------------------------------------------------------------------


def _report_positive_readiness(
    conn: sqlite3.Connection,
    receipt: dict[str, Any],
    registry: dict[str, Any],
    fact_set_rows: list[dict[str, Any]],
    members: dict[str, dict[str, Any]],
) -> ReceiptCalculatorReadinessReport:
    """Verify the complete Section 15 list for the single active fact set.

    Every check is SELECT-only.  Any defect of an existing fact set is an
    integrity failure; this path never emits a new ordinary not-ready
    reason code (IA-D10).
    """
    active_rows = [row for row in fact_set_rows if row["superseded_by_fact_set_public_id"] is None]
    if len(active_rows) != 1:
        raise ReceiptFactsIntegrityError(
            f"Receipt {receipt['public_id']!r} has {len(fact_set_rows)} fact-set "
            f"registry rows but {len(active_rows)} active versions; exactly one "
            "active fact set is required and the persisted state is untrusted"
        )
    active = active_rows[0]

    if str(active["conversion_command_public_id"]) != str(registry["command_public_id"]) or str(
        active["expected_conversion_result_hash"]
    ) != str(registry["conversion_result_hash"]):
        raise ReceiptFactsIntegrityError(
            "The active fact set is not bound to the receipt's B4.1 conversion "
            "registry row (conversion command ID or result hash mismatch)"
        )
    if str(active["actor_type"]) != "human":
        raise ReceiptFactsIntegrityError(
            f"The active fact set must carry actor_type='human'; got: {active['actor_type']!r}"
        )

    fact_set_public_id = str(active["fact_set_public_id"])
    try:
        verify_receipt_item_allocation_fact_set_for_review(conn, fact_set_public_id)
    except ReceiptItemAllocationFactsError as exc:
        raise ReceiptFactsIntegrityError(
            f"The active fact set {fact_set_public_id!r} failed full "
            "service-depth persisted-state verification; readiness fails closed"
        ) from exc

    try:
        payload = json.loads(str(active["canonical_fact_set_payload"]))
        counts = _verify_fact_set_content(payload, receipt, members)
    except ReceiptFactsIntegrityError:
        raise
    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        RecursionError,
        MemoryError,
    ) as exc:
        raise ReceiptFactsIntegrityError(
            f"The active fact set {fact_set_public_id!r} carries a canonical "
            "payload that cannot be content-verified; readiness fails closed"
        ) from exc

    _verify_active_binding_stable(conn, receipt, active, expected_total=len(fact_set_rows))

    # Positive readiness must guarantee the deterministic calculator accepts the
    # projection of this fact set.  A fact set whose exact shares cannot be
    # reconciled -- for example a subtract adjustment that drives a
    # participant's exact share below zero -- is an integrity failure (IA-D10):
    # existing fact-set defects are never an ordinary not-ready outcome.
    _calculator_preflight(receipt, payload, members, fact_set_public_id)

    return ReceiptCalculatorReadinessReport(
        receipt_public_id=str(receipt["public_id"]),
        conversion_command_public_id=str(registry["command_public_id"]),
        is_calculator_ready=True,
        not_ready_reasons=(),
        active_fact_set_public_id=fact_set_public_id,
        active_fact_set_version=int(active["version"]),
        fact_set_result_hash=str(active["fact_set_result_hash"]),
        item_count=counts["item_count"],
        allocation_count=counts["allocation_count"],
        adjustment_count=counts["adjustment_count"],
        currency=str(receipt["currency"]),
        net_paid_amount_canonical_text=str(receipt["net_paid_amount_canonical_text"]),
    )


def _calculator_preflight(
    receipt: dict[str, Any],
    payload: Any,
    members: dict[str, dict[str, Any]],
    fact_set_public_id: str,
) -> None:
    """Fail closed if the deterministic calculator would reject this fact set.

    Reuses the single shared implementations -- the fact-set to calculator-input
    mapping and :func:`compute_exact_receipt_shares` -- so readiness enforces
    exactly the rules the deterministic calculator enforces, without running the
    calculator itself, without touching the database, and without a second money
    implementation.

    An unmappable payload is not reported here: that is the projection
    boundary's own typed fail-closed integrity error, and reporting it as an
    ordinary not-ready reason would change that established contract.

    A fact set that fails the calculator's deterministic preconditions (for
    example a subtract adjustment that drives a participant's exact share below
    zero) is an integrity failure (IA-D10), never an ordinary not-ready outcome:
    the fact set was persisted through the guarded IAF boundary, so its defect
    is corruption of the persisted state.
    """
    included = {
        public_id
        for public_id, row in members.items()
        if row.get("is_included") is not None and int(row["is_included"]) == 1
    }
    payer_public_ids = [
        public_id for public_id, row in members.items() if str(row.get("role")) == _ROLE_PAYER
    ]
    if len(payer_public_ids) != 1:
        raise ReceiptFactsIntegrityError(
            "Receipt membership does not resolve to exactly one payer public ID"
        )
    payer_public_id = payer_public_ids[0]

    try:
        participants = build_participant_list(included, payer_public_id)
        calculator_receipt = build_calculator_receipt(
            receipt_public_id=str(receipt["public_id"]),
            merchant=str(receipt["merchant"]),
            currency=str(receipt["currency"]),
            payer_public_id=payer_public_id,
            net_paid_text=str(receipt["net_paid_amount_canonical_text"]),
            payload=payload,
            included=included,
        )
    except CalculatorInputMappingError as exc:
        raise ReceiptFactsIntegrityError(
            f"The active fact set {fact_set_public_id!r} cannot be mapped onto the "
            "deterministic calculator input; the persisted state is corrupt (IA-D10)"
        ) from exc

    try:
        compute_exact_receipt_shares(calculator_receipt, participants, str(receipt["currency"]))
    except (ValueError, ArithmeticError, KeyError, TypeError) as exc:
        raise ReceiptFactsIntegrityError(
            f"The active fact set {fact_set_public_id!r} fails a deterministic "
            "calculator precondition; the persisted state is corrupt (IA-D10)"
        ) from exc


def _verify_fact_set_content(
    payload: Any,
    receipt: dict[str, Any],
    members: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Directly re-validate the Section 15 content rules on the payload.

    The canonical payload was already proven byte-exact against the
    persisted rows, all three hashes, and the audit binding by the
    service-depth verifier, so content validated here is validated for
    the persisted facts.  Readiness re-runs the arithmetic and membership
    rules itself (IA-D10) instead of trusting write-time validation.
    """
    currency = str(receipt["currency"])
    net_paid_text = str(receipt["net_paid_amount_canonical_text"])
    if payload["currency"] != currency or payload["net_paid_amount"] != net_paid_text:
        raise ReceiptFactsIntegrityError(
            "The fact-set payload contradicts the receipt's authoritative "
            "currency or canonical net-paid amount"
        )
    included = {public_id for public_id, row in members.items() if int(row["is_included"]) == 1}

    items = payload["items"]
    if not items:
        raise ReceiptFactsIntegrityError(
            "A complete fact set requires at least one item fact; an empty "
            "persisted fact set is corruption, not an ordinary not-ready state"
        )
    line_amounts: dict[int, Decimal] = {}
    for item in items:
        line = item["line_number"]
        if isinstance(line, bool) or not isinstance(line, int):
            raise ReceiptFactsIntegrityError("Item line_number must be an integer")
        if line in line_amounts:
            raise ReceiptFactsIntegrityError(f"Duplicate item line number {line}")
        name = item["item_name"]
        if not isinstance(name, str) or not name.strip():
            raise ReceiptFactsIntegrityError(f"Item {line} has an empty item_name")
        amount = _require_canonical_positive_amount(
            item["line_amount"], currency, f"item {line} line_amount"
        )
        quantity = item["quantity"]
        unit_price = item["unit_price"]
        if quantity is not None and (
            not isinstance(quantity, str) or not _QUANTITY_TEXT_RE.match(quantity)
        ):
            raise ReceiptFactsIntegrityError(
                f"Item {line} quantity is not a canonical positive integer text"
            )
        if unit_price is not None:
            unit_amount = _require_canonical_positive_amount(
                unit_price, currency, f"item {line} unit_price"
            )
            if quantity is not None and Decimal(quantity) * unit_amount != amount:
                raise ReceiptFactsIntegrityError(
                    f"Item {line} fails quantity × unit_price = line_amount "
                    "exact reconciliation (IA-D6)"
                )
        line_amounts[line] = amount
    if sorted(line_amounts) != list(range(1, len(items) + 1)):
        raise ReceiptFactsIntegrityError(
            "Item line numbers must be unique and contiguous from 1..N"
        )

    allocation_count = 0
    allocated_lines: set[int] = set()
    for entry in payload["allocations"]:
        line = int(entry["line_number"])
        if line not in line_amounts:
            raise ReceiptFactsIntegrityError(
                f"Allocation references unknown item line number {line}"
            )
        if line in allocated_lines:
            raise ReceiptFactsIntegrityError(
                f"Item line {line} carries more than one allocation entry"
            )
        allocated_lines.add(line)
        method = entry["allocation_method"]
        if method not in ITEM_ALLOCATION_METHODS:
            raise ReceiptFactsIntegrityError(
                f"Unsupported item allocation method {method!r} (IA-D7)"
            )
        participants = entry["participants"]
        if not participants:
            raise ReceiptFactsIntegrityError(
                f"Item line {line} has an allocation entry with zero participants"
            )
        seen: set[str] = set()
        manual_total = Decimal(0)
        for participant in participants:
            public_id = str(participant["participant_public_id"])
            if public_id in seen:
                raise ReceiptFactsIntegrityError(
                    f"Item line {line} allocates twice to participant {public_id!r}"
                )
            seen.add(public_id)
            if public_id not in included:
                raise ReceiptFactsIntegrityError(
                    f"Item line {line} allocates to {public_id!r}, which is not "
                    "an included receipt member (Decision D5)"
                )
            share = participant["share_amount"]
            if method == "equal_amount":
                if share is not None:
                    raise ReceiptFactsIntegrityError(
                        f"Item line {line}: equal_amount allocations must not "
                        "carry a persisted per-participant amount"
                    )
            else:
                manual_total += _require_canonical_positive_amount(
                    share, currency, f"item {line} share for {public_id!r}"
                )
        if method == "manual" and manual_total != line_amounts[line]:
            raise ReceiptFactsIntegrityError(
                f"Item line {line}: manual shares do not sum exactly to the line amount (IA-D6)"
            )
        allocation_count += len(participants)
    if allocated_lines != set(line_amounts):
        raise ReceiptFactsIntegrityError(
            "Every item fact must carry at least one allocation; unallocated "
            "items make the fact set incomplete and untrusted"
        )

    add_total = Decimal(0)
    subtract_total = Decimal(0)
    adjustment_indexes: list[int] = []
    for entry in payload["adjustments"]:
        index = int(entry["adjustment_index"])
        adjustment_indexes.append(index)
        if entry["adjustment_type"] not in ADJUSTMENT_TYPES:
            raise ReceiptFactsIntegrityError(
                f"Unsupported adjustment type {entry['adjustment_type']!r} (IA-D7b)"
            )
        direction = entry["direction"]
        if direction not in ADJUSTMENT_DIRECTIONS:
            raise ReceiptFactsIntegrityError(
                f"Unsupported adjustment direction {direction!r} (IA-D7b)"
            )
        method = entry["allocation_method"]
        if method not in ADJUSTMENT_ALLOCATION_METHODS:
            raise ReceiptFactsIntegrityError(
                f"Unsupported adjustment allocation method {method!r} (IA-D7b)"
            )
        amount = _require_canonical_positive_amount(
            entry["amount"], currency, f"adjustment {index} amount"
        )
        participants = entry["participants"]
        if method == "manual":
            if not participants:
                raise ReceiptFactsIntegrityError(
                    f"Adjustment {index}: manual adjustments require explicit "
                    "per-participant shares"
                )
            seen = set()
            manual_total = Decimal(0)
            for participant in participants:
                public_id = str(participant["participant_public_id"])
                if public_id in seen:
                    raise ReceiptFactsIntegrityError(
                        f"Adjustment {index} allocates twice to {public_id!r}"
                    )
                seen.add(public_id)
                if public_id not in included:
                    raise ReceiptFactsIntegrityError(
                        f"Adjustment {index} allocates to {public_id!r}, which "
                        "is not an included receipt member (Decision D5)"
                    )
                manual_total += _require_canonical_positive_amount(
                    participant["share_amount"],
                    currency,
                    f"adjustment {index} share for {public_id!r}",
                )
            if manual_total != amount:
                raise ReceiptFactsIntegrityError(
                    f"Adjustment {index}: manual shares do not sum exactly to "
                    "the adjustment amount (IA-D6)"
                )
        elif participants is not None:
            raise ReceiptFactsIntegrityError(
                f"Adjustment {index}: per-participant shares are only "
                "meaningful for the manual method"
            )
        if direction == "add":
            add_total += amount
        else:
            subtract_total += amount
    if sorted(adjustment_indexes) != list(range(1, len(adjustment_indexes) + 1)):
        raise ReceiptFactsIntegrityError(
            "Adjustment indexes must be unique and contiguous from 1..N"
        )

    items_total = sum(line_amounts.values(), Decimal(0))
    net_paid = money_decimal(net_paid_text, label="authoritative net paid")
    if items_total + add_total - subtract_total != net_paid:
        raise ReceiptFactsIntegrityError(
            f"Fact-set reconciliation failed (IA-D6): items {items_total} + "
            f"add {add_total} - subtract {subtract_total} does not equal the "
            f"authoritative net paid {net_paid_text}"
        )

    return {
        "item_count": len(items),
        "allocation_count": allocation_count,
        "adjustment_count": len(adjustment_indexes),
    }


def _require_canonical_positive_amount(value: Any, currency: str, label: str) -> Decimal:
    """Money Contract re-validation of one canonical monetary text."""
    if not isinstance(value, str):
        raise ReceiptFactsIntegrityError(
            f"{label} must be a canonical monetary string, got: {value!r}"
        )
    try:
        amount = money_decimal(value, label=label)
        validate_amount_for_currency(amount, currency, label=label)
    except MoneyValidationError as exc:
        raise ReceiptFactsIntegrityError(f"{label} failed the Money Contract: {exc}") from exc
    if amount <= Decimal(0):
        raise ReceiptFactsIntegrityError(f"{label} must be strictly positive, got: {value!r}")
    if canonical_money_str(amount, currency) != value:
        raise ReceiptFactsIntegrityError(
            f"{label} {value!r} is not the byte-exact canonical minor-unit form for {currency}"
        )
    return amount


def _verify_active_binding_stable(
    conn: sqlite3.Connection,
    receipt: dict[str, Any],
    active: dict[str, Any],
    *,
    expected_total: int,
) -> None:
    """Mid-read supersession backstop (IAF.4 review precedent).

    The report is assembled from multiple SELECTs without an owned
    transaction, so re-read the active identity last and fail closed if a
    concurrent supersession moved it while readiness was verifying.
    """
    final_rows = _fetch_all(
        conn,
        "SELECT fact_set_public_id, version, fact_set_result_hash "
        "FROM receipt_item_allocation_fact_sets "
        "WHERE receipt_id = ? AND superseded_by_fact_set_public_id IS NULL",
        (receipt["id"],),
    )
    final_total = int(
        _fetch_all(
            conn,
            "SELECT COUNT(*) AS n FROM receipt_item_allocation_fact_sets WHERE receipt_id = ?",
            (receipt["id"],),
        )[0]["n"]
    )
    if (
        final_total != expected_total
        or len(final_rows) != 1
        or str(final_rows[0]["fact_set_public_id"]) != str(active["fact_set_public_id"])
        or int(final_rows[0]["version"]) != int(active["version"])
        or str(final_rows[0]["fact_set_result_hash"]) != str(active["fact_set_result_hash"])
    ):
        raise ReceiptFactsIntegrityError(
            "The active fact set changed during the SELECT-only readiness "
            "read; retry from a fresh snapshot"
        )


# ---------------------------------------------------------------------------
# Row helpers (support connections with or without sqlite3.Row)
# ---------------------------------------------------------------------------


def _count(conn: sqlite3.Connection, sql: str, receipt: dict[str, Any]) -> int:
    return int(_fetch_all(conn, sql, (receipt["id"],))[0]["n"])


def _fetch_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    if cursor.description is None:
        raise ReceiptFactsIntegrityError(
            "SQLite cursor did not expose column metadata for a readiness query"
        )
    columns = [column[0] for column in cursor.description]
    return [_row_to_dict(row, columns) for row in cursor.fetchall()]


def _row_to_dict(row: Any, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, Mapping):
        try:
            return {column: row[column] for column in columns}
        except KeyError as exc:
            raise ReceiptCalculatorReadinessError(
                "Mapping row factory did not supply every selected column "
                f"({exc.args[0]!r}); readiness cannot trust a partial row"
            ) from exc
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        return dict(zip(columns, row, strict=True))
    raise ReceiptCalculatorReadinessError(
        "Unsupported sqlite3 row_factory result type "
        f"{type(row).__name__!r}; use sqlite3.Row, a mapping keyed by column "
        "name, or the default tuple rows"
    )


__all__ = [
    "READINESS_SCHEMA_VERSION",
    "REASON_NO_AUTHORITATIVE_ITEM_FACTS",
    "REASON_NO_AUTHORITATIVE_ALLOCATION_FACTS",
    "ReceiptCalculatorReadinessError",
    "ReadinessStagingDatabaseRejectedError",
    "ReceiptNotFoundError",
    "UnsupportedReceiptProvenanceError",
    "ReceiptFactsIntegrityError",
    "ReceiptCalculatorReadinessReport",
    "report_receipt_calculator_readiness",
]
