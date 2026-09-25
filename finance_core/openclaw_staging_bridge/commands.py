"""Command handlers for the OpenClaw staging bridge v1 JSON CLI.

Every handler opens the staging database through the existing guard, calls
only existing public Finance service boundaries, and fails closed with
stable codes.  The bridge adds no financial logic: capture persists intake,
propose reuses deterministic parser/OCR proposal boundaries, get_review is a
bounded read, and confirm/edit/reject delegate to the authoritative
confirmation, completion, and supersession boundaries, and the S4 commands
(finalize, authorize_finalization, apply_fact_set) delegate to the existing
guarded conversion, IAF fact-set, calculation, authorization, and
finalization boundaries while carrying no monetary material in envelopes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import unicodedata
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from finance_core.application import review as application_review
from finance_core.application.capture_processing import (
    CaptureProcessingConflictError,
    process_claimed_capture_job,
)
from finance_core.calculators.receipt_calculator_readiness import (
    ReceiptFactsIntegrityError,
    ReceiptNotFoundError,
    report_receipt_calculator_readiness,
)
from finance_core.intake.attachment_evidence import (
    InvalidAttachmentIdentityError,
    get_telegram_source_evidence_for_raw_intake,
    validate_attachment_identity_metadata,
)
from finance_core.intake.capture_jobs import (
    CaptureJobConflictError,
    CaptureJobNotRunnableError,
    CaptureLeaseLostError,
    DurableCaptureConnectionError,
    claim_capture_job,
    ensure_capture_job,
    ensure_replayed_text_capture_job,
    get_capture_job,
    require_durable_capture_connection,
)
from finance_core.intake.raw_text_repository import (
    TELEGRAM_TEXT,
    RawIntakeIdempotencyConflictError,
    create_raw_intake_record,
    get_raw_intake_record_by_idempotency_key,
    get_raw_intake_record_by_public_id,
)
from finance_core.intake.receipt_ocr_evidence import (
    ReceiptOcrError,
    extract_and_persist_receipt_ocr_evidence,
    verify_telegram_original_attachment,
)
from finance_core.intake.receipt_ocr_proposal import (
    ReceiptOcrProposalError,
    ingest_receipt_ocr_evidence_as_total_expense_proposal,
)
from finance_core.intake.telegram_text_adapter import (
    TelegramTextUpdateValidationError,
    process_openclaw_telegram_text_message,
    process_telegram_text_update,
    validate_openclaw_telegram_text_message,
    validate_telegram_text_update,
)
from finance_core.money import (
    MoneyValidationError,
    canonical_money_str,
    money_decimal,
    validate_amount_for_currency,
)
from finance_core.openclaw_staging_bridge import (
    callback_tokens,
    errors,
    guided_edit,
    human_actions,
    identity,
    telegram_boundary,
    workspace_access,
)
from finance_core.openclaw_staging_bridge.envelope import BridgeRequest
from finance_core.openclaw_staging_bridge.ocr_boundary import build_ocr_engine
from finance_core.openclaw_staging_bridge.receipt_handoff import (
    publish_receipt_handoff,
    read_handoff_descriptor,
    read_handoff_file,
    validate_receipt_handoff_metadata,
)
from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    claim_ai_fallback_invocation,
    claim_ai_fallback_invocation_v2,
    get_ai_processing_status_v2,
    prepare_ai_fallback,
    prepare_ai_fallback_v2,
    record_ai_fallback_result_v2,
    record_ai_fallback_result_with_disposition,
    requires_deterministic_intent_policy,
    verify_ai_fallback_child,
    verify_deterministic_intent_policy,
)
from finance_core.parser_proposals.ai_model_compatibility import (
    ModelCompatibilityError,
    canonical_projection_hash,
    register_ai_model_compatibility_receipt_v2,
    validate_config_projection,
    verify_harness_outcome,
)
from finance_core.parser_proposals.completion import (
    InvalidCompletionFieldValueError,
    InvalidCompletionStatusError,
    NoMaterialChangeError,
    ProposalCompletionError,
    StaleProposalContentError,
    UnknownCompletionFieldError,
    complete_proposal,
    get_completion_by_public_id,
)
from finance_core.parser_proposals.confirmation import confirm_proposal, reject_proposal
from finance_core.parser_proposals.conversion_state import (
    has_legacy_transaction_conversion,
    has_receipt_ocr_proposal_link,
    has_receipt_registry_conversion,
)
from finance_core.parser_proposals.human_draft_delivery import (
    active_human_draft_exists,
    begin_human_draft_card_delivery,
    find_active_human_draft_card_generation,
    get_human_draft_action_authority,
    get_human_draft_card,
    get_human_draft_presentation,
    record_human_draft_card_delivery_outcome,
    reissue_human_draft_card,
)
from finance_core.parser_proposals.human_drafts import (
    HumanDraftCommand,
    HumanDraftContext,
    HumanDraftError,
    HumanDraftResult,
    apply_human_draft_card,
    begin_human_draft_in_transaction,
)
from finance_core.parser_proposals.human_revision import (
    HumanRevisionLineageError,
    publish_human_revision_in_transaction,
)
from finance_core.parser_proposals.lifecycle import (
    CONFIRMED,
    PARSED_PENDING_CONFIRMATION,
    TERMINAL_STATUSES,
)
from finance_core.parser_proposals.receipt_facts_conversion import (
    AmbiguousReceiptInputError,
    ConversionIdempotencyConflictError,
    ConversionStagingDatabaseRejectedError,
    IncompleteReceiptInputsError,
    ReceiptFactsConversionCommand,
    ReceiptFactsConversionError,
    StaleConfirmationHashError,
    UnauthorizedConversionActorError,
    convert_confirmed_receipt_proposal_to_facts,
    derive_receipt_public_id,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    InvalidItemFactsCommandError,
    ItemFactSetAlreadyExistsError,
    ItemFactsIdempotencyConflictError,
    ItemFactsReceiptNotFoundError,
    ItemFactsStagingDatabaseRejectedError,
    ReceiptItemAllocationFactsCommand,
    ReceiptItemAllocationFactsError,
    StaleItemFactSetVersionError,
    StaleItemFactsReceiptBindingError,
    UnauthorizedItemFactsActorError,
    persist_receipt_item_allocation_facts,
)
from finance_core.parser_proposals.receipt_supersession import (
    InvalidSupersessionFieldValueError,
    NoMaterialSupersessionChangeError,
    NonMonetarySupersessionError,
    ReceiptSupersessionError,
    StaleSupersessionContentError,
    UnknownSupersessionFieldError,
    get_receipt_proposal_revision_by_correction_id,
    supersede_receipt_total_proposal,
)
from finance_core.parser_proposals.repository import (
    ParserAuthorizationRepository,
    ParserConversionRepository,
    ParserProposalRepository,
)
from finance_core.parser_proposals.service import (
    AlreadyConvertedProposalError,
    InvalidProposalStatusError,
    MissingConfirmationRecordError,
    ParserConfirmationError,
    StaleProposalConfirmationError,
    StaleProposalDecisionStateError,
    UnsupportedProposalTypeError,
    convert_confirmed_parser_proposal,
)
from finance_core.persistence_fingerprint import canonical_fingerprint
from finance_core.receipt_finalization import (
    BridgeAuthorizationActorError,
    BridgeAuthorizationConflictError,
    BridgeBindingAuthorityError,
    BridgeCalculationRunConflictError,
    BridgePreparationError,
    BridgeRecoveryError,
    PreparedReceiptCalculation,
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    load_persisted_receipt_finalization_authorization,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.snapshot_authority import (
    SnapshotAuthorityError,
    read_snapshot_bound_authority,
)
from finance_core.receipt_finalization.stage_read import (
    read_receipt_conversion_registry,
    read_receipt_finalization_stage,
)
from finance_core.receipt_staging_runner import workspace as runner_workspace
from finance_core.receipt_staging_runner.models import CallbackKeyMissingError, RunnerWorkspaceError
from finance_core.receipt_staging_runner.participants import read_participants
from finance_core.telegram_source_context import (
    TelegramSourceContext,
    TelegramSourceContextError,
    record_telegram_source_context,
    require_telegram_source_context,
)

DEFAULT_DEADLINE_SECONDS = 30.0
PROCESS_CAPTURE_LEASE_MS = 90_000

BRIDGE_CONFIRMATION_CHANNEL = "openclaw_staging_bridge"

TELEGRAM_PHOTO_SOURCE_TYPE = "telegram_image"
BRIDGE_RAW_INTAKE_PREFIX = "raw_intake_bridge_"

_MIN_TOKEN_TTL_SECONDS = 60
_MAX_TOKEN_TTL_SECONDS = 86_400
_DEFAULT_TOKEN_TTL_SECONDS = 3_600
_MAX_CAPTION_LENGTH = 2_000
_MAX_FIELD_VALUE_LENGTH = application_review.MAX_REVIEW_FIELD_LENGTH
_MAX_ACTOR_ID_LENGTH = 128
_CONTENT_HASH_LENGTH = 64

_MONETARY_FIELDS = frozenset({"amount", "currency"})
_NON_MONETARY_FIELDS = frozenset({"transaction_date", "merchant", "description", "category"})
_ALLOWED_EDIT_FIELDS = _MONETARY_FIELDS | _NON_MONETARY_FIELDS


# ---------------------------------------------------------------------------
# Canonical idempotency material
#
# Every mutating command binds its envelope idempotency key to the durable
# identity of the command it authorizes: capture binds the Telegram message
# identity, propose binds the intake, and confirm/edit/reject bind the
# proposal plus the action.  The CLI verifies the supplied key against the
# canonical derivation and refuses mismatches fail-closed, so identical keys
# can never authorize different material.
# ---------------------------------------------------------------------------


def canonical_capture_key(*, chat_id: int, message_id: int) -> str:
    return f"raw-intake:telegram:{chat_id}:{message_id}"


def canonical_propose_key(intake_public_id: str) -> str:
    return f"bridge-propose:{intake_public_id}"


def canonical_decision_key(*, action: str, proposal_public_id: str) -> str:
    return f"bridge-{action}:{proposal_public_id}"


def canonical_edit_key(*, proposal_public_id: str, version: int, content_hash: str) -> str:
    # Every edit version carries its own idempotency identity: binding the
    # pre-edit version and content hash keeps consecutive edits on the same
    # proposal from colliding on a shared completion/correction public ID,
    # while an identical redelivery of one version replays safely.
    return f"bridge-edit:{proposal_public_id}:v{version}:{content_hash}"


def canonical_human_action_issuance_key(reference_batch_id: str) -> str:
    return f"bridge-human-action-issue:{reference_batch_id}"


def _persisted_human_action_issuance_keys(
    reference_batch_id: str, *, key: bytes
) -> tuple[str, ...]:
    # Migration 041 fixes the persisted suffix at 32 hex characters while D1
    # publishes a 64-hex batch.  Secret-derived alternatives keep that legacy
    # column compatible without exposing a deterministic key that another
    # public issuance can pre-claim.
    is_d1 = len(reference_batch_id) == 64 and all(
        character in "0123456789abcdef" for character in reference_batch_id
    )
    candidates = [] if is_d1 else [canonical_human_action_issuance_key(reference_batch_id)]
    domain = (
        "d1-human-action-issuance-private-v2"
        if is_d1
        else "legacy-human-action-issuance-fallback-v1"
    )
    for slot in range(16 - len(candidates)):
        parts = (domain, reference_batch_id, str(slot))
        material = b"".join(
            len(encoded).to_bytes(4, "big") + encoded
            for encoded in (part.encode("utf-8") for part in parts)
        )
        suffix = hmac.new(key, material, hashlib.sha256).hexdigest()[:32]
        candidate = canonical_human_action_issuance_key(suffix)
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def canonical_human_action_redemption_key(callback_id: str) -> str:
    digest = hashlib.sha256(callback_id.encode("utf-8")).hexdigest()[:32]
    return f"bridge-human-action-redeem:{digest}"


def canonical_prepare_posting_review_key(card_generation_public_id: str) -> str:
    return f"bridge-d2-prepare:{card_generation_public_id}"


def canonical_prepare_initial_posting_review_key(
    proposal_public_id: str, admitted_source_message_id: str
) -> str:
    return f"bridge-d2-prepare-initial:{proposal_public_id}:{admitted_source_message_id}"


def canonical_issue_posting_review_actions_key(review_public_id: str) -> str:
    return f"bridge-d2-issue:{review_public_id}"


def canonical_confirm_and_post_key(callback_id: str) -> str:
    digest = hashlib.sha256(callback_id.encode("utf-8")).hexdigest()[:32]
    return f"bridge-d2-confirm:{digest}"


def canonical_resume_posting_key(attempt_public_id: str) -> str:
    return f"bridge-d2-resume:{attempt_public_id}"


def canonical_guided_edit_update_key(session_public_id: str, message_id: int) -> str:
    return f"bridge-guided-edit-update:{session_public_id}:{message_id}"


def canonical_guided_edit_complete_key(session_public_id: str, message_id: int) -> str:
    return f"bridge-guided-edit-complete:{session_public_id}:{message_id}"


def canonical_human_draft_apply_key(operation_public_id: str) -> str:
    return f"bridge-human-draft-apply:{operation_public_id}"


def canonical_human_draft_delivery_key(attempt_public_id: str) -> str:
    return f"bridge-human-draft-delivery:{attempt_public_id}"


def canonical_human_draft_observation_key(observation_public_id: str) -> str:
    return f"bridge-human-draft-observation:{observation_public_id}"


def canonical_human_draft_reissue_key(recovery_public_id: str) -> str:
    return f"bridge-human-draft-reissue:{recovery_public_id}"


def canonical_finalize_key(proposal_public_id: str) -> str:
    return f"bridge-finalize:{proposal_public_id}"


def canonical_prepare_receipt_completion_key(proposal_public_id: str) -> str:
    return f"bridge-prepare-receipt:{proposal_public_id}"


def canonical_finalization_snapshot_review_key(proposal_public_id: str) -> str:
    return f"bridge-finalization-snapshot-review:{proposal_public_id}"


def canonical_authorize_finalization_key(proposal_public_id: str) -> str:
    return f"bridge-authorize-finalization:{proposal_public_id}"


def canonical_apply_fact_set_key(proposal_public_id: str) -> str:
    return f"bridge-apply-fact-set:{proposal_public_id}"


def canonical_prepare_ai_fallback_key(intake_public_id: str) -> str:
    return identity.ai_fallback_idempotency_key("prepare_ai_fallback", intake_public_id)


def canonical_claim_ai_fallback_key(attempt_public_id: str) -> str:
    return identity.ai_fallback_idempotency_key("claim_ai_fallback_invocation", attempt_public_id)


def canonical_record_ai_fallback_key(attempt_public_id: str) -> str:
    return identity.ai_fallback_idempotency_key("record_ai_fallback_result", attempt_public_id)


def canonical_register_ai_model_receipt_v2_key(config_projection: Mapping[str, Any]) -> str:
    return f"bridge-register-ai-model-receipt-v2:{canonical_projection_hash(config_projection)}"


def canonical_prepare_ai_fallback_v2_key(intake_public_id: str) -> str:
    return identity.ai_fallback_v2_idempotency_key("prepare_ai_fallback_v2", intake_public_id)


def canonical_claim_ai_fallback_v2_key(attempt_public_id: str) -> str:
    return identity.ai_fallback_v2_idempotency_key(
        "claim_ai_fallback_invocation_v2", attempt_public_id
    )


def canonical_record_ai_fallback_v2_key(attempt_public_id: str) -> str:
    return identity.ai_fallback_v2_idempotency_key(
        "record_ai_fallback_result_v2", attempt_public_id
    )


def _require_canonical_idempotency_key(request: BridgeRequest, canonical_key: str) -> None:
    if request.idempotency_key != canonical_key:
        raise errors.bridge_error(
            errors.IDEMPOTENCY_CONFLICT,
            "idempotency_key does not match the canonical identity derived from the "
            "command arguments.",
            errors.EXIT_AUTHORITY_REFUSED,
        )


class Deadline:
    """Cooperative monotonic deadline checked between bounded phases."""

    def __init__(
        self,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._deadline = clock() + deadline_seconds
        self._clock = clock

    def check(self, phase: str) -> None:
        if self._deadline - self._clock() <= 0:
            raise errors.bridge_error(
                errors.DEADLINE_EXCEEDED,
                f"Bridge command deadline expired during {phase}.",
                errors.EXIT_DEADLINE_EXCEEDED,
            )


HandlerResult = tuple[dict[str, Any], bool]


# ---------------------------------------------------------------------------
# Argument validation helpers
# ---------------------------------------------------------------------------


def _require_exact_arguments(
    arguments: dict[str, Any], *, required: frozenset[str], optional: frozenset[str] = frozenset()
) -> None:
    allowed = required | optional
    unknown = frozenset(arguments) - allowed
    if unknown:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"Unknown argument fields: {sorted(unknown)}",
            errors.EXIT_VALIDATION_REFUSED,
        )
    missing = required - frozenset(arguments)
    if missing:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"Missing required argument fields: {sorted(missing)}",
            errors.EXIT_VALIDATION_REFUSED,
        )


def _require_string(value: object, name: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"{name} must be a non-empty string.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if len(value) > max_length:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"{name} exceeds the bounded length limit.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"{name} must contain valid Unicode scalar values.",
            errors.EXIT_VALIDATION_REFUSED,
        ) from exc
    return value


def _require_non_negative_int(value: object, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"{name} must be a non-negative integer.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if maximum is not None and value > maximum:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"{name} exceeds the bounded limit.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return value


def _require_positive_int(value: object, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"{name} must be a positive integer.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if maximum is not None and value > maximum:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"{name} exceeds the bounded limit.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return value


def _require_content_hash(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _CONTENT_HASH_LENGTH
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "content_hash must be a 64-character lowercase SHA-256 hex string.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return value


def _open_context(arguments: dict[str, Any], deadline: Deadline) -> tuple[Path, sqlite3.Connection]:
    deadline.check("workspace validation")
    workspace = workspace_access.validate_workspace_path(arguments["workspace_path"])
    deadline.check("workspace structure verification")
    workspace_access.verify_workspace_structure(workspace)
    deadline.check("database open")
    conn = workspace_access.open_workspace_database(workspace)
    return workspace, conn


def _require_durable_capture_connection(conn: sqlite3.Connection) -> None:
    """Keep a Core capture committed after the host discards its spool copy."""
    try:
        require_durable_capture_connection(conn)
    except DurableCaptureConnectionError as exc:
        raise errors.bridge_error(
            errors.INTERNAL_ERROR,
            "Core capture could not prove a durable SQLite commit setting.",
            errors.EXIT_INTERNAL,
        ) from exc


def _map_review_error(exc: application_review.ReviewError) -> errors.BridgeError:
    if isinstance(exc, application_review.ReviewNotFoundError):
        return errors.bridge_error(
            errors.PROPOSAL_NOT_FOUND, str(exc), errors.EXIT_VALIDATION_REFUSED
        )
    return errors.bridge_error(errors.PROPOSAL_UNAVAILABLE, str(exc), errors.EXIT_AUTHORITY_REFUSED)


def _fetch_proposal_by_public_id(
    conn: sqlite3.Connection, proposal_public_id: str
) -> dict[str, Any]:
    try:
        return application_review.read_proposal(conn, proposal_public_id)
    except application_review.ReviewError as exc:
        raise _map_review_error(exc) from exc


def _proposal_effective_state(
    conn: sqlite3.Connection, proposal: dict[str, Any]
) -> tuple[dict[str, Any], int, str]:
    return application_review.effective_state(conn, proposal)


def _conversion_status(conn: sqlite3.Connection, parser_output_id: int) -> str:
    if has_legacy_transaction_conversion(conn, parser_output_id) or has_receipt_registry_conversion(
        conn, parser_output_id
    ):
        return "converted"
    return "not_converted"


# ---------------------------------------------------------------------------
# S4 finalize helpers
#
# The bridge contains no direct SQL and no financial logic: these helpers
# only fail closed on durable truth read through owning-module boundaries
# (confirmation record, participant projection, readiness report, stage
# projection) and map typed boundary errors onto the stable S4 codes.
# ---------------------------------------------------------------------------


def _sqlite_busy(exc: Exception) -> bool:
    """Detect SQLite write-lock contention on an arbitrary exception chain.

    Walks both explicit ``__cause__`` and implicit ``__context__`` chains
    (cycle-protected) so a boundary that wraps ``OperationalError`` without
    ``raise ... from`` still maps lock contention to the retryable
    ``FINALIZATION_LOCKED`` instead of a misleading non-retryable refusal.
    """
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        cursor = pending.pop()
        if cursor is None or id(cursor) in seen:
            continue
        seen.add(id(cursor))
        if isinstance(cursor, sqlite3.OperationalError):
            text = str(cursor)
            if "database is locked" in text or "database is busy" in text:
                return True
        pending.append(cursor.__cause__)
        pending.append(cursor.__context__)
    return False


def _finalization_refused(
    reason: str, message: str, *, extra_details: dict[str, str | int] | None = None
) -> errors.BridgeError:
    details: dict[str, str | int] = {"reason": reason}
    if extra_details:
        details.update(extra_details)
    return errors.bridge_error(
        errors.FINALIZATION_REFUSED,
        message,
        errors.EXIT_AUTHORITY_REFUSED,
        details=details,
    )


def _finalization_locked(subject: str) -> errors.BridgeError:
    return errors.bridge_error(
        errors.FINALIZATION_LOCKED,
        f"{subject} is locked by another writer; retry after the lock is released.",
        errors.EXIT_AUTHORITY_REFUSED,
        retryable=True,
    )


def _require_durable_confirmed_actor(
    conn: sqlite3.Connection, proposal: dict[str, Any], operator_actor_id: str
) -> str:
    """Return the durable human confirmation actor after re-verifying it.

    The envelope actor is an identity check only; authority is the durable
    confirmation record itself.
    """
    authorization = ParserAuthorizationRepository(conn).get_for_proposal(int(proposal["id"]))
    if (
        authorization is None
        or str(authorization.get("actor_type") or "") != "human"
        or not str(authorization.get("authenticated_actor_id") or "").strip()
    ):
        raise _finalization_refused(
            "missing_confirmation",
            "Proposal has no durable authenticated human confirmation record.",
        )
    durable_actor = str(authorization["authenticated_actor_id"])
    if operator_actor_id != durable_actor:
        raise errors.bridge_error(
            errors.ACTOR_MISMATCH,
            "operator_actor_id does not match the durable confirmed-decision actor.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    return durable_actor


def _require_single_self_participant(conn: sqlite3.Connection) -> str:
    """Fail closed unless the durable participant table is personal-only.

    Mirrors the proven B5.1 runner personal-only semantics structurally:
    exactly one active participant, which is the self participant.  No
    amounts or allocation semantics are read or derived here.
    """
    rows = read_participants(conn)
    if not rows:
        raise _finalization_refused(
            "participants_not_bootstrapped",
            "The workspace participant table is empty; bootstrap participants "
            "out-of-envelope before receipt finalization.",
        )
    active = tuple(row for row in rows if row.is_active)
    self_rows = tuple(row for row in active if row.is_self)
    if len(active) != 1 or len(self_rows) != 1:
        raise _finalization_refused(
            "personal_only_violated",
            "Receipt finalization requires exactly one active participant, "
            "which must be the self participant.",
        )
    return self_rows[0].public_id


def _receipt_identities_for(proposal_public_id: str, content_hash: str) -> tuple[str, str]:
    """Deterministic (conversion command ID, receipt public ID) pair."""
    command_public_id = identity.receipt_conversion_command_public_id(
        proposal_public_id, content_hash
    )
    return command_public_id, derive_receipt_public_id(command_public_id)


def _require_ready_receipt(
    conn: sqlite3.Connection,
    receipt_public_id: str,
    *,
    command_public_id: str,
    conversion_result_hash: str | None = None,
) -> None:
    """Fail closed unless a durable human-authored ready fact set exists.

    D1b: the fact set is authored out-of-envelope and persisted through the
    existing guarded IAF boundary; the readiness boundary is the sole judge.
    The refusal carries the bounded identities a human author needs —
    including the conversion result hash the IAF command must bind to.
    """
    try:
        report = report_receipt_calculator_readiness(conn, receipt_public_id)
    except ReceiptFactsIntegrityError as exc:
        raise _finalization_refused(
            "fact_set_integrity_refused",
            f"Receipt fact-set integrity verification failed: {exc}",
        ) from exc
    if report.is_calculator_ready:
        return
    extra_details: dict[str, str | int] = {
        "receipt_public_id": receipt_public_id,
        "conversion_command_public_id": command_public_id,
        "not_ready_reasons": ",".join(report.not_ready_reasons),
    }
    if conversion_result_hash is not None:
        extra_details["conversion_result_hash"] = conversion_result_hash
    raise _finalization_refused(
        "no_authoritative_item_facts",
        "Receipt finalization requires an existing, durable, human-authored, "
        "calculator-ready IAF fact set persisted out-of-envelope (D1b).",
        extra_details=extra_details,
    )


def _finalization_view(
    conn: sqlite3.Connection, proposal: dict[str, Any], content_hash: str
) -> dict[str, Any]:
    """Bounded finalization-state projection for get_status (read-only).

    Reconstructed solely from durable truth through owning-module read
    boundaries; never infers finalization from a confirmation record.
    """
    if str(proposal["parse_status"]) != CONFIRMED:
        return {"finalization_state": "unconfirmed", "final_transaction_created": False}
    parser_output_id = int(proposal["id"])
    if not has_receipt_ocr_proposal_link(conn, parser_output_id):
        if not has_legacy_transaction_conversion(conn, parser_output_id):
            return {
                "finalization_state": "confirmed_incomplete",
                "final_transaction_created": False,
            }
        conversion_row = ParserConversionRepository(conn).get_for_proposal(parser_output_id)
        if conversion_row is None or not conversion_row.get("transaction_public_id"):
            # An audit row without its canonical transaction is drift, never
            # finalization; stay visibly incomplete.
            return {
                "finalization_state": "confirmed_incomplete",
                "final_transaction_created": False,
            }
        return {
            "finalization_state": "finalized",
            "final_transaction_created": True,
            "transaction_public_id": str(conversion_row["transaction_public_id"]),
        }

    proposal_public_id = str(proposal["public_id"])
    command_public_id, receipt_public_id = _receipt_identities_for(proposal_public_id, content_hash)
    try:
        report = report_receipt_calculator_readiness(conn, receipt_public_id)
    except ReceiptNotFoundError:
        return {
            "finalization_state": "confirmed_incomplete",
            "final_transaction_created": False,
        }
    except ReceiptFactsIntegrityError:
        return {
            "finalization_state": "confirmed_incomplete",
            "final_transaction_created": False,
            "readiness_reasons": ("fact_set_integrity_refused",),
        }
    if not report.is_calculator_ready:
        return {
            "finalization_state": "confirmed_incomplete",
            "final_transaction_created": False,
            "receipt_public_id": receipt_public_id,
            "readiness_reasons": tuple(report.not_ready_reasons),
        }
    stage = read_receipt_finalization_stage(conn, receipt_public_id=receipt_public_id)
    if (
        stage.finalization_status in ("finalized", "already_finalized")
        and stage.transaction_public_id
    ):
        finalized_view: dict[str, Any] = {
            "finalization_state": "finalized",
            "final_transaction_created": True,
            "receipt_public_id": receipt_public_id,
            "transaction_public_id": stage.transaction_public_id,
        }
        for key, value in (
            ("finalization_public_id", stage.finalization_public_id),
            ("authorization_id", stage.authorization_id),
            ("calculation_snapshot_id", stage.calculation_snapshot_id),
            ("calculation_snapshot_hash", stage.calculation_snapshot_hash),
            ("fact_set_public_id", report.active_fact_set_public_id),
        ):
            if value is not None:
                finalized_view[key] = value
        if report.active_fact_set_version is not None:
            finalized_view["fact_set_version"] = report.active_fact_set_version
        return finalized_view
    if stage.authorization_state == "authorized":
        authorized_view: dict[str, Any] = {
            "finalization_state": "authorized_pending_finalization",
            "final_transaction_created": False,
            "receipt_public_id": receipt_public_id,
        }
        for key, value in (
            ("authorization_id", stage.authorization_id),
            ("calculation_snapshot_id", stage.calculation_snapshot_id),
            ("calculation_snapshot_hash", stage.calculation_snapshot_hash),
        ):
            if value is not None:
                authorized_view[key] = value
        return authorized_view
    if stage.authorization_id is not None:
        # A non-authorized, non-consumed durable authorization state is an
        # anomaly; it stays visibly incomplete, never finalized.
        return {
            "finalization_state": "confirmed_incomplete",
            "final_transaction_created": False,
            "receipt_public_id": receipt_public_id,
        }
    if stage.calculation_snapshot_id is not None:
        prepared_view: dict[str, Any] = {
            "finalization_state": "prepared_pending_authorization",
            "final_transaction_created": False,
            "receipt_public_id": receipt_public_id,
            "calculation_snapshot_id": stage.calculation_snapshot_id,
        }
        if stage.calculation_snapshot_hash is not None:
            prepared_view["calculation_snapshot_hash"] = stage.calculation_snapshot_hash
        return prepared_view
    return {
        "finalization_state": "confirmed_incomplete",
        "final_transaction_created": False,
        "receipt_public_id": receipt_public_id,
    }


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def handle_health(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(request.arguments, required=frozenset({"workspace_path"}))
    workspace, conn = _open_context(request.arguments, deadline)
    try:
        from finance_core.reconciliation.migrations import migration_ledger_rows

        ledger = migration_ledger_rows(conn)
        latest = str(ledger[-1].get("migration_id", "")) if ledger else ""
        key_status = runner_workspace.callback_key_status(str(workspace / "runtime"))
        result = {
            "workspace_verified": True,
            "database_verified": True,
            "migration_ledger_count": len(ledger),
            "latest_migration": latest,
            "callback_key_status": key_status,
        }
        return result, False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def handle_get_status(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path"}),
        optional=frozenset(
            {
                "intake_public_id",
                "job_public_id",
                "proposal_public_id",
                "posting_review_public_id",
                "short_reference",
                "operator_actor_id",
                "telegram_account_id",
                "telegram_conversation_id",
                "conversation_binding_id",
            }
        ),
    )
    intake_public_id = request.arguments.get("intake_public_id")
    job_public_id = request.arguments.get("job_public_id")
    proposal_public_id = request.arguments.get("proposal_public_id")
    posting_review_public_id = request.arguments.get("posting_review_public_id")
    short_reference = request.arguments.get("short_reference")
    identities = tuple(
        value
        for value in (
            intake_public_id,
            job_public_id,
            proposal_public_id,
            posting_review_public_id,
            short_reference,
        )
        if value is not None
    )
    if len(identities) != 1:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Provide exactly one supported status identity.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    posting_lookup = posting_review_public_id is not None or short_reference is not None
    context_fields = {
        "operator_actor_id",
        "telegram_account_id",
        "telegram_conversation_id",
        "conversation_binding_id",
    }
    supplied_context = context_fields & set(request.arguments)
    if (posting_lookup and supplied_context != context_fields) or (
        not posting_lookup and supplied_context
    ):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Posting status requires the complete authenticated Telegram context.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("status read")
        if posting_lookup:
            from finance_core import posting_authority

            context = _require_telegram_human_context(request.arguments)
            try:
                status = (
                    posting_authority.get_status(
                        conn,
                        review_public_id=_require_string(
                            posting_review_public_id,
                            "posting_review_public_id",
                            max_length=64,
                        ),
                        context=context,
                    )
                    if posting_review_public_id is not None
                    else posting_authority.get_status_by_reference(
                        conn,
                        reference=_require_string(
                            short_reference,
                            "short_reference",
                            max_length=64,
                        ),
                        context=context,
                    )
                )
            except posting_authority.PostingAuthorityError as exc:
                _raise_posting_authority_error(exc)
            return {
                "identity_kind": "posting_review",
                **_posting_status_payload(status),
            }, False
        if proposal_public_id is not None:
            proposal = _fetch_proposal_by_public_id(
                conn, _require_string(proposal_public_id, "proposal_public_id", max_length=200)
            )
            _payload, version, content_hash = _proposal_effective_state(conn, proposal)
            deadline.check("finalization state reconstruction")
            result = {
                "identity_kind": "proposal",
                "proposal_public_id": proposal["public_id"],
                "intake_public_id": proposal["source_public_id"],
                "parse_status": proposal["parse_status"],
                "proposal_version": version,
                "effective_content_hash": content_hash,
                "conversion_status": _conversion_status(conn, int(proposal["id"])),
            }
            result.update(_finalization_view(conn, proposal, content_hash))
            return result, False

        if job_public_id is not None:
            job = get_capture_job(
                conn,
                public_id=_require_string(job_public_id, "job_public_id", max_length=200),
            )
            if job is None:
                raise errors.bridge_error(
                    errors.INTAKE_NOT_FOUND,
                    "Capture job was not found in the staging database.",
                    errors.EXIT_VALIDATION_REFUSED,
                )
            intake_public_id = job["intake_public_id"]
        public_id = _require_string(intake_public_id, "intake_public_id", max_length=200)
        intake = get_raw_intake_record_by_public_id(conn, public_id)
        if intake is None:
            raise errors.bridge_error(
                errors.INTAKE_NOT_FOUND,
                "Intake public ID was not found in the staging database.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        proposal_public_id_value: str | None = None
        parse_status: str | None = None
        if intake["parser_output_id"] is not None:
            proposal_row = ParserProposalRepository(conn).get(int(intake["parser_output_id"]))
            if proposal_row is not None:
                proposal_public_id_value = str(proposal_row["public_id"])
                parse_status = str(proposal_row["parse_status"])
        capture_job = get_capture_job(conn, intake_public_id=public_id)
        capture_attachment_integrity: str | None = None
        if capture_job is not None and capture_job["capture_kind"] == "receipt_image":
            source_id = capture_job["attachment_evidence_id"]
            expected_hash = capture_job["attachment_content_hash"]
            intake_attachment_id = intake["attachment_id"]
            try:
                if (
                    not isinstance(source_id, int)
                    or not isinstance(expected_hash, str)
                    or not isinstance(intake_attachment_id, int)
                ):
                    raise ValueError("Receipt capture job has incomplete original evidence")
                verify_telegram_original_attachment(
                    conn,
                    source_id=source_id,
                    expected_hash=expected_hash,
                    expected_intake_id=int(capture_job["raw_intake_record_id"]),
                    expected_attachment_id=intake_attachment_id,
                )
            except (ReceiptOcrError, ValueError):
                capture_attachment_integrity = "missing"
            else:
                capture_attachment_integrity = "verified"
            deadline.check("receipt original status verification")
        return {
            "identity_kind": "intake",
            "intake_public_id": intake["public_id"],
            "intake_status": intake["status"],
            "source_type": intake["source_type"],
            "proposal_public_id": proposal_public_id_value,
            "parse_status": parse_status,
            "final_transaction_created": False,
            "capture_job": capture_job,
            "capture_attachment_integrity": capture_attachment_integrity,
        }, False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


_D2_CAPTURE_CONTEXT_FIELDS = frozenset(
    {
        "authenticated_actor_id",
        "telegram_account_id",
        "telegram_conversation_id",
        "conversation_binding_id",
    }
)


def _validated_capture_source_context(
    arguments: dict[str, Any],
    *,
    chat_id: int,
    message_id: int,
    sender_id: int | None,
) -> TelegramSourceContext | None:
    supplied = frozenset(arguments) & _D2_CAPTURE_CONTEXT_FIELDS
    if not supplied:
        return None
    if supplied != _D2_CAPTURE_CONTEXT_FIELDS:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "D2 capture source context fields must be supplied together.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    actor_id = _require_string(
        arguments["authenticated_actor_id"],
        "authenticated_actor_id",
        max_length=_MAX_ACTOR_ID_LENGTH,
    )
    account_id = _require_string(
        arguments["telegram_account_id"], "telegram_account_id", max_length=200
    )
    conversation_id = _require_string(
        arguments["telegram_conversation_id"],
        "telegram_conversation_id",
        max_length=200,
    )
    binding_id = _require_string(
        arguments["conversation_binding_id"],
        "conversation_binding_id",
        max_length=500,
    )
    if sender_id is None or actor_id != str(sender_id) or conversation_id != str(chat_id):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "D2 capture source context does not match the Telegram message.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    return TelegramSourceContext(
        authenticated_actor_id=actor_id,
        account_id=account_id,
        conversation_id=conversation_id,
        binding_id=binding_id,
        message_id=str(message_id),
    )


def _capture_context_effect(
    source_context: TelegramSourceContext | None,
) -> Callable[[sqlite3.Connection, dict[str, Any]], None] | None:
    if source_context is None:
        return None

    def persist(conn: sqlite3.Connection, intake: dict[str, Any]) -> None:
        try:
            record_telegram_source_context(
                conn,
                raw_intake_record_id=int(intake["id"]),
                context=source_context,
                captured_at=str(intake["received_at"]),
            )
        except TelegramSourceContextError as exc:
            raise errors.bridge_error(
                errors.IDEMPOTENCY_CONFLICT,
                "Capture source context conflicts with durable intake identity.",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc

    return persist


def _capture_text_effect(
    source_context: TelegramSourceContext | None,
    ingress_identity_digest: str | None,
) -> Callable[[sqlite3.Connection, dict[str, Any]], None]:
    context_effect = _capture_context_effect(source_context)

    def persist(conn: sqlite3.Connection, intake: dict[str, Any]) -> None:
        if context_effect is not None:
            context_effect(conn, intake)
        ensure_capture_job(
            conn,
            intake_id=int(intake["id"]),
            capture_kind="text",
            ingress_identity_digest=ingress_identity_digest,
        )

    return persist


def _ensure_replayed_capture_job(
    conn: sqlite3.Connection, *, intake_id: int, ingress_identity_digest: str | None
) -> dict[str, Any]:
    # A pre-D3 capture may exist without a job. Enlist it before reporting
    # successful D3 capture, including when replay wins a concurrent insert.
    try:
        return ensure_replayed_text_capture_job(
            conn, intake_id=intake_id, ingress_identity_digest=ingress_identity_digest
        )
    except CaptureJobConflictError as exc:
        raise _capture_job_conflict(exc) from exc


def _capture_job_conflict(exc: CaptureJobConflictError) -> errors.BridgeError:
    return errors.bridge_error(
        errors.IDEMPOTENCY_CONFLICT,
        "Capture job conflicts with persisted source evidence.",
        errors.EXIT_AUTHORITY_REFUSED,
    )


def _validated_finance_ingress(
    arguments: dict[str, Any],
    *,
    capture_kind: str,
    chat_id: int,
    message_id: int,
    sender_id: int | None,
    source_context: TelegramSourceContext | None,
    update_id: int | None,
) -> tuple[str | None, str | None]:
    ingress = arguments.get("finance_ingress")
    if ingress is None:
        return None, None
    required = {
        "channel",
        "accountId",
        "updateId",
        "chatId",
        "messageId",
        "senderId",
        "payloadSha256",
        "bindingId",
    }
    expected = required | ({"attachmentSha256"} if capture_kind == "receipt_image" else set())
    if not isinstance(ingress, dict) or set(ingress) != expected:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Finance ingress identity has unexpected fields.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if ingress["channel"] != "telegram" or source_context is None:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Finance ingress requires authenticated Telegram source context.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    update = _require_non_negative_int(ingress["updateId"], "finance_ingress.updateId")
    mapped = (
        (_require_non_negative_int(ingress["chatId"], "finance_ingress.chatId"), chat_id),
        (_require_positive_int(ingress["messageId"], "finance_ingress.messageId"), message_id),
        (_require_non_negative_int(ingress["senderId"], "finance_ingress.senderId"), sender_id),
    )
    if any(given != expected_value for given, expected_value in mapped) or (
        update_id is not None and update != update_id
    ):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Finance ingress Telegram identity does not match capture source.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    account = _require_string(ingress["accountId"], "finance_ingress.accountId", max_length=200)
    binding = _require_string(ingress["bindingId"], "finance_ingress.bindingId", max_length=500)
    if account != source_context.account_id or binding != source_context.binding_id:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Finance ingress account or binding conflicts with authenticated source.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    hashes = (
        ("payloadSha256", "attachmentSha256")
        if capture_kind == "receipt_image"
        else ("payloadSha256",)
    )
    for name in hashes:
        digest = _require_string(ingress[name], f"finance_ingress.{name}", max_length=64)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                "Finance ingress SHA-256 value is invalid.",
                errors.EXIT_VALIDATION_REFUSED,
            )
    material = json.dumps(ingress, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest(), ingress.get("attachmentSha256")


def _require_replay_source_context(
    conn: sqlite3.Connection,
    intake: dict[str, Any],
    source_context: TelegramSourceContext | None,
) -> None:
    if source_context is None:
        return
    try:
        require_telegram_source_context(
            conn,
            raw_intake_record_id=int(intake["id"]),
            context=source_context,
        )
    except TelegramSourceContextError as exc:
        raise errors.bridge_error(
            errors.IDEMPOTENCY_CONFLICT,
            "Capture source context conflicts with durable intake identity.",
            errors.EXIT_AUTHORITY_REFUSED,
        ) from exc


def handle_capture(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    kind = request.arguments.get("kind")
    if kind == "text":
        return _capture_text(request, deadline)
    if kind == "receipt_image":
        return _capture_receipt_image(request, deadline)
    raise errors.bridge_error(
        errors.ARGUMENTS_REFUSED,
        "capture.kind must be 'text' or 'receipt_image'.",
        errors.EXIT_VALIDATION_REFUSED,
    )


def _expected_text_fingerprint(validated: Any) -> str:
    return canonical_fingerprint(
        schema_version="raw-intake-v1",
        material={
            "source_type": TELEGRAM_TEXT,
            "source_channel": "telegram",
            "external_source_id": (f"telegram:{validated.chat_id}:{validated.message_id}"),
            "source_message_id": str(validated.message_id),
            "raw_input": validated.text,
            "attachment_content_hash": None,
        },
    )


def _capture_text(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    has_update = "telegram_update" in request.arguments
    has_message = "telegram_message" in request.arguments
    if has_update == has_message:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Text capture requires exactly one Telegram payload shape.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    payload_field = "telegram_update" if has_update else "telegram_message"
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "kind", payload_field}),
        optional=_D2_CAPTURE_CONTEXT_FIELDS | frozenset({"finance_ingress"}),
    )
    payload = request.arguments[payload_field]
    policy_payload = payload if has_update else {"message": payload}
    # Structural private-DM policy first: group/supergroup shapes are refused
    # before any validation or persistence.
    telegram_boundary.validate_private_direct_message(policy_payload)
    try:
        validated = (
            validate_telegram_text_update(payload)
            if has_update
            else validate_openclaw_telegram_text_message(payload)
        )
    except TelegramTextUpdateValidationError as exc:
        code = (
            errors.UNSUPPORTED_UPDATE_TYPE
            if exc.reason_code == "UNSUPPORTED_UPDATE_TYPE"
            else errors.ARGUMENTS_REFUSED
        )
        raise errors.bridge_error(code, str(exc), errors.EXIT_VALIDATION_REFUSED) from exc
    source_context = _validated_capture_source_context(
        request.arguments,
        chat_id=validated.chat_id,
        message_id=validated.message_id,
        sender_id=validated.sender_id,
    )
    ingress_digest, _unused_attachment_hash = _validated_finance_ingress(
        request.arguments,
        capture_kind="text",
        chat_id=validated.chat_id,
        message_id=validated.message_id,
        sender_id=validated.sender_id,
        source_context=source_context,
        update_id=validated.update_id,
    )

    # The idempotency key must bind the durable Telegram message identity.
    _require_canonical_idempotency_key(
        request,
        canonical_capture_key(chat_id=validated.chat_id, message_id=validated.message_id),
    )

    workspace, conn = _open_context(request.arguments, deadline)
    try:
        _require_durable_capture_connection(conn)
        deadline.check("capture replay inspection")
        idempotency_key = f"raw-intake:telegram:{validated.chat_id}:{validated.message_id}"
        existing = get_raw_intake_record_by_idempotency_key(conn, idempotency_key)
        if existing is not None:
            expected_fingerprint = _expected_text_fingerprint(validated)
            if existing.get("content_fingerprint") != expected_fingerprint:
                raise errors.bridge_error(
                    errors.IDEMPOTENCY_CONFLICT,
                    "Capture idempotency key is already bound to different content.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            _require_replay_source_context(conn, existing, source_context)
            _ensure_replayed_capture_job(
                conn, intake_id=int(existing["id"]), ingress_identity_digest=ingress_digest
            )
            return _text_capture_result(conn, existing), True

        deadline.check("capture persistence")
        try:
            persistence_effect = _capture_text_effect(source_context, ingress_digest)
            if has_update:
                result = process_telegram_text_update(
                    conn, payload, persistence_effect=persistence_effect
                )
            else:
                result = process_openclaw_telegram_text_message(
                    conn, payload, persistence_effect=persistence_effect
                )
        except RawIntakeIdempotencyConflictError as exc:
            raise errors.bridge_error(
                errors.IDEMPOTENCY_CONFLICT,
                "Capture idempotency key is already bound to different content.",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc
        except sqlite3.IntegrityError as exc:
            # Concurrent first-capture race on the deterministic identity:
            # classify through the persisted winner's fingerprint instead of
            # surfacing a retryable internal error.
            winner = get_raw_intake_record_by_idempotency_key(conn, idempotency_key)
            if winner is None or winner.get("content_fingerprint") != _expected_text_fingerprint(
                validated
            ):
                raise errors.bridge_error(
                    errors.IDEMPOTENCY_CONFLICT,
                    "Capture idempotency key is already bound to different content.",
                    errors.EXIT_AUTHORITY_REFUSED,
                ) from exc
            _require_replay_source_context(conn, winner, source_context)
            _ensure_replayed_capture_job(
                conn, intake_id=int(winner["id"]), ingress_identity_digest=ingress_digest
            )
            return _text_capture_result(conn, winner), True
        except CaptureJobConflictError as exc:
            raise _capture_job_conflict(exc) from exc
        return _text_capture_result(conn, result["intake"]), False
    finally:
        conn.close()


def _text_capture_result(conn: sqlite3.Connection, intake: dict[str, Any]) -> dict[str, Any]:
    proposal_public_id: str | None = None
    parse_status: str | None = None
    if intake.get("parser_output_id") is not None:
        row = ParserProposalRepository(conn).get(int(intake["parser_output_id"]))
        if row is not None:
            proposal_public_id = str(row["public_id"])
            parse_status = str(row["parse_status"])
    return {
        "capture_kind": "text",
        "intake_public_id": intake["public_id"],
        "intake_status": intake["status"],
        "proposal_public_id": proposal_public_id,
        "parse_status": parse_status,
        "final_transaction_created": False,
        "capture_job": get_capture_job(conn, intake_public_id=str(intake["public_id"])),
    }


def _require_replay_content_matches(
    conn: sqlite3.Connection,
    intake: dict[str, Any],
    handoff_path: Path,
    provided_content: bytes | None = None,
) -> bytes | None:
    """Fail closed when a replayed capture carries different handoff content.

    Returns the handoff bytes when the check read them, so publication can
    reuse the exact bound content instead of re-reading the file.
    """
    evidence_row = get_telegram_source_evidence_for_raw_intake(conn, int(intake["id"]))
    if evidence_row is not None:
        if provided_content is None:
            try:
                content, _size = read_handoff_file(handoff_path)
            except errors.BridgeError as exc:
                if exc.code == errors.HANDOFF_NOT_FOUND:
                    # A missing handoff on replay is tolerated: durable bytes are
                    # already published and persisted, so the replay proceeds from
                    # durable truth.  Every other handoff refusal (empty,
                    # oversized, symlink, non-regular, unreadable) fails closed
                    # without deleting the file or touching persisted state.
                    return None
                raise
        else:
            content = provided_content
        if hashlib.sha256(content).hexdigest() != evidence_row["content_hash"]:
            raise errors.bridge_error(
                errors.IDEMPOTENCY_CONFLICT,
                "Capture idempotency key is already bound to different handoff content.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        return content

    # Crash window: the intake row was persisted but evidence persistence
    # never committed, so no durable truth exists yet.  The handoff file is
    # required, and its content must match the content hash bound into the
    # intake's fingerprint at creation time.
    if provided_content is None:
        content, _size = read_handoff_file(handoff_path)
    else:
        content = provided_content
    expected_fingerprint = canonical_fingerprint(
        schema_version="raw-intake-v1",
        material={
            "source_type": intake["source_type"],
            "source_channel": intake["source_channel"],
            "external_source_id": intake["external_source_id"],
            "source_message_id": intake["source_message_id"],
            "raw_input": intake["raw_input"],
            "attachment_content_hash": hashlib.sha256(content).hexdigest(),
        },
    )
    if intake.get("content_fingerprint") != expected_fingerprint:
        raise errors.bridge_error(
            errors.IDEMPOTENCY_CONFLICT,
            "Capture idempotency key is already bound to different handoff content.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    return content


def _require_replay_caption_matches(intake: dict[str, Any], caption: str) -> None:
    if intake["raw_input"] != (caption or "[telegram receipt image]"):
        raise errors.bridge_error(
            errors.IDEMPOTENCY_CONFLICT,
            "Capture idempotency key is already bound to a different receipt caption.",
            errors.EXIT_AUTHORITY_REFUSED,
        )


def _capture_receipt_image(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset(
            {
                "workspace_path",
                "kind",
                "handoff_filename",
                "telegram_message_id",
                "telegram_chat_id",
            }
        ),
        optional=frozenset(
            {
                "telegram_update_id",
                "telegram_message_date",
                "handoff_descriptor_fd",
                "handoff_content_hash",
                "sender_id",
                "original_filename",
                "declared_mime_type",
                "caption",
                "finance_ingress",
            }
        )
        | _D2_CAPTURE_CONTEXT_FIELDS,
    )
    arguments = request.arguments
    has_update_id = arguments.get("telegram_update_id") is not None
    has_message_date = arguments.get("telegram_message_date") is not None
    if has_update_id == has_message_date:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Receipt capture requires exactly one transport timestamp identity.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    update_id: int | None = None
    message_date: int | None = None
    message_received_at: str | None = None
    if has_update_id:
        update_id = _require_non_negative_int(arguments["telegram_update_id"], "telegram_update_id")
    else:
        message_date = _require_non_negative_int(
            arguments["telegram_message_date"], "telegram_message_date"
        )
        if message_date < 1_262_304_000:
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                "telegram_message_date predates Telegram.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        try:
            message_received_at = datetime.fromtimestamp(message_date, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                "telegram_message_date is outside the supported UTC range.",
                errors.EXIT_VALIDATION_REFUSED,
            ) from None
    message_id = _require_positive_int(arguments["telegram_message_id"], "telegram_message_id")
    chat_id = _require_non_negative_int(arguments["telegram_chat_id"], "telegram_chat_id")
    sender_id: int | None = None
    if arguments.get("sender_id") is not None:
        sender_id = _require_non_negative_int(arguments["sender_id"], "sender_id")
    # Structural private-DM policy: group/supergroup shapes and sender/chat
    # mismatches are refused before any persistence.
    telegram_boundary.validate_private_direct_chat(chat_id=chat_id, sender_id=sender_id)
    assert sender_id is not None
    source_context = _validated_capture_source_context(
        arguments,
        chat_id=chat_id,
        message_id=message_id,
        sender_id=sender_id,
    )
    ingress_digest, expected_attachment_hash = _validated_finance_ingress(
        arguments,
        capture_kind="receipt_image",
        chat_id=chat_id,
        message_id=message_id,
        sender_id=sender_id,
        source_context=source_context,
        update_id=update_id,
    )
    # The idempotency key must bind the durable Telegram message identity.
    _require_canonical_idempotency_key(
        request, canonical_capture_key(chat_id=chat_id, message_id=message_id)
    )
    handoff_filename = workspace_access.validate_handoff_filename(arguments["handoff_filename"])
    descriptor_content: bytes | None = None
    if arguments.get("handoff_descriptor_fd") is not None:
        descriptor_fd = _require_non_negative_int(
            arguments["handoff_descriptor_fd"], "handoff_descriptor_fd"
        )
        if descriptor_fd != 3:
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                "handoff_descriptor_fd must be the fixed inherited descriptor 3.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        expected_handoff_hash = _require_string(
            arguments.get("handoff_content_hash"), "handoff_content_hash", max_length=64
        )
        if len(expected_handoff_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_handoff_hash
        ):
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                "handoff_content_hash must be lowercase SHA-256 hex.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        descriptor_content, _descriptor_size = read_handoff_descriptor(descriptor_fd)
        if hashlib.sha256(descriptor_content).hexdigest() != expected_handoff_hash:
            raise errors.bridge_error(
                errors.HANDOFF_REFUSED,
                "Inherited handoff descriptor content hash does not match the request.",
                errors.EXIT_VALIDATION_REFUSED,
            )
    elif arguments.get("handoff_content_hash") is not None:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "handoff_content_hash requires handoff_descriptor_fd.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    original_filename: str | None = None
    if arguments.get("original_filename") is not None:
        original_filename = _require_string(
            arguments["original_filename"], "original_filename", max_length=1_024
        )
    declared_mime_type: str | None = None
    if arguments.get("declared_mime_type") is not None:
        declared_mime_type = _require_string(
            arguments["declared_mime_type"], "declared_mime_type", max_length=255
        )
    try:
        validate_attachment_identity_metadata(original_filename, declared_mime_type)
    except InvalidAttachmentIdentityError as exc:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"Receipt attachment metadata is invalid: {exc}",
            errors.EXIT_VALIDATION_REFUSED,
        ) from exc
    caption = ""
    if arguments.get("caption") is not None:
        caption = _require_string(arguments["caption"], "caption", max_length=_MAX_CAPTION_LENGTH)

    assert request.idempotency_key is not None
    identities = identity.capture_identities(request.idempotency_key)
    derived_intake_key = f"raw-intake:telegram:{chat_id}:{message_id}"

    workspace, conn = _open_context(arguments, deadline)
    try:
        _require_durable_capture_connection(conn)
        deadline.check("receipt capture replay inspection")
        existing = get_raw_intake_record_by_idempotency_key(conn, derived_intake_key)
        if existing is not None and existing["public_id"] != identities["raw_intake_public_id"]:
            raise errors.bridge_error(
                errors.IDEMPOTENCY_CONFLICT,
                "Capture idempotency key is already bound to a different intake identity.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        is_replay = existing is not None
        if existing is not None:
            _require_replay_caption_matches(existing, caption)
            _require_replay_source_context(conn, existing, source_context)

        deadline.check("receipt handoff publication")
        handoff_dir = workspace_access.ensure_handoff_directory(workspace)
        handoff_path = handoff_dir / handoff_filename
        preloaded_content: bytes | None = None
        if existing is None:
            # Read the handoff exactly once before any persistence: the same
            # bytes bind the intake fingerprint and feed durable publication,
            # so the content identity is established before the crash window
            # between intake persistence and evidence persistence opens.
            deadline.check("receipt handoff evidence read")
            if descriptor_content is None:
                content, _observed_size = read_handoff_file(handoff_path)
            else:
                content = descriptor_content
            preloaded_content = content
            validate_receipt_handoff_metadata(
                content,
                original_filename=original_filename,
                declared_mime_type=declared_mime_type,
            )
            deadline.check("receipt intake persistence")
            raw_input = caption or "[telegram receipt image]"
            source_metadata: dict[str, Any] = {
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "source_message_id": str(message_id),
                "idempotency_key": derived_intake_key,
                "handoff_filename": handoff_filename,
                "attachment_hash": hashlib.sha256(content).hexdigest(),
            }
            if update_id is not None:
                source_metadata["telegram_update_id"] = str(update_id)
            if message_date is not None:
                source_metadata["telegram_message_date"] = str(message_date)
                assert message_received_at is not None
                source_metadata["source_received_at"] = message_received_at
            if sender_id is not None:
                source_metadata["sender_id"] = str(sender_id)
            try:
                with conn:
                    existing = create_raw_intake_record(
                        conn,
                        raw_input,
                        source_type=TELEGRAM_PHOTO_SOURCE_TYPE,
                        source_channel="telegram",
                        source_metadata=source_metadata,
                        public_id=identities["raw_intake_public_id"],
                    )
                    persistence_effect = _capture_context_effect(source_context)
                    if persistence_effect is not None:
                        persistence_effect(conn, existing)
            except RawIntakeIdempotencyConflictError as exc:
                raise errors.bridge_error(
                    errors.IDEMPOTENCY_CONFLICT,
                    "Capture idempotency key is already bound to different handoff content.",
                    errors.EXIT_AUTHORITY_REFUSED,
                ) from exc
            except sqlite3.IntegrityError as exc:
                # Concurrent first-capture race on the deterministic identity.
                # Fall back to the persisted winner and verify it carries the
                # exact identity and content this request bound.
                winner = get_raw_intake_record_by_idempotency_key(conn, derived_intake_key)
                if winner is None or winner["public_id"] != identities["raw_intake_public_id"]:
                    raise errors.bridge_error(
                        errors.IDEMPOTENCY_CONFLICT,
                        "Capture idempotency key is already bound to a different intake identity.",
                        errors.EXIT_AUTHORITY_REFUSED,
                    ) from exc
                _require_replay_caption_matches(winner, caption)
                preloaded_content = _require_replay_content_matches(
                    conn, winner, handoff_path, descriptor_content
                )
                existing = winner
                is_replay = True
                _require_replay_source_context(conn, winner, source_context)
        else:
            preloaded_content = _require_replay_content_matches(
                conn, existing, handoff_path, descriptor_content
            )

        assert existing is not None
        deadline.check("receipt handoff publication")

        def persist_job(transaction: sqlite3.Connection, evidence: dict[str, Any]) -> None:
            if (
                expected_attachment_hash is not None
                and evidence["content_hash"] != expected_attachment_hash
            ):
                raise CaptureJobConflictError("Finance ingress image hash differs from original")
            ensure_capture_job(
                transaction,
                intake_id=int(existing["id"]),
                capture_kind="receipt_image",
                attachment_evidence_id=int(evidence["id"]),
                ingress_identity_digest=ingress_digest,
            )

        try:
            handoff = publish_receipt_handoff(
                conn,
                workspace=workspace,
                handoff_path=handoff_path,
                attachment_evidence_public_id=identities["attachment_evidence_public_id"],
                raw_intake_id=int(existing["id"]),
                original_filename=original_filename,
                declared_mime_type=declared_mime_type,
                preloaded_content=preloaded_content,
                persistence_effect=persist_job,
            )
        except CaptureJobConflictError as exc:
            raise _capture_job_conflict(exc) from exc
        return {
            "capture_kind": "receipt_image",
            "intake_public_id": existing["public_id"],
            "attachment_evidence_public_id": identities["attachment_evidence_public_id"],
            "attachment_content_hash": handoff.content_hash,
            "observed_file_size": handoff.observed_file_size,
            "mime_type": handoff.mime_type,
            "durable_file_reused": handoff.durable_file_reused,
            "persistence_idempotent": handoff.persistence_idempotent,
            "final_transaction_created": False,
            "capture_job": get_capture_job(conn, intake_public_id=str(existing["public_id"])),
        }, is_replay or handoff.persistence_idempotent
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------


def handle_propose(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "intake_public_id"}),
    )
    intake_public_id = _require_string(
        request.arguments["intake_public_id"], "intake_public_id", max_length=200
    )
    # The idempotency key must bind the intake this propose authorizes.
    _require_canonical_idempotency_key(request, canonical_propose_key(intake_public_id))
    workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("propose intake lookup")
        intake = get_raw_intake_record_by_public_id(conn, intake_public_id)
        if intake is None:
            raise errors.bridge_error(
                errors.INTAKE_NOT_FOUND,
                "Intake public ID was not found in the staging database.",
                errors.EXIT_VALIDATION_REFUSED,
            )

        # Replay and text path: never create a duplicate proposal; reuse the
        # deterministic parser proposal already persisted at capture.
        if intake["parser_output_id"] is not None:
            proposal_row = ParserProposalRepository(conn).get(int(intake["parser_output_id"]))
            if proposal_row is None:
                raise errors.bridge_error(
                    errors.PROPOSAL_UNAVAILABLE,
                    "Intake parser pointer references a missing proposal.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            proposal = _fetch_proposal_by_public_id(conn, str(proposal_row["public_id"]))
            _payload, version, content_hash = _proposal_effective_state(conn, proposal)
            return {
                "intake_public_id": intake["public_id"],
                "proposal_public_id": proposal["public_id"],
                "proposal_version": version,
                "effective_content_hash": content_hash,
                "parse_status": proposal["parse_status"],
                "source_type": proposal["source_type"],
                "final_transaction_created": False,
            }, True

        if intake["source_type"] != TELEGRAM_PHOTO_SOURCE_TYPE:
            raise errors.bridge_error(
                errors.PROPOSAL_UNAVAILABLE,
                "Intake has no deterministic proposal and no bridge receipt lineage.",
                errors.EXIT_AUTHORITY_REFUSED,
            )

        if not intake["public_id"].startswith(BRIDGE_RAW_INTAKE_PREFIX):
            raise errors.bridge_error(
                errors.PROPOSAL_UNAVAILABLE,
                "Receipt intake was not captured through the bridge boundary.",
                errors.EXIT_AUTHORITY_REFUSED,
            )

        return _propose_receipt(conn, workspace, request, intake, deadline)
    finally:
        conn.close()


def _propose_receipt(
    conn: sqlite3.Connection,
    workspace: Path,
    request: BridgeRequest,
    intake: dict[str, Any],
    deadline: Deadline,
) -> HandlerResult:
    digest_suffix = intake["public_id"][len(BRIDGE_RAW_INTAKE_PREFIX) :]
    identities = {
        "extraction_public_id": f"rocr_bridge_{digest_suffix}",
        "proposal_public_id": f"prop_bridge_{digest_suffix}",
        "link_public_id": f"ropl_bridge_{digest_suffix}",
    }

    deadline.check("propose attachment lookup")
    source_row = get_telegram_source_evidence_for_raw_intake(conn, int(intake["id"]))
    if source_row is None or source_row["attachment_id"] is None:
        raise errors.bridge_error(
            errors.ATTACHMENT_EVIDENCE_NOT_FOUND,
            "Receipt intake has no persisted attachment evidence; run capture first.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    attachment_id = int(source_row["attachment_id"])

    deadline.check("propose OCR engine construction")
    engine = build_ocr_engine(workspace)

    deadline.check("propose OCR evidence extraction")
    try:
        extraction = extract_and_persist_receipt_ocr_evidence(
            conn,
            public_id=identities["extraction_public_id"],
            attachment_id=attachment_id,
            engine=engine,
        )
    except ReceiptOcrError as exc:
        raise errors.bridge_error(
            errors.OCR_EXTRACTION_FAILED,
            f"Receipt OCR evidence extraction failed: {type(exc).__name__}",
            errors.EXIT_AUTHORITY_REFUSED,
            retryable=False,
        ) from exc

    deadline.check("propose receipt proposal ingestion")
    try:
        ingestion = ingest_receipt_ocr_evidence_as_total_expense_proposal(
            conn,
            extraction_public_id=extraction.public_id,
            proposal_public_id=identities["proposal_public_id"],
            link_public_id=identities["link_public_id"],
        )
    except ReceiptOcrProposalError as exc:
        raise errors.bridge_error(
            errors.OCR_EXTRACTION_FAILED,
            f"Receipt OCR proposal ingestion failed: {type(exc).__name__}",
            errors.EXIT_AUTHORITY_REFUSED,
            retryable=False,
        ) from exc

    proposal = _fetch_proposal_by_public_id(conn, ingestion.proposal_public_id)
    _payload, version, content_hash = _proposal_effective_state(conn, proposal)
    return {
        "intake_public_id": intake["public_id"],
        "proposal_public_id": ingestion.proposal_public_id,
        "proposal_version": version,
        "effective_content_hash": content_hash,
        "parse_status": ingestion.parse_status,
        "source_type": proposal["source_type"],
        "ambiguity_indicators": list(ingestion.ambiguity_flags),
        "extraction_public_id": extraction.public_id,
        "extraction_idempotent": extraction.persistence_idempotent,
        "proposal_idempotent": ingestion.idempotent,
        "final_transaction_created": False,
    }, ingestion.idempotent and extraction.persistence_idempotent


def handle_process_capture_job(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    """Thin local processor adapter; model calls and reply delivery are absent."""
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "job_public_id"}),
    )
    job_public_id = _require_string(
        request.arguments["job_public_id"], "job_public_id", max_length=200
    )
    canonical_key = "fcp_" + identity.canonical_digest(
        "finance-process-capture-job-v1", job_public_id
    )
    _require_canonical_idempotency_key(request, canonical_key)
    workspace, conn = _open_context(request.arguments, deadline)
    try:
        job = get_capture_job(conn, public_id=job_public_id)
        if job is None:
            raise errors.bridge_error(
                errors.INTAKE_NOT_FOUND,
                "Capture job was not found.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        if job["status"] in {"awaiting_user", "needs_attention", "result_ready"}:
            return {"capture_job": job, "final_transaction_created": False}, True
        if (
            job["last_error"] == "ocr_timeout_retry_pending"
            and int(job["ocr_retry_not_before_ms"]) > time.time_ns() // 1_000_000
        ):
            return {"capture_job": job, "final_transaction_created": False}, True
        if (
            job["status"] == "processing"
            and job["proposal_public_id"] is not None
            and job["lease_owner"] is None
            and job["lease_expires_at"] is None
            and job["last_error"] != "ocr_timeout_retry_pending"
            and conn.execute(
                "SELECT 1 FROM parser_outputs WHERE public_id = ?",
                (job["proposal_public_id"],),
            ).fetchone()
            is not None
        ):
            # Local processing committed its proposal but a durable review
            # card has not yet been established. The next stage may resume
            # from this exact job without repeating OCR or proposal work.
            return {"capture_job": job, "final_transaction_created": False}, True
        engine = build_ocr_engine(workspace) if job["capture_kind"] == "receipt_image" else None
        deadline.check("capture processing claim")
        try:
            lease = claim_capture_job(
                conn,
                public_id=job_public_id,
                owner="fcp_"
                + identity.canonical_digest("finance-capture-worker-v1", request.request_id)[:40],
                duration_ms=PROCESS_CAPTURE_LEASE_MS,
            )
            result = process_claimed_capture_job(conn, lease=lease, engine=engine)
        except CaptureJobNotRunnableError as exc:
            raise errors.bridge_error(
                errors.PROPOSAL_UNAVAILABLE,
                str(exc),
                errors.EXIT_AUTHORITY_REFUSED,
                retryable=True,
            ) from exc
        except CaptureLeaseLostError as exc:
            raise errors.bridge_error(
                errors.PROPOSAL_UNAVAILABLE,
                "Capture lease changed; query the job before retrying.",
                errors.EXIT_AUTHORITY_REFUSED,
                retryable=True,
            ) from exc
        except CaptureProcessingConflictError as exc:
            raise errors.bridge_error(
                errors.PROPOSAL_UNAVAILABLE,
                str(exc),
                errors.EXIT_AUTHORITY_REFUSED,
                retryable=False,
            ) from exc
        return {"capture_job": result, "final_transaction_created": False}, False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# S5e AI fallback
# ---------------------------------------------------------------------------


def _map_ai_fallback_error(exc: AiFallbackServiceError) -> errors.BridgeError:
    code_map = {
        "INTAKE_NOT_FOUND": errors.INTAKE_NOT_FOUND,
        "PROPOSAL_NOT_FOUND": errors.PROPOSAL_NOT_FOUND,
        "AI_FALLBACK_NOT_FOUND": errors.AI_FALLBACK_NOT_FOUND,
        "AI_FALLBACK_NOT_ELIGIBLE": errors.AI_FALLBACK_NOT_ELIGIBLE,
        "AI_FALLBACK_CONFLICT": errors.AI_FALLBACK_CONFLICT,
        "AI_FALLBACK_ARGUMENTS_REFUSED": errors.AI_FALLBACK_ARGUMENTS_REFUSED,
        "AI_FALLBACK_POLICY_REFUSED": errors.AI_FALLBACK_POLICY_REFUSED,
        "AI_FALLBACK_VALIDATION_REFUSED": errors.AI_FALLBACK_VALIDATION_REFUSED,
        "AI_FALLBACK_INTERNAL": errors.AI_FALLBACK_INTERNAL,
        "DEADLINE_EXCEEDED": errors.DEADLINE_EXCEEDED,
        "AI_MODEL_COMPATIBILITY_ARGUMENTS_REFUSED": errors.AI_MODEL_COMPATIBILITY_ARGUMENTS_REFUSED,
        "AI_MODEL_COMPATIBILITY_POLICY_REFUSED": errors.AI_MODEL_COMPATIBILITY_POLICY_REFUSED,
        "AI_MODEL_COMPATIBILITY_FK_REFUSED": errors.AI_MODEL_COMPATIBILITY_FK_REFUSED,
        "AI_MODEL_COMPATIBILITY_CONFLICT": errors.AI_MODEL_COMPATIBILITY_CONFLICT,
        "AI_MODEL_COMPATIBILITY_INTERNAL": errors.AI_MODEL_COMPATIBILITY_INTERNAL,
        "AI_MODEL_CONFIG_REFUSED": errors.AI_MODEL_CONFIG_REFUSED,
        "AI_MODEL_CONFIG_NOT_ACCEPTED": errors.AI_MODEL_CONFIG_NOT_ACCEPTED,
        "AI_MODEL_EVAL_REFUSED": errors.AI_MODEL_EVAL_REFUSED,
    }
    code = code_map.get(exc.code, errors.AI_FALLBACK_INTERNAL)
    exit_code = (
        errors.EXIT_DEADLINE_EXCEEDED
        if code == errors.DEADLINE_EXCEEDED
        else errors.EXIT_INTERNAL
        if code == errors.AI_MODEL_COMPATIBILITY_INTERNAL
        else errors.EXIT_AUTHORITY_REFUSED
        if code
        in {
            errors.AI_FALLBACK_NOT_FOUND,
            errors.AI_FALLBACK_NOT_ELIGIBLE,
            errors.AI_FALLBACK_CONFLICT,
            errors.AI_FALLBACK_POLICY_REFUSED,
            errors.AI_MODEL_COMPATIBILITY_POLICY_REFUSED,
            errors.AI_MODEL_COMPATIBILITY_FK_REFUSED,
            errors.AI_MODEL_COMPATIBILITY_CONFLICT,
            errors.AI_MODEL_CONFIG_NOT_ACCEPTED,
        }
        else errors.EXIT_VALIDATION_REFUSED
    )
    return errors.bridge_error(
        code,
        str(exc),
        exit_code,
        details=exc.details,
        retryable=False,
    )


def _map_model_compatibility_error(exc: ModelCompatibilityError) -> errors.BridgeError:
    return _map_ai_fallback_error(AiFallbackServiceError(exc.code, str(exc), details=exc.details))


def handle_register_ai_model_compatibility_receipt_v2(
    request: BridgeRequest, deadline: Deadline
) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "config_projection", "harness_outcomes"}),
    )
    projection = request.arguments["config_projection"]
    outcomes = request.arguments["harness_outcomes"]
    if not isinstance(projection, dict) or not isinstance(outcomes, list):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Receipt registration requires object projection and array outcomes.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    try:
        projection = validate_config_projection(
            projection, repo_root=Path(__file__).resolve().parents[2]
        )
    except ModelCompatibilityError as exc:
        raise _map_model_compatibility_error(exc) from exc
    _require_canonical_idempotency_key(
        request, canonical_register_ai_model_receipt_v2_key(projection)
    )
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("AI model compatibility receipt registration")
        try:
            result = register_ai_model_compatibility_receipt_v2(
                conn,
                config_projection=projection,
                harness_outcomes=outcomes,
                repo_root=Path(__file__).resolve().parents[2],
            )
        except ModelCompatibilityError as exc:
            raise _map_model_compatibility_error(exc) from exc
        return result, bool(result["idempotent_replay"])
    finally:
        conn.close()


def handle_verify_ai_model_compatibility_case_v2(
    request: BridgeRequest, deadline: Deadline
) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"config_projection", "harness_outcome"}),
    )
    projection = request.arguments["config_projection"]
    outcome = request.arguments["harness_outcome"]
    if not isinstance(projection, dict) or not isinstance(outcome, dict):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Case verification requires object projection and object harness outcome.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    deadline.check("AI model compatibility case verification")
    try:
        verified = verify_harness_outcome(
            projection,
            outcome,
            repo_root=Path(__file__).resolve().parents[2],
        )
    except ModelCompatibilityError as exc:
        raise _map_model_compatibility_error(exc) from exc
    return {
        "case_id": verified["case_id"],
        "verified": True,
        "response_sha256": verified["response_sha256"],
    }, False


def handle_prepare_ai_fallback_v2(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "intake_public_id", "config_projection"}),
    )
    intake_public_id = _require_string(
        request.arguments["intake_public_id"], "intake_public_id", max_length=200
    )
    projection = request.arguments["config_projection"]
    if not isinstance(projection, dict):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "config_projection must be an object.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    _require_canonical_idempotency_key(
        request, canonical_prepare_ai_fallback_v2_key(intake_public_id)
    )
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("AI fallback v2 preparation")
        try:
            result = prepare_ai_fallback_v2(
                conn,
                intake_public_id=intake_public_id,
                config_projection=projection,
                repo_root=Path(__file__).resolve().parents[2],
            )
        except AiFallbackServiceError as exc:
            raise _map_ai_fallback_error(exc) from exc
        return result, result["claim_disposition"] == "do_not_claim"
    finally:
        conn.close()


def handle_claim_ai_fallback_invocation_v2(
    request: BridgeRequest, deadline: Deadline
) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "attempt_public_id"}),
    )
    attempt_public_id = _require_string(
        request.arguments["attempt_public_id"], "attempt_public_id", max_length=100
    )
    _require_canonical_idempotency_key(
        request, canonical_claim_ai_fallback_v2_key(attempt_public_id)
    )
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("AI fallback v2 invocation claim")
        try:
            result = claim_ai_fallback_invocation_v2(conn, attempt_public_id=attempt_public_id)
        except AiFallbackServiceError as exc:
            raise _map_ai_fallback_error(exc) from exc
        return result, result["invocation_disposition"] == "do_not_invoke"
    finally:
        conn.close()


_AI_RESULT_OPTIONAL_ARGUMENTS = frozenset(
    {
        "returned_provider",
        "returned_model",
        "returned_agent_id",
        "audit_caller_kind",
        "audit_caller_id",
        "audit_caller_name",
        "audit_purpose",
        "audit_session_key_sha256",
        "usage_input_tokens",
        "usage_output_tokens",
        "response_utf8_b64",
        "response_byte_count",
        "response_sha256",
        "response_code_unit_count",
        "response_utf16_sha256",
        "response_body_state",
        "metadata_field",
        "metadata_reason",
        "metadata_code_unit_count",
        "metadata_sha256",
        "failure_code",
    }
)


def handle_record_ai_fallback_result_v2(
    request: BridgeRequest, deadline: Deadline
) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "attempt_public_id", "transport_outcome"}),
        optional=_AI_RESULT_OPTIONAL_ARGUMENTS,
    )
    attempt_public_id = _require_string(
        request.arguments["attempt_public_id"], "attempt_public_id", max_length=100
    )
    transport_outcome = _require_string(
        request.arguments["transport_outcome"], "transport_outcome", max_length=64
    )
    _require_canonical_idempotency_key(
        request, canonical_record_ai_fallback_v2_key(attempt_public_id)
    )
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("AI fallback v2 result")
        try:
            result, replay = record_ai_fallback_result_v2(
                conn,
                attempt_public_id=attempt_public_id,
                transport_outcome=transport_outcome,
                arguments={
                    key: value
                    for key, value in request.arguments.items()
                    if key not in {"workspace_path", "attempt_public_id", "transport_outcome"}
                },
            )
        except AiFallbackServiceError as exc:
            raise _map_ai_fallback_error(exc) from exc
        return result, replay
    finally:
        conn.close()


def handle_get_ai_processing_status_v2(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "intake_public_id"}),
    )
    intake_public_id = _require_string(
        request.arguments["intake_public_id"], "intake_public_id", max_length=200
    )
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("AI processing status v2")
        try:
            result = get_ai_processing_status_v2(conn, intake_public_id=intake_public_id)
        except AiFallbackServiceError as exc:
            raise _map_ai_fallback_error(exc) from exc
        return result, False
    finally:
        conn.close()


def handle_prepare_ai_fallback(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "intake_public_id"}),
    )
    intake_public_id = _require_string(
        request.arguments["intake_public_id"], "intake_public_id", max_length=200
    )
    _require_canonical_idempotency_key(request, canonical_prepare_ai_fallback_key(intake_public_id))
    workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("AI fallback preparation")
        try:
            result = prepare_ai_fallback(
                conn,
                intake_public_id=intake_public_id,
                repo_root=Path(__file__).resolve().parents[2],
            )
        except AiFallbackServiceError as exc:
            raise _map_ai_fallback_error(exc) from exc
        return result, result["claim_disposition"] == "do_not_claim"
    finally:
        conn.close()


def handle_claim_ai_fallback_invocation(
    request: BridgeRequest, deadline: Deadline
) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "attempt_public_id"}),
    )
    attempt_public_id = _require_string(
        request.arguments["attempt_public_id"], "attempt_public_id", max_length=100
    )
    _require_canonical_idempotency_key(request, canonical_claim_ai_fallback_key(attempt_public_id))
    workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("AI fallback invocation claim")
        try:
            result = claim_ai_fallback_invocation(
                conn,
                attempt_public_id=attempt_public_id,
            )
        except AiFallbackServiceError as exc:
            raise _map_ai_fallback_error(exc) from exc
        return result, result["invocation_disposition"] == "do_not_invoke"
    finally:
        conn.close()


def handle_record_ai_fallback_result(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    required = frozenset({"workspace_path", "attempt_public_id", "transport_outcome"})
    _require_exact_arguments(
        request.arguments,
        required=required,
        optional=frozenset(
            {
                "returned_provider",
                "returned_model",
                "returned_agent_id",
                "audit_caller_kind",
                "audit_caller_id",
                "audit_caller_name",
                "audit_purpose",
                "audit_session_key_sha256",
                "usage_input_tokens",
                "usage_output_tokens",
                "response_utf8_b64",
                "response_byte_count",
                "response_sha256",
                "response_code_unit_count",
                "response_utf16_sha256",
                "response_body_state",
                "metadata_field",
                "metadata_reason",
                "metadata_code_unit_count",
                "metadata_sha256",
                "failure_code",
            }
        ),
    )
    attempt_public_id = _require_string(
        request.arguments["attempt_public_id"], "attempt_public_id", max_length=100
    )
    transport_outcome = _require_string(
        request.arguments["transport_outcome"], "transport_outcome", max_length=64
    )
    _require_canonical_idempotency_key(request, canonical_record_ai_fallback_key(attempt_public_id))
    workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("AI fallback result")
        try:
            result, replay = record_ai_fallback_result_with_disposition(
                conn,
                attempt_public_id=attempt_public_id,
                transport_outcome=transport_outcome,
                arguments={
                    key: value
                    for key, value in request.arguments.items()
                    if key not in {"workspace_path", "attempt_public_id", "transport_outcome"}
                },
            )
        except AiFallbackServiceError as exc:
            raise _map_ai_fallback_error(exc) from exc
        return result, replay
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_review
# ---------------------------------------------------------------------------


def _ambiguity_indicators(
    conn: sqlite3.Connection, proposal: dict[str, Any], payload: dict[str, Any]
) -> list[str]:
    try:
        return application_review.review_ambiguity_indicators(conn, proposal, payload)
    except application_review.ReviewError as exc:
        raise _map_review_error(exc) from exc


def _classification(payload: dict[str, Any]) -> tuple[str, bool]:
    return application_review.classify_payload(payload)


def handle_get_review(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset({"workspace_path", "proposal_public_id"}),
        optional=frozenset({"token_ttl_seconds"}),
    )
    proposal_public_id = _require_string(
        request.arguments["proposal_public_id"], "proposal_public_id", max_length=200
    )
    token_ttl = _DEFAULT_TOKEN_TTL_SECONDS
    if request.arguments.get("token_ttl_seconds") is not None:
        token_ttl = _require_positive_int(
            request.arguments["token_ttl_seconds"],
            "token_ttl_seconds",
            maximum=_MAX_TOKEN_TTL_SECONDS,
        )
        if token_ttl < _MIN_TOKEN_TTL_SECONDS:
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                "token_ttl_seconds is below the bounded minimum.",
                errors.EXIT_VALIDATION_REFUSED,
            )

    workspace, conn = _open_context(request.arguments, deadline)
    try:
        with application_review.review_snapshot(conn):
            deadline.check("review read")
            prepared = application_review.prepare_proposal_review(conn, proposal_public_id)
            proposal, version, content_hash = (
                prepared.proposal,
                prepared.version,
                prepared.content_hash,
            )
            callback_tokens_payload: dict[str, dict[str, object]] | None = None
            parse_status = str(proposal["parse_status"])
            if parse_status not in TERMINAL_STATUSES:
                deadline.check("callback token issuance")
                # get_review is strictly read-only: a missing or unsafe callback
                # key fails closed instead of creating or repairing the key.
                key = _load_callback_key(workspace)
                expiry = int(datetime.now(UTC).timestamp()) + token_ttl
                callback_tokens_payload = callback_tokens.issue_callback_tokens(
                    key,
                    proposal_public_id=proposal["public_id"],
                    version=version,
                    content_hash=content_hash,
                    expiry=expiry,
                )

            result = application_review.project_proposal_review(conn, prepared)
            if result["proposal_origin"] == "ai_fallback" and result["ambiguity_indicators"]:
                callback_tokens_payload = None
            result["callback_tokens"] = callback_tokens_payload
            return result, False
    except application_review.ReviewError as exc:
        raise _map_review_error(exc) from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Decision command shared verification
# ---------------------------------------------------------------------------


_DECISION_REQUIRED_FIELDS = frozenset(
    {
        "workspace_path",
        "proposal_public_id",
        "operator_actor_id",
        "proposal_version",
        "content_hash",
        "callback_token",
        "callback_expiry",
    }
)

_D1_DECISION_FIELDS = frozenset(
    {
        "d1_reference_public_id",
        "telegram_account_id",
        "telegram_conversation_id",
        "conversation_binding_id",
    }
)


def _validate_decision_arguments(request: BridgeRequest) -> dict[str, Any]:
    arguments = request.arguments
    proposal_public_id = _require_string(
        arguments["proposal_public_id"], "proposal_public_id", max_length=200
    )
    operator_actor_id = _require_string(
        arguments["operator_actor_id"], "operator_actor_id", max_length=_MAX_ACTOR_ID_LENGTH
    )
    proposal_version = _require_non_negative_int(
        arguments["proposal_version"], "proposal_version", maximum=1_000_000
    )
    content_hash = _require_content_hash(arguments["content_hash"])
    callback_token = arguments["callback_token"]
    if not callback_tokens.token_body_is_well_formed(callback_token):
        raise errors.bridge_error(
            errors.CALLBACK_TOKEN_INVALID,
            "callback_token is malformed.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    callback_expiry = _require_positive_int(
        arguments["callback_expiry"], "callback_expiry", maximum=2**40
    )
    validated = {
        "proposal_public_id": proposal_public_id,
        "operator_actor_id": operator_actor_id,
        "proposal_version": proposal_version,
        "content_hash": content_hash,
        "callback_token": str(callback_token),
        "callback_expiry": callback_expiry,
    }
    supplied_d1_fields = frozenset(arguments) & _D1_DECISION_FIELDS
    if supplied_d1_fields and supplied_d1_fields != _D1_DECISION_FIELDS:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "D1 decision authority fields must be supplied together.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if supplied_d1_fields:
        context = _require_telegram_human_context(arguments)
        reference_public_id = _require_string(
            arguments["d1_reference_public_id"],
            "d1_reference_public_id",
            max_length=38,
        )
        if len(reference_public_id) != 38 or not reference_public_id.startswith("haref_"):
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                "d1_reference_public_id is malformed.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        validated["d1_reference_public_id"] = reference_public_id
        validated["d1_context"] = context
    return validated


def _load_callback_key(workspace: Path) -> bytes:
    try:
        return runner_workspace.load_callback_signing_key(str(workspace / "runtime"))
    except CallbackKeyMissingError as exc:
        raise errors.bridge_error(
            errors.CALLBACK_KEY_MISSING,
            "Callback signing key is missing; issue a fresh review card.",
            errors.EXIT_AUTHORITY_REFUSED,
        ) from exc
    except RunnerWorkspaceError as exc:
        raise errors.bridge_error(
            errors.CALLBACK_KEY_UNSAFE,
            "Callback signing key failed the workspace safety verification.",
            errors.EXIT_AUTHORITY_REFUSED,
        ) from exc


def _authenticate_callback(
    workspace: Path,
    validated: dict[str, Any],
    *,
    action: str,
    deadline: Deadline,
    proposal_public_id: str,
    durable_version: int,
    durable_hash: str,
) -> None:
    """Authenticate one callback against an explicit bound state.

    Runs the expiry check, the version/content-hash binding comparison, and
    the HMAC verification.  Callers supply the state the token was issued
    against: the current durable state for fresh commands, or the persisted
    decision-time binding for authenticated replays.
    """
    deadline.check("callback key load")
    key = _load_callback_key(workspace)

    now = int(datetime.now(UTC).timestamp())
    if validated["callback_expiry"] <= now:
        raise errors.bridge_error(
            errors.CALLBACK_EXPIRED,
            "Callback token expiry has passed.",
            errors.EXIT_AUTHORITY_REFUSED,
        )

    if validated["proposal_version"] != durable_version:
        raise errors.bridge_error(
            errors.STALE_VERSION,
            "Callback is bound to a stale proposal version.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    if validated["content_hash"] != durable_hash:
        raise errors.bridge_error(
            errors.STALE_CONTENT_HASH,
            "Callback is bound to a stale proposal content hash.",
            errors.EXIT_AUTHORITY_REFUSED,
        )

    if not callback_tokens.verify_token(
        key,
        token=validated["callback_token"],
        proposal_public_id=proposal_public_id,
        version=durable_version,
        content_hash=durable_hash,
        action=action,
        expiry=validated["callback_expiry"],
    ):
        mismatched = callback_tokens.find_mismatched_action(
            key,
            token=validated["callback_token"],
            proposal_public_id=proposal_public_id,
            version=durable_version,
            content_hash=durable_hash,
            expected_action=action,
            expiry=validated["callback_expiry"],
        )
        if mismatched is not None:
            raise errors.bridge_error(
                errors.CALLBACK_WRONG_ACTION,
                f"Callback token was issued for action '{mismatched}', not '{action}'.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        raise errors.bridge_error(
            errors.CALLBACK_TOKEN_INVALID,
            "Callback token does not verify against the durable proposal state.",
            errors.EXIT_AUTHORITY_REFUSED,
        )


def _verify_callback_context(
    conn: sqlite3.Connection,
    workspace: Path,
    validated: dict[str, Any],
    *,
    action: str,
    deadline: Deadline,
    terminal_guard: bool = True,
) -> dict[str, Any]:
    """Run the stateless token verification chain and return the proposal row.

    ``terminal_guard=False`` is used by authenticated decision replays: a
    persisted decision inherently leaves the proposal terminal, and the
    replay still authenticates expiry, version/content-hash binding, and the
    HMAC token before returning the original result.
    """
    deadline.check("callback durable state read")
    proposal = _fetch_proposal_by_public_id(conn, validated["proposal_public_id"])
    _payload, durable_version, durable_hash = _proposal_effective_state(conn, proposal)
    if terminal_guard and str(proposal["parse_status"]) in TERMINAL_STATUSES:
        raise errors.bridge_error(
            errors.PROPOSAL_TERMINAL_STATE,
            "Proposal is in a terminal lifecycle state.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    _authenticate_callback(
        workspace,
        validated,
        action=action,
        deadline=deadline,
        proposal_public_id=str(proposal["public_id"]),
        durable_version=durable_version,
        durable_hash=durable_hash,
    )
    return proposal


# ---------------------------------------------------------------------------
# S5c durable direct-human action references
# ---------------------------------------------------------------------------


def _require_telegram_human_context(arguments: dict[str, Any]) -> human_actions.HumanActionContext:
    actor_id = _require_string(arguments["operator_actor_id"], "operator_actor_id", max_length=32)
    account_id = _require_string(
        arguments["telegram_account_id"], "telegram_account_id", max_length=200
    )
    conversation_id = _require_string(
        arguments["telegram_conversation_id"], "telegram_conversation_id", max_length=32
    )
    binding_id = _require_string(
        arguments["conversation_binding_id"], "conversation_binding_id", max_length=200
    )
    host_identifiers = (account_id, binding_id)
    if (
        not actor_id.isascii()
        or not actor_id.isdecimal()
        or actor_id.startswith("0")
        or actor_id != conversation_id
        or any(
            not identifier.isascii()
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in identifier)
            for identifier in host_identifiers
        )
    ):
        raise errors.bridge_error(
            errors.ACTOR_MISMATCH,
            "Direct-human action requires one canonical private Telegram actor/conversation.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    return human_actions.HumanActionContext(
        actor_id=actor_id,
        account_id=account_id,
        conversation_id=conversation_id,
        binding_id=binding_id,
    )


def _raise_human_action_error(exc: human_actions.HumanActionReferenceError) -> None:
    mapping = {
        "proposal_missing": (errors.PROPOSAL_NOT_FOUND, errors.EXIT_VALIDATION_REFUSED),
        "proposal_terminal": (errors.PROPOSAL_TERMINAL_STATE, errors.EXIT_AUTHORITY_REFUSED),
        "reference_expired": (errors.CALLBACK_EXPIRED, errors.EXIT_AUTHORITY_REFUSED),
        "reference_expiring": (errors.CALLBACK_EXPIRED, errors.EXIT_AUTHORITY_REFUSED),
        "reference_consumed": (errors.CALLBACK_EXPIRED, errors.EXIT_AUTHORITY_REFUSED),
        "wrong_action": (errors.CALLBACK_WRONG_ACTION, errors.EXIT_AUTHORITY_REFUSED),
        "stale_version": (errors.STALE_VERSION, errors.EXIT_AUTHORITY_REFUSED),
        "stale_content_hash": (errors.STALE_CONTENT_HASH, errors.EXIT_AUTHORITY_REFUSED),
        "actor_or_context_mismatch": (errors.ACTOR_MISMATCH, errors.EXIT_AUTHORITY_REFUSED),
        "reference_replayed": (
            errors.HUMAN_ACTION_REFERENCE_REPLAYED,
            errors.EXIT_AUTHORITY_REFUSED,
        ),
        "reference_invalid": (
            errors.HUMAN_ACTION_REFERENCE_INVALID,
            errors.EXIT_AUTHORITY_REFUSED,
        ),
        "reference_integrity": (
            errors.HUMAN_ACTION_REFERENCE_INVALID,
            errors.EXIT_AUTHORITY_REFUSED,
        ),
        "issuance_conflict": (errors.IDEMPOTENCY_CONFLICT, errors.EXIT_AUTHORITY_REFUSED),
        "transaction_conflict": (errors.IDEMPOTENCY_CONFLICT, errors.EXIT_AUTHORITY_REFUSED),
    }
    code, exit_code = mapping.get(
        exc.reason, (errors.HUMAN_ACTION_REFERENCE_INVALID, errors.EXIT_AUTHORITY_REFUSED)
    )
    raise errors.bridge_error(
        code,
        "Durable direct-human action reference was refused.",
        exit_code,
    ) from exc


def _raise_posting_authority_error(exc: Exception) -> None:
    raise errors.bridge_error(
        errors.FINALIZATION_REFUSED,
        "D2 posting authority was refused.",
        errors.EXIT_AUTHORITY_REFUSED,
    ) from exc


def _raise_posting_sqlite_error(exc: sqlite3.OperationalError) -> None:
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        raise errors.bridge_error(
            errors.FINALIZATION_LOCKED,
            "D2 posting is temporarily locked; query status before resuming.",
            errors.EXIT_AUTHORITY_REFUSED,
            retryable=True,
        ) from exc
    raise exc


def _posting_status_payload(status: Any) -> dict[str, Any]:
    result = asdict(status)
    result["final_transaction_created"] = (
        status.state == "finalized" and status.transaction_public_id is not None
    )
    return result


def handle_prepare_posting_review(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset(
            {
                "workspace_path",
                "operator_actor_id",
                "telegram_account_id",
                "telegram_conversation_id",
                "conversation_binding_id",
            }
        ),
        optional=frozenset(
            {"card_generation_public_id", "proposal_public_id", "admitted_source_message_id"}
        ),
    )
    has_card = "card_generation_public_id" in request.arguments
    has_initial = (
        "proposal_public_id" in request.arguments
        or "admitted_source_message_id" in request.arguments
    )
    if has_card == has_initial or (
        has_initial
        and not {
            "proposal_public_id",
            "admitted_source_message_id",
        }.issubset(request.arguments)
    ):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "prepare_posting_review requires exactly one card or initial proposal source.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    card_generation_public_id = (
        _require_card_generation_public_id(request.arguments["card_generation_public_id"])
        if has_card
        else None
    )
    proposal_public_id = (
        _require_string(
            request.arguments["proposal_public_id"], "proposal_public_id", max_length=80
        )
        if has_initial
        else None
    )
    admitted_source_message_id = (
        _require_string(
            request.arguments["admitted_source_message_id"],
            "admitted_source_message_id",
            max_length=32,
        )
        if has_initial
        else None
    )
    context = _require_telegram_human_context(request.arguments)
    canonical_key = (
        canonical_prepare_posting_review_key(card_generation_public_id)
        if card_generation_public_id is not None
        else canonical_prepare_initial_posting_review_key(
            proposal_public_id or "", admitted_source_message_id or ""
        )
    )
    _require_canonical_idempotency_key(request, canonical_key)
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        from finance_core import posting_authority

        deadline.check("D2 posting review preparation")
        try:
            prepared = posting_authority.prepare_posting_review(
                conn,
                review_idempotency_key=request.idempotency_key or "",
                card_generation_public_id=card_generation_public_id,
                proposal_public_id=proposal_public_id,
                admitted_source_message_id=admitted_source_message_id,
                context=context,
            )
        except posting_authority.PostingAuthorityError as exc:
            _raise_posting_authority_error(exc)
        except sqlite3.OperationalError as exc:
            _raise_posting_sqlite_error(exc)
        return {
            "review_public_id": prepared.review_public_id,
            "card_generation_public_id": prepared.card_generation_public_id,
            "initial_card_public_id": prepared.initial_card_public_id,
            "proposal_public_id": prepared.proposal_public_id,
            "proposal_version": prepared.proposal_version,
            "proposal_content_hash": prepared.proposal_content_hash,
            "posting_path": prepared.posting_path,
            "visible_projection": dict(prepared.visible_projection),
            "visible_projection_hash": prepared.visible_projection_hash,
            "presentation_text": prepared.presentation_text,
            "expires_at": prepared.expires_at,
            "final_transaction_created": False,
        }, prepared.idempotent
    finally:
        conn.close()


def handle_issue_posting_review_actions(
    request: BridgeRequest, deadline: Deadline
) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset(
            {
                "workspace_path",
                "posting_review_public_id",
                "operator_actor_id",
                "telegram_account_id",
                "telegram_conversation_id",
                "conversation_binding_id",
            }
        ),
    )
    review_public_id = _require_string(
        request.arguments["posting_review_public_id"],
        "posting_review_public_id",
        max_length=64,
    )
    context = _require_telegram_human_context(request.arguments)
    _require_canonical_idempotency_key(
        request, canonical_issue_posting_review_actions_key(review_public_id)
    )
    workspace, conn = _open_context(request.arguments, deadline)
    try:
        from finance_core import posting_authority

        key = _load_callback_key(workspace)
        deadline.check("D2 Confirm action issuance")
        try:
            manifest = posting_authority.begin_posting_review_delivery(
                conn,
                review_public_id=review_public_id,
                key=key,
                context=context,
            )
        except posting_authority.PostingAuthorityError as exc:
            _raise_posting_authority_error(exc)
        except human_actions.HumanActionReferenceError as exc:
            _raise_human_action_error(exc)
        except sqlite3.OperationalError as exc:
            _raise_posting_sqlite_error(exc)
        return {
            "posting_review_public_id": review_public_id,
            "delivery_attempt_public_id": manifest.delivery_attempt_public_id,
            "delivery_manifest_version": manifest.version,
            "text": manifest.text,
            "controls": [
                {
                    "action": control.action,
                    "label": control.label,
                    "row_index": control.row_index,
                    "column_index": control.column_index,
                    "callback_value": control.callback_value,
                }
                for control in manifest.controls
            ],
            "finance_delivery_material_sha256": manifest.finance_delivery_material_sha256,
            "delivery_attempt_nonce": manifest.delivery_attempt_nonce,
            "final_transaction_created": False,
        }, manifest.idempotent
    finally:
        conn.close()


def handle_confirm_and_post(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset(
            {
                "workspace_path",
                "short_reference",
                "operator_actor_id",
                "telegram_account_id",
                "telegram_conversation_id",
                "conversation_binding_id",
                "callback_id",
                "callback_message_id",
            }
        ),
    )
    reference = _require_string(
        request.arguments["short_reference"], "short_reference", max_length=64
    )
    context = _require_telegram_human_context(request.arguments)
    callback_id = _require_string(request.arguments["callback_id"], "callback_id", max_length=200)
    callback_message_id = _require_positive_int(
        request.arguments["callback_message_id"], "callback_message_id", maximum=2**63 - 1
    )
    _require_canonical_idempotency_key(request, canonical_confirm_and_post_key(callback_id))
    workspace, conn = _open_context(request.arguments, deadline)
    try:
        from finance_core import posting_authority

        key = _load_callback_key(workspace)
        try:
            prior = posting_authority.get_status_by_reference(
                conn, reference=reference, context=context
            )
            deadline.check("D2 Confirm and post")
            status = posting_authority.confirm_and_post(
                conn,
                key=key,
                reference=reference,
                context=context,
                callback_id=callback_id,
                callback_message_id=callback_message_id,
            )
        except posting_authority.PostingAuthorityError as exc:
            _raise_posting_authority_error(exc)
        except human_actions.HumanActionReferenceError as exc:
            _raise_human_action_error(exc)
        except sqlite3.OperationalError as exc:
            _raise_posting_sqlite_error(exc)
        return _posting_status_payload(status), prior.state != "awaiting_confirmation"
    finally:
        conn.close()


def handle_resume_posting(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset(
            {
                "workspace_path",
                "attempt_public_id",
                "operator_actor_id",
                "telegram_account_id",
                "telegram_conversation_id",
                "conversation_binding_id",
            }
        ),
    )
    attempt_public_id = _require_string(
        request.arguments["attempt_public_id"], "attempt_public_id", max_length=64
    )
    context = _require_telegram_human_context(request.arguments)
    _require_canonical_idempotency_key(request, canonical_resume_posting_key(attempt_public_id))
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        from finance_core import posting_authority

        deadline.check("D2 posting resume")
        try:
            status = posting_authority.resume_posting(
                conn,
                attempt_public_id=attempt_public_id,
                context=context,
            )
        except posting_authority.PostingAuthorityError as exc:
            _raise_posting_authority_error(exc)
        except sqlite3.OperationalError as exc:
            _raise_posting_sqlite_error(exc)
        return _posting_status_payload(status), False
    finally:
        conn.close()


def handle_issue_human_actions(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset(
            {
                "workspace_path",
                "proposal_public_id",
                "operator_actor_id",
                "telegram_account_id",
                "telegram_conversation_id",
                "conversation_binding_id",
                "reference_batch_id",
                "token_ttl_seconds",
                "expected_proposal_version",
                "expected_content_hash",
            }
        ),
        optional=frozenset(
            {
                "minimum_remaining_seconds",
                "require_unconsumed_replay",
                "card_generation_public_id",
                "requested_actions",
            }
        ),
    )
    proposal_public_id = _require_string(
        request.arguments["proposal_public_id"], "proposal_public_id", max_length=200
    )
    context = _require_telegram_human_context(request.arguments)
    reference_batch_id = _require_string(
        request.arguments["reference_batch_id"], "reference_batch_id", max_length=64
    )
    card_generation_public_id = (
        None
        if "card_generation_public_id" not in request.arguments
        else _require_card_generation_public_id(request.arguments["card_generation_public_id"])
    )
    expected_batch_length = 64 if card_generation_public_id is not None else 32
    if len(reference_batch_id) != expected_batch_length or any(
        character not in "0123456789abcdef" for character in reference_batch_id
    ):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "reference_batch_id does not match the required lowercase hexadecimal identity.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    token_ttl = _require_positive_int(
        request.arguments["token_ttl_seconds"], "token_ttl_seconds", maximum=3600
    )
    expected_proposal_version = _require_non_negative_int(
        request.arguments["expected_proposal_version"],
        "expected_proposal_version",
    )
    expected_content_hash = _require_content_hash(request.arguments["expected_content_hash"])
    minimum_remaining_seconds = _require_non_negative_int(
        request.arguments.get("minimum_remaining_seconds", 0),
        "minimum_remaining_seconds",
        maximum=3599,
    )
    require_unconsumed_replay = request.arguments.get("require_unconsumed_replay", False)
    if not isinstance(require_unconsumed_replay, bool):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "require_unconsumed_replay must be a boolean.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if token_ttl < 60:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "token_ttl_seconds is below the bounded minimum.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if minimum_remaining_seconds >= token_ttl:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "minimum_remaining_seconds must be below token_ttl_seconds.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    _require_canonical_idempotency_key(
        request, canonical_human_action_issuance_key(reference_batch_id)
    )

    workspace, conn = _open_context(request.arguments, deadline)
    try:
        if card_generation_public_id is not None:
            authority = get_human_draft_action_authority(conn, card_generation_public_id)
            if authority is None or authority.action_issue_batch_id != reference_batch_id:
                raise errors.bridge_error(
                    errors.HUMAN_DRAFT_AUTHORITY_REFUSED,
                    "D1 action issuance identity does not match the durable card.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
        deadline.check("human action reference key load")
        proposal = _fetch_proposal_by_public_id(conn, proposal_public_id)
        payload, version, content_hash = _proposal_effective_state(conn, proposal)
        try:
            ai_lineage = verify_ai_fallback_child(
                conn,
                proposal,
                content_hash=content_hash,
                proposal_version=version,
                require_resolved=False,
            )
            if ai_lineage is None and requires_deterministic_intent_policy(proposal):
                verify_deterministic_intent_policy(proposal)
        except AiFallbackServiceError as exc:
            raise errors.bridge_error(
                errors.PROPOSAL_UNAVAILABLE,
                str(exc),
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc
        ambiguity_indicators = _ambiguity_indicators(conn, proposal, payload)
        if ai_lineage is not None:
            ambiguity_indicators = sorted(
                set(ambiguity_indicators) | set(ai_lineage["ambiguity_flags"])
            )
            if ai_lineage["requires_resolution"]:
                ambiguity_indicators = sorted(set(ambiguity_indicators) | {"low_confidence"})
        allowed_actions: tuple[str, ...] = (
            human_actions.REFERENCE_ACTIONS
            if ai_lineage is None or not ambiguity_indicators
            else (human_actions.callback_tokens.ACTION_REJECT,)
        )
        if card_generation_public_id is not None:
            assert authority is not None
            if authority.result_completeness is None:
                raise errors.bridge_error(
                    errors.HUMAN_DRAFT_NOT_FOUND,
                    "D1 card generation was not found.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            if authority.result_completeness != "complete":
                allowed_actions = (human_actions.callback_tokens.ACTION_REJECT,)
        requested_actions = request.arguments.get("requested_actions")
        if requested_actions is not None:
            if (
                not isinstance(requested_actions, list)
                or not requested_actions
                or len(requested_actions) > len(human_actions.REFERENCE_ACTIONS)
                or any(not isinstance(action, str) for action in requested_actions)
                or len(set(requested_actions)) != len(requested_actions)
                or any(action not in allowed_actions for action in requested_actions)
            ):
                raise errors.bridge_error(
                    errors.ARGUMENTS_REFUSED,
                    "requested_actions are not available for the current durable card.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            allowed_actions = tuple(requested_actions)
        key = _load_callback_key(workspace)
        persisted_issuance_keys = _persisted_human_action_issuance_keys(reference_batch_id, key=key)
        deadline.check("human action reference issuance")
        try:
            issued, replay = human_actions.issue_human_action_references(
                conn,
                key=key,
                issuance_idempotency_key=persisted_issuance_keys[0],
                fallback_issuance_idempotency_keys=persisted_issuance_keys[1:],
                proposal_public_id=proposal_public_id,
                expected_proposal_version=expected_proposal_version,
                expected_proposal_content_hash=expected_content_hash,
                context=context,
                ttl_seconds=token_ttl,
                minimum_remaining_seconds=minimum_remaining_seconds,
                require_unconsumed_replay=require_unconsumed_replay,
                allowed_actions=allowed_actions,
                card_generation_public_id=card_generation_public_id,
            )
        except human_actions.HumanActionReferenceError as exc:
            _raise_human_action_error(exc)
        return {
            "proposal_public_id": proposal_public_id,
            "proposal_version": expected_proposal_version,
            "content_hash": expected_content_hash,
            "actions": {
                item.action: {"reference": item.reference, "expiry": item.expires_at}
                for item in issued
            },
            **(
                {"card_generation_public_id": card_generation_public_id}
                if card_generation_public_id is not None
                else {}
            ),
            "final_transaction_created": False,
        }, replay
    finally:
        conn.close()


def handle_redeem_human_action(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=frozenset(
            {
                "workspace_path",
                "short_reference",
                "action",
                "operator_actor_id",
                "telegram_account_id",
                "telegram_conversation_id",
                "conversation_binding_id",
                "callback_id",
                "callback_message_id",
            }
        ),
    )
    reference = _require_string(
        request.arguments["short_reference"], "short_reference", max_length=64
    )
    action = _require_string(request.arguments["action"], "action", max_length=16)
    if action not in human_actions.REFERENCE_ACTIONS:
        raise errors.bridge_error(
            errors.CALLBACK_WRONG_ACTION,
            "Human action reference does not support the requested action.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    context = _require_telegram_human_context(request.arguments)
    callback_id = _require_string(request.arguments["callback_id"], "callback_id", max_length=200)
    callback_message_id = _require_positive_int(
        request.arguments["callback_message_id"], "callback_message_id", maximum=2**63 - 1
    )
    _require_canonical_idempotency_key(request, canonical_human_action_redemption_key(callback_id))

    workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("human action redemption key load")
        key = _load_callback_key(workspace)
        deadline.check("human action atomic redemption")
        try:
            redeemed = human_actions.redeem_human_action_reference(
                conn,
                key=key,
                reference=reference,
                action=action,
                context=context,
                callback_id=callback_id,
                callback_message_id=callback_message_id,
                action_validator=lambda locked, row, validated_action: (
                    _validate_redeemed_human_action(
                        locked,
                        row,
                        validated_action,
                        callback_message_id=callback_message_id,
                    )
                ),
                redemption_effect=lambda locked, row, validated_action, now: (
                    _begin_human_draft_for_redeemed_edit(
                        locked,
                        row,
                        validated_action,
                        reference=reference,
                        callback_id=callback_id,
                        callback_message_id=callback_message_id,
                        now_epoch=now,
                    )
                ),
            )
        except human_actions.HumanActionReferenceError as exc:
            _raise_human_action_error(exc)
        edit_session_public_id: str | None = None
        if redeemed.action == callback_tokens.ACTION_EDIT:
            reference_hash = hashlib.sha256(reference.encode("utf-8")).hexdigest()
            session = guided_edit.session_for_reference(conn, reference_hash)
            if (
                session is None
                or session["status"] != "active"
                or int(session["expires_at"]) <= int(datetime.now(UTC).timestamp())
            ):
                raise errors.bridge_error(
                    errors.PROPOSAL_TERMINAL_STATE,
                    "Guided edit session is no longer active.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            edit_session_public_id = str(session["session_public_id"])
        decision_key = (
            canonical_edit_key(
                proposal_public_id=redeemed.proposal_public_id,
                version=redeemed.proposal_version,
                content_hash=redeemed.proposal_content_hash,
            )
            if redeemed.action == callback_tokens.ACTION_EDIT
            else canonical_decision_key(
                action=redeemed.action, proposal_public_id=redeemed.proposal_public_id
            )
        )
        result: dict[str, Any] = {
            "action": redeemed.action,
            "proposal_public_id": redeemed.proposal_public_id,
            "operator_actor_id": redeemed.actor_id,
            "proposal_version": redeemed.proposal_version,
            "content_hash": redeemed.proposal_content_hash,
            "callback_token": redeemed.callback_token,
            "callback_expiry": redeemed.callback_expiry,
            "decision_idempotency_key": decision_key,
            **(
                {"guided_edit_session_public_id": edit_session_public_id}
                if edit_session_public_id is not None
                else {}
            ),
            "final_transaction_created": False,
        }
        if redeemed.d1_decision_binding is not None:
            result["d1_decision_binding"] = asdict(redeemed.d1_decision_binding)
        if redeemed.action == callback_tokens.ACTION_EDIT:
            draft_context = HumanDraftContext(
                authenticated_actor_id=context.actor_id,
                telegram_account_id=context.account_id,
                telegram_conversation_id=context.conversation_id,
                conversation_binding_id=context.binding_id,
            )
            try:
                lookup_identity = (
                    {
                        "card_generation_public_id": (
                            redeemed.d1_decision_binding.card_generation_public_id
                        )
                    }
                    if redeemed.d1_decision_binding is not None
                    else {"source_edit_reference_public_id": redeemed.reference_public_id}
                )
                draft = get_human_draft_card(
                    conn,
                    context=draft_context,
                    **lookup_identity,
                )
            except HumanDraftError as exc:
                active_card = find_active_human_draft_card_generation(
                    conn,
                    proposal_public_id=redeemed.proposal_public_id,
                    context=draft_context,
                )
                if active_card is None:
                    raise errors.bridge_error(
                        errors.LIFECYCLE_CONFLICT,
                        "Redeemed Edit reference has no authoritative D1 draft result.",
                        errors.EXIT_AUTHORITY_REFUSED,
                    ) from exc
                draft = get_human_draft_card(
                    conn,
                    context=draft_context,
                    card_generation_public_id=active_card,
                )
            result["human_draft_card"] = _human_draft_result_payload(conn, draft)
        return result, redeemed.idempotent_replay
    finally:
        conn.close()


def _validate_redeemed_human_action(
    conn: sqlite3.Connection,
    reference_row: dict[str, Any],
    action: str,
    *,
    callback_message_id: int,
) -> None:
    """Recheck AI action availability inside the redemption transaction."""
    proposal = ParserProposalRepository(conn).get(int(reference_row["parser_output_id"]))
    if proposal is None:
        raise human_actions.HumanActionReferenceError("proposal_missing")
    _payload, version, content_hash = _proposal_effective_state(conn, proposal)
    try:
        lineage = verify_ai_fallback_child(
            conn,
            proposal,
            content_hash=content_hash,
            proposal_version=version,
            require_resolved=action == callback_tokens.ACTION_CONFIRM,
        )
        if lineage is None and requires_deterministic_intent_policy(proposal):
            verify_deterministic_intent_policy(proposal)
    except AiFallbackServiceError as exc:
        raise human_actions.HumanActionReferenceError("action_unavailable") from exc
    if lineage is not None and action != callback_tokens.ACTION_REJECT:
        if lineage["requires_resolution"]:
            raise human_actions.HumanActionReferenceError("action_unavailable")
    if action == callback_tokens.ACTION_EDIT:
        try:
            guided_edit.begin_session_in_transaction(
                conn,
                reference_row,
                action,
                initial_message_id=callback_message_id,
            )
        except guided_edit.GuidedEditError as exc:
            raise human_actions.HumanActionReferenceError("action_unavailable") from exc


def _begin_human_draft_for_redeemed_edit(
    conn: sqlite3.Connection,
    reference_row: dict[str, Any],
    action: str,
    *,
    reference: str,
    callback_id: str,
    callback_message_id: int,
    now_epoch: int,
) -> None:
    if (
        action != callback_tokens.ACTION_EDIT
        or reference_row.get("card_generation_public_id") is not None
    ):
        return
    context = HumanDraftContext(
        authenticated_actor_id=str(reference_row["authenticated_actor_id"]),
        telegram_account_id=str(reference_row["channel_account_id"]),
        telegram_conversation_id=str(reference_row["channel_conversation_id"]),
        conversation_binding_id=str(reference_row["conversation_binding_id"]),
    )
    if active_human_draft_exists(
        conn,
        parser_output_id=int(reference_row["parser_output_id"]),
        context=context,
    ):
        return
    callback_hash = hashlib.sha256(callback_id.encode("utf-8")).hexdigest()
    start_parts = (
        "d1-human-draft-start-v1",
        str(reference_row["reference_public_id"]),
        callback_hash,
    )
    start_material = b"".join(
        len(encoded).to_bytes(4, "big") + encoded
        for encoded in (part.encode("utf-8") for part in start_parts)
    )
    start_public_id = f"d1start_{hashlib.sha256(start_material).hexdigest()[:32]}"
    try:
        begin_human_draft_in_transaction(
            conn,
            locked_edit_reference_row=reference_row,
            source_edit_reference_id=int(reference_row["id"]),
            reference_public_id=str(reference_row["reference_public_id"]),
            reference_integrity_material=reference.encode("utf-8"),
            callback_message_id=callback_message_id,
            redemption_public_id=start_public_id,
            redemption_material_hash=callback_hash,
            now_epoch=now_epoch,
        )
    except HumanDraftError as exc:
        raise human_actions.HumanActionReferenceError("action_unavailable") from exc


def _human_draft_result_payload(
    conn: sqlite3.Connection, result: HumanDraftResult
) -> dict[str, Any]:
    now_epoch = int(datetime.now(UTC).timestamp())
    presentation = get_human_draft_presentation(
        conn,
        result=result,
        now_epoch=now_epoch,
    )
    return {
        "draft_public_id": result.draft_public_id,
        "draft_version": result.draft_version,
        "draft_content_hash": result.draft_content_hash,
        "completeness": result.completeness,
        "reason_contributors": [asdict(item) for item in result.reason_contributors],
        "unresolved_flags": list(result.unresolved_flags),
        "human_reply_evidence_public_id": result.human_reply_evidence_public_id,
        "delivery_state": result.delivery_state,
        "delivery_state_hash": result.delivery_state_hash,
        "delivery_attempts": [dict(item) for item in result.delivery_attempts],
        "delivery_outcomes": [dict(item) for item in result.delivery_outcomes],
        "action_issue_batch_id": result.action_issue_batch_id,
        "operation_outcome": result.operation_outcome,
        "refusal_code": result.refusal_code,
        "idempotent_replay": result.idempotent_replay,
        "action_issuance_state": result.action_issuance_state,
        "proposal_public_id": result.proposal_public_id,
        "proposal_version": result.proposal_version,
        "proposal_content_hash": result.proposal_content_hash,
        "card_generation_public_id": result.card_generation_public_id,
        "current_card_generation_public_id": result.current_card_generation_public_id,
        "original_operation_or_start_public_id": (
            presentation.original_operation_or_start_public_id
        ),
        "field_values": dict(result.field_values),
        "decision_target_proposal_public_id": result.decision_target_proposal_public_id,
        "decision_target_proposal_version": result.decision_target_proposal_version,
        "decision_target_proposal_content_hash": result.decision_target_proposal_content_hash,
        "confirm_available": (
            presentation.active
            and result.completeness == "complete"
            and result.proposal_public_id is not None
            and result.card_generation_public_id == result.current_card_generation_public_id
        ),
        "reject_available": presentation.active,
        "final_transaction_created": False,
    }


# ---------------------------------------------------------------------------
# confirm / reject
# ---------------------------------------------------------------------------


def handle_confirm(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    return _handle_decision(request, deadline, action=callback_tokens.ACTION_CONFIRM)


def handle_reject(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    return _handle_decision(request, deadline, action=callback_tokens.ACTION_REJECT)


def _handle_decision(request: BridgeRequest, deadline: Deadline, *, action: str) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=_DECISION_REQUIRED_FIELDS,
        optional=_D1_DECISION_FIELDS,
    )
    validated = _validate_decision_arguments(request)
    # The idempotency key must bind the proposal and action this command
    # authorizes; cross-proposal key reuse is refused before any lookup.
    _require_canonical_idempotency_key(
        request,
        canonical_decision_key(action=action, proposal_public_id=validated["proposal_public_id"]),
    )

    workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("decision replay reconstruction")
        proposal = _fetch_proposal_by_public_id(conn, validated["proposal_public_id"])
        d1_decision_binding = None
        if "d1_reference_public_id" in validated:
            try:
                d1_decision_binding = human_actions.load_redeemed_human_action_binding(
                    conn,
                    reference_public_id=validated["d1_reference_public_id"],
                    action=action,
                    proposal_public_id=validated["proposal_public_id"],
                    proposal_version=validated["proposal_version"],
                    proposal_content_hash=validated["content_hash"],
                    context=validated["d1_context"],
                )
            except human_actions.HumanActionReferenceError as exc:
                raise errors.bridge_error(
                    errors.HUMAN_DRAFT_AUTHORITY_REFUSED,
                    f"D1 decision authority refused: {exc.reason}.",
                    errors.EXIT_AUTHORITY_REFUSED,
                ) from exc
        existing = ParserAuthorizationRepository(conn).get_for_proposal(int(proposal["id"]))
        decision = "confirmed" if action == callback_tokens.ACTION_CONFIRM else "rejected"
        if existing is not None:
            assert request.idempotency_key is not None
            if existing["confirmation_public_id"] != identity.confirmation_public_id(
                request.idempotency_key
            ):
                raise errors.bridge_error(
                    errors.LIFECYCLE_CONFLICT,
                    "Proposal already carries a persisted decision under a different "
                    "idempotency key.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            if (
                existing["confirmation_state"] != decision
                or existing["authenticated_actor_id"] != validated["operator_actor_id"]
            ):
                raise errors.bridge_error(
                    errors.IDEMPOTENCY_CONFLICT,
                    "Decision idempotency key is already bound to different decision material.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            if existing["confirmation_channel"] != BRIDGE_CONFIRMATION_CHANNEL:
                raise errors.bridge_error(
                    errors.LIFECYCLE_CONFLICT,
                    "Proposal already carries a decision from a different channel.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            # A replay never skips authentication: expiry, durable
            # version/content-hash binding, and the HMAC token must all verify
            # before the original persisted result is returned.
            _verify_callback_context(
                conn,
                workspace,
                validated,
                action=action,
                deadline=deadline,
                terminal_guard=False,
            )
        if existing is None:
            proposal = _verify_callback_context(
                conn, workspace, validated, action=action, deadline=deadline
            )

        deadline.check("decision persistence")
        assert request.idempotency_key is not None
        confirmation_public_id = identity.confirmation_public_id(request.idempotency_key)
        boundary = confirm_proposal if decision == "confirmed" else reject_proposal
        try:
            result = boundary(
                conn,
                int(proposal["id"]),
                actor=validated["operator_actor_id"],
                reason=None,
                confirmation_public_id=confirmation_public_id,
                confirmation_channel=BRIDGE_CONFIRMATION_CHANNEL,
                expected_content_hash=validated["content_hash"],
                expected_version=validated["proposal_version"],
                d1_decision_binding=d1_decision_binding,
            )
        except StaleProposalDecisionStateError as exc:
            # Authoritative atomic guard: the proposal changed between token
            # verification and the decision transaction; nothing was written.
            raise errors.bridge_error(
                errors.STALE_CONTENT_HASH,
                "Authoritative decision boundary observed newer proposal content; "
                "the decision was refused and nothing was persisted.",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc
        except ParserConfirmationError as exc:
            raise errors.bridge_error(
                errors.LIFECYCLE_CONFLICT,
                f"Authoritative decision boundary refused: {exc}",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc

        return {
            "decision": decision,
            "confirmation_id": result["confirmation_id"],
            "proposal_public_id": proposal["public_id"],
            "to_status": result["to_status"],
            "final_transaction_created": bool(result["final_transaction_created"]),
        }, bool(result["idempotent"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


def handle_edit(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments, required=_DECISION_REQUIRED_FIELDS | frozenset({"field_updates"})
    )
    validated = _validate_decision_arguments(request)
    # The idempotency key must bind the proposal and the exact pre-edit
    # state this edit authorizes, so consecutive edit versions carry
    # distinct identities while identical redeliveries replay.
    _require_canonical_idempotency_key(
        request,
        canonical_edit_key(
            proposal_public_id=validated["proposal_public_id"],
            version=validated["proposal_version"],
            content_hash=validated["content_hash"],
        ),
    )
    field_updates = request.arguments["field_updates"]
    if not isinstance(field_updates, dict) or not field_updates:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "field_updates must be a non-empty object.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    unknown_fields = frozenset(field_updates) - _ALLOWED_EDIT_FIELDS
    if unknown_fields:
        raise errors.bridge_error(
            errors.UNSUPPORTED_EDIT,
            f"Unsupported edit fields: {sorted(unknown_fields)}. No authoritative boundary exists.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    for field_name, value in field_updates.items():
        if isinstance(value, bool):
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                f"field_updates.{field_name} must not be a boolean.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        if isinstance(value, str) and len(value) > _MAX_FIELD_VALUE_LENGTH:
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                f"field_updates.{field_name} exceeds the bounded length limit.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        if value is None or (isinstance(value, str) and not value.strip()):
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                f"field_updates.{field_name} must carry a non-empty value.",
                errors.EXIT_VALIDATION_REFUSED,
            )

    monetary = _MONETARY_FIELDS & frozenset(field_updates)

    workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("edit replay reconstruction")
        proposal = _fetch_proposal_by_public_id(conn, validated["proposal_public_id"])
        assert request.idempotency_key is not None
        replay = _edit_replay_result(
            conn, workspace, request, proposal, validated, deadline, monetary=monetary
        )
        if replay is not None:
            return replay

        proposal = _verify_callback_context(
            conn, workspace, validated, action=callback_tokens.ACTION_EDIT, deadline=deadline
        )
        _payload, _version, current_hash = _proposal_effective_state(conn, proposal)

        deadline.check("edit persistence")
        if monetary:
            return _edit_receipt_monetary(conn, request, proposal, field_updates, current_hash)
        return _edit_completion(conn, request, proposal, field_updates, current_hash)
    finally:
        conn.close()


def _require_replay_decision_context_matches(
    *,
    persisted_actor: object,
    persisted_channel: object,
    persisted_base_hash: object,
    validated: dict[str, Any],
) -> None:
    """Mirror the confirm/reject replay conflict semantics for edit replays.

    Same-key edits carrying different decision material (actor, channel, or
    expected content) fail closed as a deterministic idempotency conflict.
    """
    if (
        persisted_actor != validated["operator_actor_id"]
        or persisted_channel != BRIDGE_CONFIRMATION_CHANNEL
        or persisted_base_hash != validated["content_hash"]
    ):
        raise errors.bridge_error(
            errors.IDEMPOTENCY_CONFLICT,
            "Edit idempotency key is already bound to a conflicting decision context.",
            errors.EXIT_AUTHORITY_REFUSED,
        )


def _edit_replay_result(
    conn: sqlite3.Connection,
    workspace: Path,
    request: BridgeRequest,
    proposal: dict[str, Any],
    validated: dict[str, Any],
    deadline: Deadline,
    *,
    monetary: frozenset[str],
) -> HandlerResult | None:
    """Reconstruct a previously persisted identical edit, if any.

    A replay is only honored after the full callback authentication chain
    (expiry, durable version/content-hash binding, HMAC token) verifies,
    exactly like the original command.
    """
    assert request.idempotency_key is not None
    supplied_updates = dict(request.arguments["field_updates"])
    if monetary:
        correction_public_id = identity.correction_public_id(request.idempotency_key)
        row = get_receipt_proposal_revision_by_correction_id(conn, correction_public_id)
        if row is None:
            return None
        if int(row["superseded_parser_output_id"]) != int(proposal["id"]):
            raise errors.bridge_error(
                errors.IDEMPOTENCY_CONFLICT,
                "Edit idempotency key is already bound to a different proposal.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        _require_replay_decision_context_matches(
            persisted_actor=row["authenticated_actor_id"],
            persisted_channel=row["correction_channel"],
            persisted_base_hash=row["superseded_content_hash"],
            validated=validated,
        )
        _require_identical_updates(
            row["field_updates_json"],
            _canonical_supersession_updates(conn, proposal, supplied_updates),
        )
        replacement = ParserProposalRepository(conn).get(int(row["replacement_parser_output_id"]))
        if replacement is None:
            raise errors.bridge_error(
                errors.LIFECYCLE_CONFLICT,
                "Persisted edit replacement proposal is missing.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        _verify_callback_context(
            conn,
            workspace,
            validated,
            action=callback_tokens.ACTION_EDIT,
            deadline=deadline,
            terminal_guard=False,
        )
        # Mirror the supersession boundary replay contract: the replacement
        # status is the constant creation-time status and never reflects
        # later confirmation, completion, or supersession of the replacement.
        return {
            "edit_kind": "receipt_monetary_correction",
            "superseded_proposal_public_id": proposal["public_id"],
            "proposal_public_id": replacement["public_id"],
            "proposal_version": 0,
            "effective_content_hash": row["replacement_content_hash"],
            "parse_status": PARSED_PENDING_CONFIRMATION,
            "final_transaction_created": False,
        }, True

    completion_public_id = identity.completion_public_id(request.idempotency_key)
    row = get_completion_by_public_id(conn, completion_public_id)
    if row is None:
        return None
    if int(row["parser_output_id"]) != int(proposal["id"]):
        raise errors.bridge_error(
            errors.IDEMPOTENCY_CONFLICT,
            "Edit idempotency key is already bound to a different proposal.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    _require_replay_decision_context_matches(
        persisted_actor=row["authenticated_actor_id"],
        persisted_channel=row["completion_channel"],
        persisted_base_hash=row["base_content_hash"],
        validated=validated,
    )
    _require_identical_updates(
        row["field_updates_json"], _canonical_completion_updates(supplied_updates)
    )
    # A double-clicked edit carries the pre-edit token: authenticate against
    # the persisted base state the token was issued for, not the post-edit
    # durable state.
    _authenticate_callback(
        workspace,
        validated,
        action=callback_tokens.ACTION_EDIT,
        deadline=deadline,
        proposal_public_id=str(proposal["public_id"]),
        durable_version=int(row["version_number"]) - 1,
        durable_hash=str(row["base_content_hash"]),
    )
    return {
        "edit_kind": "completion",
        "proposal_public_id": proposal["public_id"],
        "proposal_version": int(row["version_number"]),
        "effective_content_hash": row["completed_content_hash"],
        "parse_status": str(proposal["parse_status"]),
        "final_transaction_created": False,
    }, True


def _require_identical_updates(persisted_json: object, canonical_supplied: dict[str, Any]) -> None:
    """Fail closed when a replayed edit carries different canonical material."""
    import json as _json

    try:
        persisted = _json.loads(str(persisted_json)) if persisted_json is not None else None
    except _json.JSONDecodeError:
        persisted = None
    if not isinstance(persisted, dict):
        raise errors.bridge_error(
            errors.LIFECYCLE_CONFLICT,
            "Persisted edit replay material is unreadable.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    canonical_persisted = _json.dumps(persisted, sort_keys=True, separators=(",", ":"), default=str)
    canonical_replay = _json.dumps(
        canonical_supplied, sort_keys=True, separators=(",", ":"), default=str
    )
    if canonical_persisted != canonical_replay:
        raise errors.bridge_error(
            errors.IDEMPOTENCY_CONFLICT,
            "Edit idempotency key is already bound to different field updates.",
            errors.EXIT_AUTHORITY_REFUSED,
        )


def _canonical_completion_updates(supplied: dict[str, Any]) -> dict[str, Any]:
    """Mirror the completion boundary canonicalization for replay comparison."""
    canonical: dict[str, Any] = {}
    if "transaction_date" in supplied:
        canonical["transaction_date"] = supplied["transaction_date"]
    for field in ("merchant", "description", "category"):
        if field in supplied and isinstance(supplied[field], str):
            canonical[field] = supplied[field].strip()
    return canonical


def _canonical_supersession_updates(
    conn: sqlite3.Connection, proposal: dict[str, Any], supplied: dict[str, Any]
) -> dict[str, Any]:
    """Mirror the supersession boundary canonicalization for replay comparison."""
    from finance_core.money import MoneyValidationError, normalize_currency
    from finance_core.parser_proposals.content_hash import canonicalize_proposal_money

    payload, _version, _hash = _proposal_effective_state(conn, proposal)
    amount_value = supplied["amount"] if "amount" in supplied else payload.get("amount")
    currency_value = supplied["currency"] if "currency" in supplied else payload.get("currency")
    canonical_amount: str | None = None
    canonical_currency: str | None = None
    try:
        if isinstance(currency_value, str):
            canonical_currency = normalize_currency(currency_value)
        if canonical_currency is not None:
            canonical_amount = canonicalize_proposal_money(amount_value, canonical_currency)
    except MoneyValidationError:
        canonical_amount = None
        canonical_currency = None

    canonical: dict[str, Any] = {}
    if "amount" in supplied:
        canonical["amount"] = canonical_amount
    if "currency" in supplied:
        canonical["currency"] = canonical_currency
    if "transaction_date" in supplied:
        canonical["transaction_date"] = supplied["transaction_date"]
    for field in ("merchant", "description", "category"):
        if field in supplied and isinstance(supplied[field], str):
            canonical[field] = supplied[field].strip()
    return canonical


def _edit_completion(
    conn: sqlite3.Connection,
    request: BridgeRequest,
    proposal: dict[str, Any],
    field_updates: dict[str, Any],
    current_hash: str,
    *,
    guided_session_id: int | None = None,
) -> HandlerResult:
    assert request.idempotency_key is not None
    try:
        result = complete_proposal(
            conn,
            int(proposal["id"]),
            actor=str(request.arguments["operator_actor_id"]),
            expected_content_hash=current_hash,
            field_updates=dict(field_updates),
            completion_public_id=identity.completion_public_id(request.idempotency_key),
            completion_channel=BRIDGE_CONFIRMATION_CHANNEL,
            transaction_guard=(
                None
                if guided_session_id is None
                else lambda locked: guided_edit.require_pending_authority_in_transaction(
                    locked, guided_session_id
                )
            ),
        )
    except UnknownCompletionFieldError as exc:
        raise errors.bridge_error(
            errors.UNSUPPORTED_EDIT, str(exc), errors.EXIT_VALIDATION_REFUSED
        ) from exc
    except InvalidCompletionFieldValueError as exc:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED, str(exc), errors.EXIT_VALIDATION_REFUSED
        ) from exc
    except NoMaterialChangeError as exc:
        raise errors.bridge_error(
            errors.NO_MATERIAL_CHANGE, str(exc), errors.EXIT_VALIDATION_REFUSED
        ) from exc
    except StaleProposalContentError as exc:
        raise errors.bridge_error(
            errors.STALE_CONTENT_HASH, str(exc), errors.EXIT_AUTHORITY_REFUSED
        ) from exc
    except InvalidCompletionStatusError as exc:
        raise errors.bridge_error(
            errors.PROPOSAL_TERMINAL_STATE, str(exc), errors.EXIT_AUTHORITY_REFUSED
        ) from exc
    except ProposalCompletionError as exc:
        raise errors.bridge_error(
            errors.LIFECYCLE_CONFLICT, str(exc), errors.EXIT_AUTHORITY_REFUSED
        ) from exc

    updated = _fetch_proposal_by_public_id(conn, proposal["public_id"])
    _payload, version, content_hash = _proposal_effective_state(conn, updated)
    return {
        "edit_kind": "completion",
        "proposal_public_id": updated["public_id"],
        "proposal_version": version,
        "effective_content_hash": content_hash,
        "parse_status": str(updated["parse_status"]),
        "final_transaction_created": False,
    }, bool(result["idempotent"])


def _edit_receipt_monetary(
    conn: sqlite3.Connection,
    request: BridgeRequest,
    proposal: dict[str, Any],
    field_updates: dict[str, Any],
    current_hash: str,
    *,
    guided_session_id: int | None = None,
) -> HandlerResult:
    if not has_receipt_ocr_proposal_link(conn, int(proposal["id"])):
        raise errors.bridge_error(
            errors.UNSUPPORTED_EDIT,
            "Text monetary edits have no authoritative bridge boundary.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    assert request.idempotency_key is not None
    try:
        result = supersede_receipt_total_proposal(
            conn,
            int(proposal["id"]),
            actor=str(request.arguments["operator_actor_id"]),
            expected_content_hash=current_hash,
            field_updates=dict(field_updates),
            correction_public_id=identity.correction_public_id(request.idempotency_key),
            correction_channel=BRIDGE_CONFIRMATION_CHANNEL,
            transaction_guard=(
                None
                if guided_session_id is None
                else lambda locked: guided_edit.require_pending_authority_in_transaction(
                    locked, guided_session_id
                )
            ),
        )
    except (NonMonetarySupersessionError, UnknownSupersessionFieldError) as exc:
        raise errors.bridge_error(
            errors.UNSUPPORTED_EDIT, str(exc), errors.EXIT_VALIDATION_REFUSED
        ) from exc
    except InvalidSupersessionFieldValueError as exc:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED, str(exc), errors.EXIT_VALIDATION_REFUSED
        ) from exc
    except NoMaterialSupersessionChangeError as exc:
        raise errors.bridge_error(
            errors.NO_MATERIAL_CHANGE, str(exc), errors.EXIT_VALIDATION_REFUSED
        ) from exc
    except StaleSupersessionContentError as exc:
        raise errors.bridge_error(
            errors.STALE_CONTENT_HASH, str(exc), errors.EXIT_AUTHORITY_REFUSED
        ) from exc
    except ReceiptSupersessionError as exc:
        raise errors.bridge_error(
            errors.LIFECYCLE_CONFLICT, str(exc), errors.EXIT_AUTHORITY_REFUSED
        ) from exc

    return {
        "edit_kind": "receipt_monetary_correction",
        "superseded_proposal_public_id": proposal["public_id"],
        "proposal_public_id": result["replacement_proposal_public_id"],
        "proposal_version": 0,
        "effective_content_hash": result["replacement_content_hash"],
        "parse_status": result["replacement_parse_status"],
        "final_transaction_created": False,
    }, bool(result["idempotent"])


# ---------------------------------------------------------------------------
# Durable guided edit
# ---------------------------------------------------------------------------


_GUIDED_EDIT_CONTEXT_FIELDS = frozenset(
    {
        "workspace_path",
        "operator_actor_id",
        "telegram_account_id",
        "telegram_conversation_id",
        "conversation_binding_id",
    }
)


def _require_guided_session_public_id(value: object) -> str:
    session_public_id = _require_string(value, "session_public_id", max_length=38)
    if (
        len(session_public_id) != 38
        or not session_public_id.startswith("gedit_")
        or any(character not in "0123456789abcdef" for character in session_public_id[6:])
    ):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "session_public_id is invalid.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return session_public_id


def _require_guided_field_value(value: object) -> str:
    field_value = _require_string(value, "field_value", max_length=_MAX_FIELD_VALUE_LENGTH)
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in field_value
    ):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "field_value contains a control or line-separator character.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return field_value


def _guided_session_payload(session: dict[str, Any]) -> dict[str, Any]:
    expired = session["status"] == "active" and (
        int(session["expires_at"]) <= int(datetime.now(UTC).timestamp())
    )
    session_status = (
        "expired"
        if expired
        else "completed_replay"
        if session["status"] == "completed"
        else "active"
    )
    return {
        "active": session_status == "active",
        "session_status": session_status,
        "session_public_id": str(session["session_public_id"]),
        "proposal_public_id": str(session["proposal_public_id"]),
        "proposal_version": int(session["current_proposal_version"]),
        "effective_content_hash": str(session["current_content_hash"]),
        "expires_at": int(session["expires_at"]),
        "recovery_required": session["pending_message_id"] is not None,
        "final_transaction_created": False,
    }


def _guided_completion_payload(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    *,
    context: human_actions.HumanActionContext,
    review_batch_id: str,
) -> dict[str, Any]:
    """Return completion state plus its authoritative current D1 card."""

    card_generation_public_id = guided_edit.active_d1_card_for_session(conn, session)
    if card_generation_public_id is None:
        raise errors.bridge_error(
            errors.LIFECYCLE_CONFLICT,
            "Guided edit completion has no authoritative D1 card.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    try:
        card = get_human_draft_card(
            conn,
            context=HumanDraftContext(
                authenticated_actor_id=context.actor_id,
                telegram_account_id=context.account_id,
                telegram_conversation_id=context.conversation_id,
                conversation_binding_id=context.binding_id,
            ),
            card_generation_public_id=card_generation_public_id,
        )
    except HumanDraftError as exc:
        _raise_human_draft_error(exc)
    return {
        **_guided_session_payload(session),
        "review_batch_id": review_batch_id,
        "human_draft_card": _human_draft_result_payload(conn, card),
    }


def _raise_guided_edit_error(exc: guided_edit.GuidedEditError) -> None:
    code, exit_code = {
        "context_mismatch": (errors.ACTOR_MISMATCH, errors.EXIT_AUTHORITY_REFUSED),
        "invalid_update": (errors.ARGUMENTS_REFUSED, errors.EXIT_VALIDATION_REFUSED),
        "message_reused": (errors.IDEMPOTENCY_CONFLICT, errors.EXIT_AUTHORITY_REFUSED),
        "stale_message": (errors.IDEMPOTENCY_CONFLICT, errors.EXIT_AUTHORITY_REFUSED),
        "pending_recovery": (errors.LIFECYCLE_CONFLICT, errors.EXIT_AUTHORITY_REFUSED),
        "proposal_terminal": (errors.PROPOSAL_TERMINAL_STATE, errors.EXIT_AUTHORITY_REFUSED),
        "session_terminal": (errors.PROPOSAL_TERMINAL_STATE, errors.EXIT_AUTHORITY_REFUSED),
        "session_expired": (errors.CALLBACK_EXPIRED, errors.EXIT_AUTHORITY_REFUSED),
        "stale_session": (errors.STALE_CONTENT_HASH, errors.EXIT_AUTHORITY_REFUSED),
    }.get(exc.reason, (errors.LIFECYCLE_CONFLICT, errors.EXIT_AUTHORITY_REFUSED))
    raise errors.bridge_error(code, "Guided edit session was refused.", exit_code) from exc


def handle_get_guided_edit_session(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    _require_exact_arguments(
        request.arguments,
        required=_GUIDED_EDIT_CONTEXT_FIELDS,
        optional=frozenset({"telegram_message_id"}),
    )
    context = _require_telegram_human_context(request.arguments)
    message_id = (
        None
        if "telegram_message_id" not in request.arguments
        else _require_positive_int(
            request.arguments["telegram_message_id"],
            "telegram_message_id",
            maximum=2**63 - 1,
        )
    )
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("guided edit session read")
        try:
            session = guided_edit.get_context_session(conn, context, message_id=message_id)
        except guided_edit.GuidedEditError as exc:
            _raise_guided_edit_error(exc)
        if session is None:
            return {
                "active": False,
                "session_status": "inactive",
                "final_transaction_created": False,
            }, False
        return _guided_session_payload(session), False
    finally:
        conn.close()


def _pending_guided_update(
    conn: sqlite3.Connection,
    session: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    if session["pending_message_id"] is None:
        raise errors.bridge_error(
            errors.LIFECYCLE_CONFLICT,
            "Guided edit recovery material is missing.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    try:
        field_value = json.loads(str(session["pending_field_value_json"]))
    except json.JSONDecodeError as exc:
        raise errors.bridge_error(
            errors.LIFECYCLE_CONFLICT,
            "Guided edit recovery material is invalid.",
            errors.EXIT_AUTHORITY_REFUSED,
        ) from exc
    if not isinstance(field_value, str):
        raise errors.bridge_error(
            errors.LIFECYCLE_CONFLICT,
            "Guided edit recovery value is invalid.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    proposal = ParserProposalRepository(conn).get(int(session["current_parser_output_id"]))
    if proposal is None:
        raise errors.bridge_error(
            errors.PROPOSAL_NOT_FOUND,
            "Guided edit proposal no longer exists.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    operation_key = str(session["pending_operation_key"])
    synthetic = BridgeRequest(
        envelope_version="v1",
        command="edit",
        request_id="req_" + "0" * 32,
        idempotency_key=operation_key,
        arguments={"operator_actor_id": str(session["authenticated_actor_id"])},
    )
    updates = {str(session["pending_field_name"]): field_value}
    current_hash = str(session["current_content_hash"])
    try:
        persisted_state = guided_edit.pending_core_state(conn, session)
    except guided_edit.GuidedEditError as exc:
        _raise_guided_edit_error(exc)
    if persisted_state is not None:
        updated = ParserProposalRepository(conn).get(persisted_state[0])
        if updated is None:
            raise errors.bridge_error(
                errors.PROPOSAL_NOT_FOUND,
                "Persisted guided edit result no longer exists.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        result = {
            "edit_kind": (
                "receipt_monetary_correction"
                if frozenset(updates) & _MONETARY_FIELDS
                else "completion"
            ),
            "proposal_public_id": str(updated["public_id"]),
            "proposal_version": persisted_state[1],
            "effective_content_hash": persisted_state[2],
            "parse_status": str(updated["parse_status"]),
            "final_transaction_created": False,
        }
        guided_edit.record_update_applied(
            conn,
            session,
            parser_output_id=persisted_state[0],
            proposal_version=persisted_state[1],
            content_hash=persisted_state[2],
        )
        return result, True
    d1_card_id = guided_edit.active_d1_card_for_session(conn, session)
    if d1_card_id is not None:
        operation_id = guided_edit.d1_compatibility_operation_public_id(
            str(session["session_public_id"]), int(session["pending_message_id"])
        )
        field_name = str(session["pending_field_name"])
        compatibility_label = {
            "amount": "Amount",
            "currency": "Currency",
            "transaction_date": "Date",
            "merchant": "Merchant",
            "description": "Description",
            "category": "Category",
        }[field_name]
        raw_card_text = f"Card Ref: {d1_card_id}\n{compatibility_label}: {field_value}"

        def authorize_compatibility_write(locked: sqlite3.Connection) -> None:
            if guided_edit.pending_core_state(locked, session) is not None:
                raise guided_edit.GuidedEditError("pending_core_committed")
            guided_edit.require_pending_authority_in_transaction(locked, int(session["id"]))

        try:
            d1_result = apply_human_draft_card(
                conn,
                HumanDraftCommand(
                    card_generation_public_id=d1_card_id,
                    telegram_message_id=int(session["pending_message_id"]),
                    operation_public_id=operation_id,
                    authenticated_actor_id=str(session["authenticated_actor_id"]),
                    telegram_account_id=str(session["channel_account_id"]),
                    telegram_conversation_id=str(session["channel_conversation_id"]),
                    conversation_binding_id=str(session["conversation_binding_id"]),
                    raw_card_text=raw_card_text,
                    field_values=updates,
                ),
                publish=publish_human_revision_in_transaction,
                authority_validator=authorize_compatibility_write,
            )
        except guided_edit.GuidedEditError as exc:
            if exc.reason == "pending_core_committed":
                recovered = guided_edit.pending_core_state(conn, session)
                if recovered is None:
                    raise errors.bridge_error(
                        errors.LIFECYCLE_CONFLICT,
                        "Guided edit recovery state disappeared after the write lock.",
                        errors.EXIT_AUTHORITY_REFUSED,
                    ) from exc
                updated = ParserProposalRepository(conn).get(recovered[0])
                if updated is None:
                    raise errors.bridge_error(
                        errors.PROPOSAL_NOT_FOUND,
                        "Persisted guided edit result no longer exists.",
                        errors.EXIT_AUTHORITY_REFUSED,
                    ) from exc
                guided_edit.record_update_applied(
                    conn,
                    session,
                    parser_output_id=recovered[0],
                    proposal_version=recovered[1],
                    content_hash=recovered[2],
                )
                return {
                    "edit_kind": "guided_replay",
                    "proposal_public_id": str(updated["public_id"]),
                    "proposal_version": recovered[1],
                    "effective_content_hash": recovered[2],
                    "parse_status": str(updated["parse_status"]),
                    "final_transaction_created": False,
                }, True
            if exc.reason == "session_expired":
                guided_edit.record_update_refused(conn, session, errors.CALLBACK_EXPIRED)
            _raise_guided_edit_error(exc)
        except HumanDraftError as exc:
            _raise_human_draft_error(exc)
        if d1_result.operation_outcome == "refused":
            refusal_code = d1_result.refusal_code or errors.HUMAN_DRAFT_AUTHORITY_REFUSED
            guided_edit.record_update_refused(conn, session, refusal_code)
            raise errors.bridge_error(
                errors.HUMAN_DRAFT_AUTHORITY_REFUSED,
                f"D1 compatibility update was refused: {refusal_code}.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        if d1_result.proposal_public_id is None:
            state = (
                int(session["current_parser_output_id"]),
                int(session["current_proposal_version"]),
                str(session["current_content_hash"]),
            )
            proposal_public_id = str(proposal["public_id"])
            parse_status = str(proposal["parse_status"])
        else:
            updated = _fetch_proposal_by_public_id(conn, d1_result.proposal_public_id)
            assert d1_result.proposal_version is not None
            assert d1_result.proposal_content_hash is not None
            state = (
                int(updated["id"]),
                int(d1_result.proposal_version),
                str(d1_result.proposal_content_hash),
            )
            proposal_public_id = str(updated["public_id"])
            parse_status = str(updated["parse_status"])
        guided_edit.record_update_applied(
            conn,
            session,
            parser_output_id=state[0],
            proposal_version=state[1],
            content_hash=state[2],
        )
        return {
            "edit_kind": "d1_compatibility",
            "proposal_public_id": proposal_public_id,
            "proposal_version": state[1],
            "effective_content_hash": state[2],
            "parse_status": parse_status,
            "human_draft_card": _human_draft_result_payload(conn, d1_result),
            "final_transaction_created": False,
        }, d1_result.idempotent_replay
    try:
        result, replay = (
            _edit_receipt_monetary(
                conn,
                synthetic,
                proposal,
                updates,
                current_hash,
                guided_session_id=int(session["id"]),
            )
            if frozenset(updates) & _MONETARY_FIELDS
            else _edit_completion(
                conn,
                synthetic,
                proposal,
                updates,
                current_hash,
                guided_session_id=int(session["id"]),
            )
        )
    except guided_edit.GuidedEditError as exc:
        if exc.reason == "session_expired":
            guided_edit.record_update_refused(conn, session, errors.CALLBACK_EXPIRED)
        _raise_guided_edit_error(exc)
    except errors.BridgeError as exc:
        if not exc.retryable:
            guided_edit.record_update_refused(conn, session, exc.code)
        raise
    updated = _fetch_proposal_by_public_id(conn, str(result["proposal_public_id"]))
    guided_edit.record_update_applied(
        conn,
        session,
        parser_output_id=int(updated["id"]),
        proposal_version=int(result["proposal_version"]),
        content_hash=str(result["effective_content_hash"]),
    )
    return result, replay


def handle_apply_guided_edit_update(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    required = _GUIDED_EDIT_CONTEXT_FIELDS | frozenset(
        {"session_public_id", "telegram_message_id", "field_name", "field_value"}
    )
    _require_exact_arguments(request.arguments, required=required)
    context = _require_telegram_human_context(request.arguments)
    session_public_id = _require_guided_session_public_id(request.arguments["session_public_id"])
    message_id = _require_positive_int(
        request.arguments["telegram_message_id"], "telegram_message_id", maximum=2**63 - 1
    )
    field_name = _require_string(request.arguments["field_name"], "field_name", max_length=32)
    field_value = _require_guided_field_value(request.arguments["field_value"])
    if field_name not in guided_edit.ALLOWED_FIELDS:
        raise errors.bridge_error(
            errors.UNSUPPORTED_EDIT,
            "Guided edit field is not supported.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    _require_canonical_idempotency_key(
        request, canonical_guided_edit_update_key(session_public_id, message_id)
    )

    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("guided edit session load")
        session = guided_edit.get_session_by_public_id(conn, session_public_id, context)
        if session is None or session["status"] != "active":
            raise errors.bridge_error(
                errors.PROPOSAL_TERMINAL_STATE,
                "Guided edit session is not active.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        replay_event = guided_edit.find_applied_replay(conn, int(session["id"]), message_id)
        if replay_event is not None:
            if (
                replay_event["field_name"] != field_name
                or json.loads(str(replay_event["field_value_json"])) != field_value
            ):
                raise errors.bridge_error(
                    errors.IDEMPOTENCY_CONFLICT,
                    "Telegram edit message is already bound to different material.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            return {
                "edit_kind": "guided_replay",
                "session_public_id": session_public_id,
                "proposal_public_id": str(replay_event["proposal_public_id"]),
                "proposal_version": int(replay_event["after_proposal_version"]),
                "effective_content_hash": str(replay_event["after_content_hash"]),
                "parse_status": "edited_pending_confirmation",
                "final_transaction_created": False,
            }, True
        refused_event = guided_edit.find_refused_replay(conn, int(session["id"]), message_id)
        if refused_event is not None:
            if (
                refused_event["field_name"] != field_name
                or json.loads(str(refused_event["field_value_json"])) != field_value
            ):
                raise errors.bridge_error(
                    errors.IDEMPOTENCY_CONFLICT,
                    "Telegram edit message is already bound to different material.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            refusal_code = str(refused_event["refusal_code"])
            validation_codes = {
                errors.ARGUMENTS_REFUSED,
                errors.NO_MATERIAL_CHANGE,
                errors.UNSUPPORTED_EDIT,
            }
            raise errors.bridge_error(
                refusal_code,
                "Guided edit update was previously refused.",
                errors.EXIT_VALIDATION_REFUSED
                if refusal_code in validation_codes
                else errors.EXIT_AUTHORITY_REFUSED,
            )
        if session["pending_message_id"] is not None:
            pending_message_id = int(session["pending_message_id"])
            if pending_message_id == message_id and (
                session["pending_field_name"] != field_name
                or json.loads(str(session["pending_field_value_json"])) != field_value
            ):
                raise errors.bridge_error(
                    errors.IDEMPOTENCY_CONFLICT,
                    "Telegram edit message is already bound to different material.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            expired = int(session["expires_at"]) <= int(datetime.now(UTC).timestamp())
            if expired and pending_message_id != message_id:
                raise errors.bridge_error(
                    errors.CALLBACK_EXPIRED,
                    "Guided edit session expired with different recovery material.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            recovered, _replay = _pending_guided_update(conn, session)
            if pending_message_id == message_id:
                return {**recovered, "session_public_id": session_public_id}, True
            session = guided_edit.get_session_by_public_id(conn, session_public_id, context)
            assert session is not None

        if int(session["expires_at"]) <= int(datetime.now(UTC).timestamp()):
            raise errors.bridge_error(
                errors.CALLBACK_EXPIRED,
                "Guided edit session has expired.",
                errors.EXIT_AUTHORITY_REFUSED,
            )

        operation_key = canonical_edit_key(
            proposal_public_id=str(session["proposal_public_id"]),
            version=int(session["current_proposal_version"]),
            content_hash=str(session["current_content_hash"]),
        )
        try:
            pending = guided_edit.request_update(
                conn,
                session,
                message_id=message_id,
                operation_key=operation_key,
                field_name=field_name,
                field_value=field_value,
            )
        except guided_edit.GuidedEditError as exc:
            _raise_guided_edit_error(exc)
        result, replay = _pending_guided_update(conn, pending)
        return {**result, "session_public_id": session_public_id}, replay
    finally:
        conn.close()


def handle_complete_guided_edit(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    required = _GUIDED_EDIT_CONTEXT_FIELDS | frozenset({"session_public_id", "telegram_message_id"})
    _require_exact_arguments(request.arguments, required=required)
    context = _require_telegram_human_context(request.arguments)
    session_public_id = _require_guided_session_public_id(request.arguments["session_public_id"])
    message_id = _require_positive_int(
        request.arguments["telegram_message_id"], "telegram_message_id", maximum=2**63 - 1
    )
    _require_canonical_idempotency_key(
        request, canonical_guided_edit_complete_key(session_public_id, message_id)
    )
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        session = guided_edit.get_session_by_public_id(conn, session_public_id, context)
        if session is None:
            raise errors.bridge_error(
                errors.ACTOR_MISMATCH,
                "Guided edit session does not match this private conversation.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        if session["status"] == "completed" and session["completed_message_id"] == message_id:
            try:
                review_batch_id = guided_edit.claim_review_batch(
                    conn, session, context=context, message_id=message_id
                )
            except guided_edit.GuidedEditError as exc:
                _raise_guided_edit_error(exc)
            return _guided_completion_payload(
                conn,
                session,
                context=context,
                review_batch_id=review_batch_id,
            ), True
        if session["status"] != "active":
            raise errors.bridge_error(
                errors.PROPOSAL_TERMINAL_STATE,
                "Guided edit session is not active.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        if session["pending_message_id"] is not None:
            if int(session["expires_at"]) <= int(datetime.now(UTC).timestamp()):
                raise errors.bridge_error(
                    errors.CALLBACK_EXPIRED,
                    "Guided edit session has expired.",
                    errors.EXIT_AUTHORITY_REFUSED,
                )
            _pending_guided_update(conn, session)
            session = guided_edit.get_session_by_public_id(conn, session_public_id, context)
            assert session is not None
        try:
            completed = guided_edit.complete_session(
                conn, session, context=context, message_id=message_id
            )
            review_batch_id = guided_edit.claim_review_batch(
                conn, completed, context=context, message_id=message_id
            )
        except guided_edit.GuidedEditError as exc:
            _raise_guided_edit_error(exc)
        return _guided_completion_payload(
            conn,
            completed,
            context=context,
            review_batch_id=review_batch_id,
        ), False
    finally:
        conn.close()


_HUMAN_DRAFT_FIELDS = frozenset(
    {"amount", "currency", "transaction_date", "merchant", "description", "category"}
)
_HUMAN_DRAFT_CONTEXT_FIELDS = frozenset(
    {
        "workspace_path",
        "operator_actor_id",
        "telegram_account_id",
        "telegram_conversation_id",
        "conversation_binding_id",
    }
)


def _require_card_generation_public_id(value: object) -> str:
    card_id = _require_string(value, "card_generation_public_id", max_length=39)
    if (
        len(card_id) != 39
        or not card_id.startswith("d1card_")
        or any(character not in "0123456789abcdef" for character in card_id[7:])
    ):
        raise errors.bridge_error(
            errors.HUMAN_DRAFT_ARGUMENTS_REFUSED,
            "card_generation_public_id is not a valid D1 card identity.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return card_id


def _require_sha256_identity(value: object, name: str) -> str:
    identity_value = _require_string(value, name, max_length=64)
    if len(identity_value) != 64 or any(
        character not in "0123456789abcdef" for character in identity_value
    ):
        raise errors.bridge_error(
            errors.HUMAN_DRAFT_ARGUMENTS_REFUSED,
            f"{name} must be a 64-character lowercase SHA-256 identity.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    return identity_value


def _require_human_draft_field_values(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or frozenset(value) != _HUMAN_DRAFT_FIELDS:
        raise errors.bridge_error(
            errors.HUMAN_DRAFT_ARGUMENTS_REFUSED,
            "field_values must contain each supported whole-card field exactly once.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    result: dict[str, str] = {}
    for field in sorted(_HUMAN_DRAFT_FIELDS):
        field_value = value[field]
        if not isinstance(field_value, str) or len(field_value) > 16_384:
            raise errors.bridge_error(
                errors.HUMAN_DRAFT_ARGUMENTS_REFUSED,
                f"field_values.{field} must be a bounded string.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        try:
            field_value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise errors.bridge_error(
                errors.HUMAN_DRAFT_ARGUMENTS_REFUSED,
                f"field_values.{field} is not valid UTF-8 material.",
                errors.EXIT_VALIDATION_REFUSED,
            ) from exc
        result[field] = field_value
    return result


def _raise_human_draft_error(exc: HumanDraftError) -> None:
    if exc.reason in {
        "card_missing",
        "draft_missing",
        "operation_missing",
        "source_reference_missing",
    }:
        code = errors.HUMAN_DRAFT_NOT_FOUND
    elif exc.reason in {
        "command_invalid",
        "durable_identity_required",
        "delivery_material_invalid",
        "delivery_outcome_invalid",
        "failure_code_required",
        "reply_size_invalid",
        "reply_utf8_invalid",
        "recovery_material_invalid",
        "recovery_reason_invalid",
        "transport_mode_invalid",
    }:
        code = errors.HUMAN_DRAFT_ARGUMENTS_REFUSED
    elif exc.reason in {
        "message_conflict",
        "attempt_conflict",
        "durable_identity_mismatch",
        "observation_conflict",
        "operation_conflict",
        "recovery_conflict",
    }:
        code = errors.HUMAN_DRAFT_CONFLICT
    else:
        code = errors.HUMAN_DRAFT_AUTHORITY_REFUSED
    raise errors.bridge_error(
        code,
        "D1 human-draft authority refused the command.",
        errors.EXIT_VALIDATION_REFUSED
        if code == errors.HUMAN_DRAFT_ARGUMENTS_REFUSED
        else errors.EXIT_AUTHORITY_REFUSED,
        details={"reason": exc.reason},
    ) from exc


def handle_apply_human_draft_card(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    required = _HUMAN_DRAFT_CONTEXT_FIELDS | frozenset(
        {
            "card_generation_public_id",
            "telegram_message_id",
            "operation_public_id",
            "raw_card_text",
            "field_values",
        }
    )
    _require_exact_arguments(request.arguments, required=required)
    context = _require_telegram_human_context(request.arguments)
    card_id = _require_card_generation_public_id(request.arguments["card_generation_public_id"])
    message_id = _require_positive_int(
        request.arguments["telegram_message_id"],
        "telegram_message_id",
        maximum=2**63 - 1,
    )
    operation_id = _require_string(
        request.arguments["operation_public_id"], "operation_public_id", max_length=200
    )
    raw_card_text = request.arguments["raw_card_text"]
    if not isinstance(raw_card_text, str) or not raw_card_text:
        raise errors.bridge_error(
            errors.HUMAN_DRAFT_ARGUMENTS_REFUSED,
            "raw_card_text must be a non-empty string.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    try:
        raw_bytes = raw_card_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise errors.bridge_error(
            errors.HUMAN_DRAFT_ARGUMENTS_REFUSED,
            "raw_card_text is not valid UTF-8 material.",
            errors.EXIT_VALIDATION_REFUSED,
        ) from exc
    if len(raw_bytes) > 16_384:
        raise errors.bridge_error(
            errors.HUMAN_DRAFT_ARGUMENTS_REFUSED,
            "raw_card_text exceeds the 16,384-byte D1 evidence limit.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    field_values = _require_human_draft_field_values(request.arguments["field_values"])
    _require_canonical_idempotency_key(request, canonical_human_draft_apply_key(operation_id))

    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("human draft whole-card apply")
        try:
            result = apply_human_draft_card(
                conn,
                HumanDraftCommand(
                    card_generation_public_id=card_id,
                    telegram_message_id=message_id,
                    operation_public_id=operation_id,
                    authenticated_actor_id=context.actor_id,
                    telegram_account_id=context.account_id,
                    telegram_conversation_id=context.conversation_id,
                    conversation_binding_id=context.binding_id,
                    raw_card_text=raw_card_text,
                    field_values=field_values,
                ),
                publish=publish_human_revision_in_transaction,
            )
        except HumanDraftError as exc:
            _raise_human_draft_error(exc)
        except HumanRevisionLineageError as exc:
            raise errors.bridge_error(
                errors.HUMAN_DRAFT_AUTHORITY_REFUSED,
                "D1 human revision lineage refused publication.",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc
        return _human_draft_result_payload(conn, result), result.idempotent_replay
    finally:
        conn.close()


def handle_get_human_draft_card(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    identity_fields = frozenset(
        {
            "operation_public_id",
            "source_edit_reference_public_id",
            "card_generation_public_id",
            "attempt_public_id",
        }
    )
    _require_exact_arguments(
        request.arguments,
        required=_HUMAN_DRAFT_CONTEXT_FIELDS,
        optional=identity_fields,
    )
    context = _require_telegram_human_context(request.arguments)
    supplied = identity_fields & frozenset(request.arguments)
    if not supplied:
        raise errors.bridge_error(
            errors.HUMAN_DRAFT_ARGUMENTS_REFUSED,
            "At least one durable D1 card identity is required.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    identities: dict[str, str] = {}
    for name in sorted(supplied):
        if name == "card_generation_public_id":
            identities[name] = _require_card_generation_public_id(request.arguments[name])
        else:
            identities[name] = _require_string(request.arguments[name], name, max_length=200)

    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("human draft card query")
        try:
            result = get_human_draft_card(
                conn,
                context=HumanDraftContext(
                    authenticated_actor_id=context.actor_id,
                    telegram_account_id=context.account_id,
                    telegram_conversation_id=context.conversation_id,
                    conversation_binding_id=context.binding_id,
                ),
                operation_public_id=identities.get("operation_public_id"),
                source_edit_reference_public_id=identities.get("source_edit_reference_public_id"),
                card_generation_public_id=identities.get("card_generation_public_id"),
                attempt_public_id=identities.get("attempt_public_id"),
            )
        except HumanDraftError as exc:
            _raise_human_draft_error(exc)
        return _human_draft_result_payload(conn, result), False
    finally:
        conn.close()


def _human_draft_context(context: human_actions.HumanActionContext) -> HumanDraftContext:
    return HumanDraftContext(
        authenticated_actor_id=context.actor_id,
        telegram_account_id=context.account_id,
        telegram_conversation_id=context.conversation_id,
        conversation_binding_id=context.binding_id,
    )


def handle_begin_human_draft_card_delivery(
    request: BridgeRequest, deadline: Deadline
) -> HandlerResult:
    required = _HUMAN_DRAFT_CONTEXT_FIELDS | frozenset(
        {
            "card_generation_public_id",
            "attempt_public_id",
            "delivery_material_hash",
            "transport_mode",
        }
    )
    _require_exact_arguments(
        request.arguments,
        required=required,
        optional=frozenset({"outbound_target_message_id"}),
    )
    context = _require_telegram_human_context(request.arguments)
    card_id = _require_card_generation_public_id(request.arguments["card_generation_public_id"])
    attempt_id = _require_sha256_identity(
        request.arguments["attempt_public_id"], "attempt_public_id"
    )
    material_hash = _require_sha256_identity(
        request.arguments["delivery_material_hash"], "delivery_material_hash"
    )
    mode = _require_string(request.arguments["transport_mode"], "transport_mode", max_length=16)
    target = request.arguments.get("outbound_target_message_id")
    if target is not None:
        target = _require_string(target, "outbound_target_message_id", max_length=200)
    _require_canonical_idempotency_key(request, canonical_human_draft_delivery_key(attempt_id))
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("human draft delivery attempt")
        changes_before = conn.total_changes
        try:
            result_id = begin_human_draft_card_delivery(
                conn,
                context=_human_draft_context(context),
                card_generation_public_id=card_id,
                attempt_public_id=attempt_id,
                delivery_material_hash=material_hash,
                transport_mode=mode,
                outbound_target_message_id=target,
                now_epoch=int(datetime.now(UTC).timestamp()),
            )
        except HumanDraftError as exc:
            _raise_human_draft_error(exc)
        replay = conn.total_changes == changes_before
        return {"attempt_public_id": result_id}, replay
    finally:
        conn.close()


def handle_record_human_draft_card_delivery_outcome(
    request: BridgeRequest, deadline: Deadline
) -> HandlerResult:
    required = _HUMAN_DRAFT_CONTEXT_FIELDS | frozenset(
        {
            "attempt_public_id",
            "observation_public_id",
            "outcome",
            "error_code",
            "outbound_message_id",
            "trusted_receipt_hash",
        }
    )
    _require_exact_arguments(request.arguments, required=required)
    context = _require_telegram_human_context(request.arguments)
    attempt_id = _require_sha256_identity(
        request.arguments["attempt_public_id"], "attempt_public_id"
    )
    observation_id = _require_sha256_identity(
        request.arguments["observation_public_id"], "observation_public_id"
    )
    outcome = _require_string(request.arguments["outcome"], "outcome", max_length=16)
    error_code = request.arguments["error_code"]
    if error_code is not None:
        error_code = _require_string(error_code, "error_code", max_length=100)
    outbound_message_id = request.arguments["outbound_message_id"]
    if outbound_message_id is not None:
        outbound_message_id = _require_string(
            outbound_message_id, "outbound_message_id", max_length=200
        )
    trusted_receipt_hash = request.arguments["trusted_receipt_hash"]
    if trusted_receipt_hash is not None:
        trusted_receipt_hash = _require_sha256_identity(
            trusted_receipt_hash, "trusted_receipt_hash"
        )
    _require_canonical_idempotency_key(
        request, canonical_human_draft_observation_key(observation_id)
    )
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("human draft delivery outcome")
        changes_before = conn.total_changes
        try:
            result_id = record_human_draft_card_delivery_outcome(
                conn,
                context=_human_draft_context(context),
                attempt_public_id=attempt_id,
                observation_public_id=observation_id,
                outcome=outcome,
                error_code=error_code,
                outbound_message_id=outbound_message_id,
                trusted_receipt_hash=trusted_receipt_hash,
                now_epoch=int(datetime.now(UTC).timestamp()),
            )
        except HumanDraftError as exc:
            _raise_human_draft_error(exc)
        replay = conn.total_changes == changes_before
        return {"observation_public_id": result_id}, replay
    finally:
        conn.close()


def handle_reissue_human_draft_card(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    required = _HUMAN_DRAFT_CONTEXT_FIELDS | frozenset(
        {
            "expected_current_generation_public_id",
            "original_operation_or_start_public_id",
            "recovery_public_id",
            "recovery_material_hash",
            "queried_delivery_state_hash",
            "reason",
        }
    )
    _require_exact_arguments(request.arguments, required=required)
    context = _require_telegram_human_context(request.arguments)
    expected_generation = _require_card_generation_public_id(
        request.arguments["expected_current_generation_public_id"]
    )
    original_id = _require_string(
        request.arguments["original_operation_or_start_public_id"],
        "original_operation_or_start_public_id",
        max_length=200,
    )
    recovery_id = _require_sha256_identity(
        request.arguments["recovery_public_id"], "recovery_public_id"
    )
    material_hash = _require_sha256_identity(
        request.arguments["recovery_material_hash"], "recovery_material_hash"
    )
    state_hash = _require_sha256_identity(
        request.arguments["queried_delivery_state_hash"], "queried_delivery_state_hash"
    )
    reason = _require_string(request.arguments["reason"], "reason", max_length=32)
    _require_canonical_idempotency_key(request, canonical_human_draft_reissue_key(recovery_id))
    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("human draft card reissue")
        try:
            result = reissue_human_draft_card(
                conn,
                context=_human_draft_context(context),
                expected_current_generation_public_id=expected_generation,
                original_operation_or_start_public_id=original_id,
                recovery_public_id=recovery_id,
                recovery_material_hash=material_hash,
                queried_delivery_state_hash=state_hash,
                reason=reason,
                now_epoch=int(datetime.now(UTC).timestamp()),
            )
        except HumanDraftError as exc:
            _raise_human_draft_error(exc)
        return _human_draft_result_payload(conn, result), result.idempotent_replay
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# finalize (S4)
# ---------------------------------------------------------------------------

_FINALIZE_REQUIRED_FIELDS = frozenset(
    {
        "workspace_path",
        "proposal_public_id",
        "operator_actor_id",
        "proposal_version",
        "content_hash",
    }
)
_FINALIZE_OPTIONAL_FIELDS = frozenset({"receipt_only"})

_PREPARE_RECEIPT_COMPLETION_REQUIRED_FIELDS = _FINALIZE_REQUIRED_FIELDS

_FINALIZATION_SNAPSHOT_REVIEW_REQUIRED_FIELDS = frozenset(
    {"workspace_path", "proposal_public_id", "operator_actor_id"}
)

_AUTHORIZE_FINALIZATION_REQUIRED_FIELDS = frozenset(
    {
        "workspace_path",
        "proposal_public_id",
        "operator_actor_id",
        "expected_calculation_snapshot_hash",
    }
)

_APPLY_FACT_SET_REQUIRED_FIELDS = frozenset(
    {"workspace_path", "proposal_public_id", "operator_actor_id", "command_filename"}
)


def handle_prepare_receipt_completion(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    """Run only the guarded receipt-conversion prerequisite for D1b.

    This bounded command does not inspect readiness, build a calculation
    snapshot, authorize finalization, or create final facts.  It makes the
    existing conversion lineage available for an out-of-envelope human D1b
    fact-set command while preserving exactly the same confirmed receipt and
    personal-only guards as receipt finalization.
    """
    _require_exact_arguments(
        request.arguments, required=_PREPARE_RECEIPT_COMPLETION_REQUIRED_FIELDS
    )
    proposal_public_id = _require_string(
        request.arguments["proposal_public_id"], "proposal_public_id", max_length=200
    )
    operator_actor_id = _require_string(
        request.arguments["operator_actor_id"], "operator_actor_id", max_length=_MAX_ACTOR_ID_LENGTH
    )
    proposal_version = _require_non_negative_int(
        request.arguments["proposal_version"], "proposal_version", maximum=1_000_000
    )
    content_hash = _require_content_hash(request.arguments["content_hash"])
    _require_canonical_idempotency_key(
        request, canonical_prepare_receipt_completion_key(proposal_public_id)
    )

    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("receipt preparation replay reconstruction")
        proposal = _fetch_proposal_by_public_id(conn, proposal_public_id)
        payload, version, durable_hash = _proposal_effective_state(conn, proposal)
        if version != proposal_version:
            raise errors.bridge_error(
                errors.STALE_VERSION,
                "Envelope proposal_version does not match the durable proposal version.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        if durable_hash != content_hash:
            raise errors.bridge_error(
                errors.STALE_CONTENT_HASH,
                "Envelope content_hash does not match the durable effective content hash.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        if str(proposal["parse_status"]) != CONFIRMED:
            raise _finalization_refused(
                "not_confirmed", "Only confirmed receipt proposals can be prepared."
            )
        if not has_receipt_ocr_proposal_link(conn, int(proposal["id"])):
            raise _finalization_refused(
                "unsupported_path", "prepare_receipt_completion supports receipt proposals only."
            )
        confirmed_actor = _require_durable_confirmed_actor(conn, proposal, operator_actor_id)
        command_public_id, receipt_public_id, conversion_result_hash, idempotent_replay = (
            _prepare_receipt_conversion(
                conn,
                deadline,
                proposal=proposal,
                payload=payload,
                confirmed_actor=confirmed_actor,
                content_hash=content_hash,
            )
        )
        return {
            "identity_kind": "prepare_receipt_completion",
            "proposal_public_id": proposal_public_id,
            "receipt_public_id": receipt_public_id,
            "conversion_command_public_id": command_public_id,
            "conversion_result_hash": conversion_result_hash,
            "content_hash": content_hash,
        }, idempotent_replay
    finally:
        conn.close()


def handle_get_finalization_snapshot_review(
    request: BridgeRequest, deadline: Deadline
) -> HandlerResult:
    """Prepare and render one verified receipt snapshot for direct human review.

    Preparation remains the existing Python IAF boundary. The rendering below
    reads its persisted, hash-verified snapshot and cannot authorize or
    finalize a receipt, including when an authorization already exists.
    """
    _require_exact_arguments(
        request.arguments, required=_FINALIZATION_SNAPSHOT_REVIEW_REQUIRED_FIELDS
    )
    proposal_public_id = _require_string(
        request.arguments["proposal_public_id"], "proposal_public_id", max_length=200
    )
    operator_actor_id = _require_string(
        request.arguments["operator_actor_id"],
        "operator_actor_id",
        max_length=_MAX_ACTOR_ID_LENGTH,
    )
    _require_canonical_idempotency_key(
        request, canonical_finalization_snapshot_review_key(proposal_public_id)
    )

    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("snapshot review replay reconstruction")
        proposal = _fetch_proposal_by_public_id(conn, proposal_public_id)
        if str(proposal["parse_status"]) != CONFIRMED:
            raise _finalization_refused(
                "not_confirmed", "Only confirmed receipt proposals can be reviewed."
            )
        if not has_receipt_ocr_proposal_link(conn, int(proposal["id"])):
            raise _finalization_refused(
                "unsupported_path",
                "get_finalization_snapshot_review supports receipt proposals only.",
            )
        _require_durable_confirmed_actor(conn, proposal, operator_actor_id)
        self_participant_id = _require_single_self_participant(conn)
        payload, _version, durable_hash = _proposal_effective_state(conn, proposal)
        _require_personal_receipt_classification(payload)
        command_public_id, receipt_public_id = _receipt_identities_for(
            proposal_public_id, durable_hash
        )

        deadline.check("fact-set readiness")
        try:
            report = report_receipt_calculator_readiness(conn, receipt_public_id)
        except ReceiptNotFoundError as exc:
            raise _finalization_refused(
                "not_converted",
                "Receipt facts do not exist yet; prepare the receipt first.",
            ) from exc
        except ReceiptFactsIntegrityError as exc:
            raise _finalization_refused(
                "fact_set_integrity_refused", f"Receipt fact-set integrity refused: {exc}"
            ) from exc
        if not report.is_calculator_ready:
            raise _finalization_refused(
                "no_authoritative_item_facts",
                "Snapshot review requires an existing, durable, human-authored, "
                "calculator-ready IAF fact set (D1b).",
                extra_details={
                    "receipt_public_id": receipt_public_id,
                    "conversion_command_public_id": command_public_id,
                    "not_ready_reasons": ",".join(report.not_ready_reasons),
                },
            )

        deadline.check("calculation preparation")
        try:
            prepared = prepare_receipt_calculation(conn, receipt_public_id)
        except BridgePreparationError as exc:
            raise _finalization_refused(
                "not_calculator_ready", f"Calculation preparation refused: {exc}"
            ) from exc
        return _render_finalization_snapshot_review(
            conn,
            proposal_public_id=proposal_public_id,
            receipt_public_id=receipt_public_id,
            self_participant_id=self_participant_id,
            prepared=prepared,
        ), prepared.idempotent_replay
    finally:
        conn.close()


def handle_finalize(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    """Finalize one confirmed proposal through the existing guarded paths.

    Text path: delegates to ``convert_confirmed_parser_proposal`` (the
    durable confirmation is the complete authority; no second human
    authorization exists).  Receipt path (D1b/D2B): converts through the
    guarded receipt conversion boundary, then requires an existing durable
    human-authored calculator-ready fact set and a separate snapshot-bound
    human authorization before the guarded finalizer may run exactly once.
    Envelopes never carry amounts, snapshots, allocations, or participant
    data.
    """
    _require_exact_arguments(
        request.arguments,
        required=_FINALIZE_REQUIRED_FIELDS,
        optional=_FINALIZE_OPTIONAL_FIELDS,
    )
    proposal_public_id = _require_string(
        request.arguments["proposal_public_id"], "proposal_public_id", max_length=200
    )
    operator_actor_id = _require_string(
        request.arguments["operator_actor_id"], "operator_actor_id", max_length=_MAX_ACTOR_ID_LENGTH
    )
    proposal_version = _require_non_negative_int(
        request.arguments["proposal_version"], "proposal_version", maximum=1_000_000
    )
    content_hash = _require_content_hash(request.arguments["content_hash"])
    receipt_only = request.arguments.get("receipt_only", False)
    if not isinstance(receipt_only, bool):
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "receipt_only must be a boolean.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    _require_canonical_idempotency_key(request, canonical_finalize_key(proposal_public_id))

    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("finalize replay reconstruction")
        proposal = _fetch_proposal_by_public_id(conn, proposal_public_id)
        payload, version, durable_hash = _proposal_effective_state(conn, proposal)
        if version != proposal_version:
            raise errors.bridge_error(
                errors.STALE_VERSION,
                "Envelope proposal_version does not match the durable proposal version.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        if durable_hash != content_hash:
            raise errors.bridge_error(
                errors.STALE_CONTENT_HASH,
                "Envelope content_hash does not match the durable effective content hash.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        if str(proposal["parse_status"]) != CONFIRMED:
            raise _finalization_refused(
                "not_confirmed",
                "Only confirmed proposals can be finalized.",
            )
        confirmed_actor = _require_durable_confirmed_actor(conn, proposal, operator_actor_id)
        if has_receipt_ocr_proposal_link(conn, int(proposal["id"])):
            return _finalize_receipt(
                conn,
                deadline,
                proposal=proposal,
                payload=payload,
                confirmed_actor=confirmed_actor,
                content_hash=content_hash,
            )
        if receipt_only:
            raise _finalization_refused(
                "unsupported_path", "receipt-only finalize supports receipt proposals only."
            )
        return _finalize_text(conn, proposal, content_hash)
    finally:
        conn.close()


def _finalize_text(
    conn: sqlite3.Connection, proposal: dict[str, Any], content_hash: str
) -> HandlerResult:
    try:
        result = convert_confirmed_parser_proposal(conn, int(proposal["id"]))
    except Exception as exc:
        if _sqlite_busy(exc):
            raise _finalization_locked("Confirmed-proposal conversion") from exc
        if isinstance(exc, (AlreadyConvertedProposalError, UnsupportedProposalTypeError)):
            raise _finalization_refused(
                "unsupported_path", f"Text-expense conversion refused: {exc}"
            ) from exc
        if isinstance(exc, MissingConfirmationRecordError):
            raise _finalization_refused(
                "missing_confirmation", f"No authoritative confirmation record: {exc}"
            ) from exc
        if isinstance(exc, InvalidProposalStatusError):
            raise _finalization_refused("not_confirmed", str(exc)) from exc
        if isinstance(exc, StaleProposalConfirmationError):
            raise errors.bridge_error(
                errors.STALE_CONTENT_HASH,
                f"Confirmation is bound to stale proposal content: {exc}",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc
        if isinstance(exc, ParserConfirmationError):
            raise _finalization_refused("guard_refused", str(exc)) from exc
        raise
    transaction_public_id = result.get("transaction_public_id")
    if not transaction_public_id:
        raise errors.bridge_error(
            errors.INTERNAL_ERROR,
            "Conversion claimed success without a durable canonical transaction.",
            errors.EXIT_INTERNAL,
        )
    return {
        "identity_kind": "finalize",
        "path": "text_expense",
        "proposal_public_id": str(proposal["public_id"]),
        "confirmation_public_id": str(result["confirmation_id"]),
        "transaction_public_id": str(transaction_public_id),
        "final_transaction_created": True,
        "content_hash": content_hash,
    }, bool(result.get("idempotent", False))


def _finalize_receipt(
    conn: sqlite3.Connection,
    deadline: Deadline,
    *,
    proposal: dict[str, Any],
    payload: dict[str, Any],
    confirmed_actor: str,
    content_hash: str,
) -> HandlerResult:
    proposal_public_id = str(proposal["public_id"])
    command_public_id, receipt_public_id, conversion_result_hash, _conversion_idempotent = (
        _prepare_receipt_conversion(
            conn,
            deadline,
            proposal=proposal,
            payload=payload,
            confirmed_actor=confirmed_actor,
            content_hash=content_hash,
        )
    )

    deadline.check("finalization stage read")
    stage = read_receipt_finalization_stage(conn, receipt_public_id=receipt_public_id)
    if (
        stage.finalization_status in ("finalized", "already_finalized")
        and stage.transaction_public_id
        and stage.authorization_id is not None
    ):
        # Pure replay: durable truth already records a completed
        # finalization, so readiness and preparation are skipped — both are
        # idempotent, but preparation could still append a non-essential
        # snapshot row after a later fact-set supersession.  Load and the
        # guarded finalizer remain the sole authorities.
        authorization_id = stage.authorization_id
    else:
        deadline.check("fact-set readiness")
        _require_ready_receipt(
            conn,
            receipt_public_id,
            command_public_id=command_public_id,
            conversion_result_hash=conversion_result_hash,
        )

        deadline.check("calculation preparation")
        try:
            prepared = prepare_receipt_calculation(conn, receipt_public_id)
        except BridgePreparationError as exc:
            raise _finalization_refused(
                "not_calculator_ready", f"Calculation preparation refused: {exc}"
            ) from exc

        # D2B: finalize never chains prepare into authorize.  Without a
        # durable snapshot-bound human authorization the command stops here
        # and returns the bounded snapshot review material for the separate
        # human step.
        if stage.authorization_id is None:
            raise errors.bridge_error(
                errors.SNAPSHOT_AUTHORIZATION_REQUIRED,
                "Receipt finalization requires a separate snapshot-bound human "
                "authorization (D2B); review the calculation snapshot and run "
                "authorize_finalization with its hash.",
                errors.EXIT_AUTHORITY_REFUSED,
                details={
                    "receipt_public_id": receipt_public_id,
                    "calculation_snapshot_id": prepared.calculation_snapshot_id,
                    "calculation_snapshot_hash": prepared.calculation_snapshot_hash,
                    "authorization_id": prepared.authorization_id,
                },
            )
        authorization_id = stage.authorization_id

    deadline.check("authorization reconstruction")
    try:
        authorization = load_persisted_receipt_finalization_authorization(conn, authorization_id)
    except BridgeRecoveryError as exc:
        raise _finalization_refused(
            "authorization_recheck_failed", f"Authorization reconstruction failed: {exc}"
        ) from exc

    deadline.check("guarded finalization")
    try:
        output = finalize_prepared_receipt(conn, authorization)
    except Exception as exc:
        if _sqlite_busy(exc):
            raise _finalization_locked("Guarded finalization") from exc
        reason = getattr(exc, "reason", None)
        raise _finalization_refused(
            str(reason) if reason is not None else "finalization_refused",
            f"Guarded finalization refused: {exc}",
        ) from exc
    if not output.transaction_public_id:
        raise errors.bridge_error(
            errors.INTERNAL_ERROR,
            "Guarded finalization claimed success without a durable transaction.",
            errors.EXIT_INTERNAL,
        )
    return {
        "identity_kind": "finalize",
        "path": "receipt",
        "proposal_public_id": proposal_public_id,
        "confirmation_public_id": authorization.confirmation_id,
        "receipt_public_id": receipt_public_id,
        "fact_set_public_id": authorization.prepared.active_fact_set_binding.fact_set_public_id,
        "fact_set_version": authorization.prepared.active_fact_set_binding.fact_set_version,
        "calculation_snapshot_id": authorization.prepared.calculation_snapshot_id,
        "calculation_snapshot_hash": authorization.prepared.calculation_snapshot_hash,
        "authorization_id": authorization.authorization_id,
        "finalization_public_id": output.finalization_public_id,
        "transaction_public_id": str(output.transaction_public_id),
        "final_transaction_created": True,
        "content_hash": content_hash,
    }, output.status == "already_finalized"


def _render_finalization_snapshot_review(
    conn: sqlite3.Connection,
    *,
    proposal_public_id: str,
    receipt_public_id: str,
    self_participant_id: str,
    prepared: PreparedReceiptCalculation,
) -> dict[str, Any]:
    """Render the personal-only view from a verified authoritative snapshot.

    The typed output has a deliberately smaller surface than the calculator's
    full internal result. It copies only verified values; it performs no
    monetary arithmetic, allocation derivation, or fallback reconstruction.
    """
    try:
        authority = read_snapshot_bound_authority(
            conn,
            snapshot_public_id=prepared.calculation_snapshot_id,
            expected_combined_hash=prepared.calculation_snapshot_hash,
        )
    except SnapshotAuthorityError as exc:
        raise _finalization_refused(
            "snapshot_review_unreadable",
            "The authoritative calculation snapshot cannot be verified for review.",
        ) from exc
    if authority is None:
        raise _finalization_refused(
            "snapshot_review_unreadable",
            "The authoritative calculation snapshot has no receipt fact-set binding.",
        )
    if (
        authority.snapshot_public_id != prepared.calculation_snapshot_id
        or authority.combined_snapshot_hash != prepared.calculation_snapshot_hash
        or authority.calculation_run_public_id != prepared.calculation_run_public_id
        or authority.receipt_group_public_id != prepared.receipt_group_public_id
        or authority.currency != prepared.currency
        or authority.active_fact_set_binding != prepared.active_fact_set_binding
        or authority.confirmed_receipt_identity != prepared.confirmed_receipt_identity
    ):
        raise _finalization_refused(
            "snapshot_review_unreadable",
            "The verified snapshot does not match the prepared receipt authority.",
        )
    output = authority.output_payload
    if not isinstance(output, dict):
        raise _finalization_refused(
            "snapshot_review_unreadable",
            "The verified snapshot output is not renderable as a receipt review.",
        )
    currency = _require_snapshot_string(output.get("currency"), label="currency", maximum=3)
    payer = _require_snapshot_string(output.get("payer"), label="payer", maximum=200)
    if currency != authority.currency or payer != self_participant_id:
        raise _finalization_refused(
            "snapshot_review_unreadable",
            "The verified snapshot output violates the personal-only review contract.",
        )
    participant_shares = output.get("participant_shares")
    if not isinstance(participant_shares, dict) or frozenset(participant_shares) != {
        self_participant_id
    }:
        raise _finalization_refused(
            "snapshot_review_unreadable",
            "The verified snapshot has unsupported participant-share cardinality.",
        )
    obligations = output.get("settlement_obligations")
    if obligations != []:
        raise _finalization_refused(
            "snapshot_review_unreadable",
            "The verified snapshot has unsupported settlement obligations.",
        )
    return {
        "identity_kind": "finalization_snapshot_review",
        "proposal_public_id": proposal_public_id,
        "receipt_public_id": receipt_public_id,
        "fact_set_public_id": authority.active_fact_set_binding.fact_set_public_id,
        "fact_set_version": authority.active_fact_set_binding.fact_set_version,
        "fact_set_result_hash": authority.active_fact_set_binding.fact_set_result_hash,
        "calculation_snapshot_id": authority.snapshot_public_id,
        "calculation_snapshot_hash": authority.combined_snapshot_hash,
        "currency": currency,
        "payer_participant_public_id": payer,
        "total_paid": _snapshot_money_display(output.get("total_paid"), currency, "total_paid"),
        "total_to_collect": _snapshot_money_display(
            output.get("total_to_collect"), currency, "total_to_collect"
        ),
        "participant_shares": {
            self_participant_id: _snapshot_money_display(
                participant_shares[self_participant_id], currency, "participant_shares"
            )
        },
        "settlement_obligations": [],
    }


def _require_snapshot_string(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _finalization_refused(
            "snapshot_review_unreadable",
            f"The verified snapshot {label} is not a safe scalar.",
        )
    return value


def _snapshot_money_display(value: object, currency: str, label: str) -> str:
    if isinstance(value, str):
        raw_decimal = value
    elif isinstance(value, dict) and frozenset(value) == {"$decimal"}:
        raw_decimal = value["$decimal"]
    else:
        raise _finalization_refused(
            "snapshot_review_unreadable",
            f"The verified snapshot {label} is not a canonical decimal.",
        )
    if not isinstance(raw_decimal, str) or len(raw_decimal) > 64:
        raise _finalization_refused(
            "snapshot_review_unreadable",
            f"The verified snapshot {label} decimal is not a safe scalar.",
        )
    try:
        amount = money_decimal(raw_decimal, label=f"snapshot {label}")
        validate_amount_for_currency(amount, currency, label=f"snapshot {label}")
        return canonical_money_str(amount, currency)
    except MoneyValidationError as exc:
        raise _finalization_refused(
            "snapshot_review_unreadable",
            f"The verified snapshot {label} decimal is invalid for its currency.",
        ) from exc


def _require_personal_receipt_classification(payload: dict[str, Any]) -> None:
    """Fail closed unless durable parser truth is a clean personal receipt."""
    classification, classification_unknown = _classification(payload)
    if classification == "shared":
        raise _finalization_refused(
            "shared_receipt_refused",
            "Shared-classification receipts are out of scope for personal-only "
            "bridge finalization.",
        )
    if classification_unknown or classification != "personal":
        raise _finalization_refused(
            "personal_only_violated",
            "Receipt proposal classification is not a clean personal expense.",
        )


def _prepare_receipt_conversion(
    conn: sqlite3.Connection,
    deadline: Deadline,
    *,
    proposal: dict[str, Any],
    payload: dict[str, Any],
    confirmed_actor: str,
    content_hash: str,
) -> tuple[str, str, str, bool]:
    """Return durable conversion lineage after the personal-only guard.

    This is deliberately the shared precondition for S5d preparation and
    existing receipt finalization. It never reaches readiness, calculation,
    authorization, or finalization.
    """
    proposal_public_id = str(proposal["public_id"])
    _require_personal_receipt_classification(payload)
    self_participant_id = _require_single_self_participant(conn)
    command_public_id, _receipt_id = _receipt_identities_for(proposal_public_id, content_hash)

    deadline.check("conversion lineage read")
    registry = read_receipt_conversion_registry(conn, command_public_id=command_public_id)
    if registry is None:
        receipt_public_id, conversion_result_hash, idempotent_replay = _run_receipt_conversion(
            conn,
            deadline,
            proposal=proposal,
            confirmed_actor=confirmed_actor,
            content_hash=content_hash,
            self_participant_id=self_participant_id,
            command_public_id=command_public_id,
        )
    else:
        # Replay reconstruction from durable truth. The conversion boundary's
        # replay integrity check is strictly valid only before fact-set
        # persistence, so later calls never re-invoke conversion.
        if registry.proposal_content_hash != content_hash:
            raise errors.bridge_error(
                errors.STALE_CONTENT_HASH,
                "Durable conversion is bound to different proposal content.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        if registry.authenticated_actor_id != confirmed_actor:
            raise errors.bridge_error(
                errors.ACTOR_MISMATCH,
                "Durable conversion actor does not match the confirmed-decision actor.",
                errors.EXIT_AUTHORITY_REFUSED,
            )
        if registry.receipt_public_id is None:
            raise _finalization_refused(
                "guard_refused",
                "Conversion registry references a destroyed receipt row.",
            )
        receipt_public_id = registry.receipt_public_id
        conversion_result_hash = registry.conversion_result_hash
        idempotent_replay = True
    return command_public_id, receipt_public_id, conversion_result_hash, idempotent_replay


def _run_receipt_conversion(
    conn: sqlite3.Connection,
    deadline: Deadline,
    *,
    proposal: dict[str, Any],
    confirmed_actor: str,
    content_hash: str,
    self_participant_id: str,
    command_public_id: str,
) -> tuple[str, str, bool]:
    """Run the guarded receipt conversion once; return (receipt, result hash).

    Invoked only while no durable conversion registry row exists for the
    deterministic command identity, i.e. strictly before any fact-set
    persistence on the receipt aggregate.
    """
    deadline.check("receipt conversion")
    command = ReceiptFactsConversionCommand.from_mapping(
        {
            "command_public_id": command_public_id,
            "proposal_public_id": str(proposal["public_id"]),
            "expected_content_hash": content_hash,
            "payer_participant_public_id": self_participant_id,
            "participants": [{"participant_public_id": self_participant_id, "is_included": True}],
            "authenticated_actor_id": confirmed_actor,
            "channel": BRIDGE_CONFIRMATION_CHANNEL,
            "actor_type": "human",
        }
    )
    try:
        conversion = convert_confirmed_receipt_proposal_to_facts(conn, command)
    except Exception as exc:
        if _sqlite_busy(exc):
            raise _finalization_locked("Receipt conversion") from exc
        if isinstance(exc, UnauthorizedConversionActorError):
            raise errors.bridge_error(
                errors.ACTOR_MISMATCH,
                f"Receipt conversion actor refused: {exc}",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc
        if isinstance(exc, StaleConfirmationHashError):
            raise errors.bridge_error(
                errors.STALE_CONTENT_HASH,
                f"Receipt conversion confirmation hash is stale: {exc}",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc
        if isinstance(exc, ConversionIdempotencyConflictError):
            raise errors.bridge_error(
                errors.IDEMPOTENCY_CONFLICT,
                f"Receipt conversion material conflict: {exc}",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc
        if isinstance(exc, ConversionStagingDatabaseRejectedError):
            raise errors.bridge_error(
                errors.STAGING_REFUSED,
                f"Receipt conversion staging database refused: {exc}",
                errors.EXIT_AUTHORITY_REFUSED,
            ) from exc
        if isinstance(exc, (IncompleteReceiptInputsError, AmbiguousReceiptInputError)):
            raise _finalization_refused("incomplete_receipt_inputs", str(exc)) from exc
        if isinstance(exc, ReceiptFactsConversionError):
            raise _finalization_refused("guard_refused", str(exc)) from exc
        raise
    return conversion.receipt_public_id, conversion.conversion_result_hash, conversion.idempotent


# ---------------------------------------------------------------------------
# authorize_finalization (S4/D2B)
# ---------------------------------------------------------------------------


def handle_authorize_finalization(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    """Persist the separate snapshot-bound human authorization (D2B).

    Reuses ``authorize_receipt_finalization`` unchanged: the prepared
    calculation is re-derived through the idempotent prepare boundary and
    the durable snapshot hash must equal the human-reviewed expected hash,
    mirroring the proven B5.1 runner authorize pattern.  No callback token
    action is involved.
    """
    _require_exact_arguments(request.arguments, required=_AUTHORIZE_FINALIZATION_REQUIRED_FIELDS)
    proposal_public_id = _require_string(
        request.arguments["proposal_public_id"], "proposal_public_id", max_length=200
    )
    operator_actor_id = _require_string(
        request.arguments["operator_actor_id"], "operator_actor_id", max_length=_MAX_ACTOR_ID_LENGTH
    )
    expected_snapshot_hash = _require_content_hash(
        request.arguments["expected_calculation_snapshot_hash"]
    )
    _require_canonical_idempotency_key(
        request, canonical_authorize_finalization_key(proposal_public_id)
    )

    _workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("authorize replay reconstruction")
        proposal = _fetch_proposal_by_public_id(conn, proposal_public_id)
        if str(proposal["parse_status"]) != CONFIRMED:
            raise _finalization_refused(
                "not_confirmed", "Only confirmed proposals can be authorized for finalization."
            )
        if not has_receipt_ocr_proposal_link(conn, int(proposal["id"])):
            raise _finalization_refused(
                "unsupported_path", "authorize_finalization supports receipt proposals only."
            )
        confirmed_actor = _require_durable_confirmed_actor(conn, proposal, operator_actor_id)
        _require_single_self_participant(conn)
        payload, _version, durable_hash = _proposal_effective_state(conn, proposal)
        _require_personal_receipt_classification(payload)
        command_public_id, receipt_public_id = _receipt_identities_for(
            proposal_public_id, durable_hash
        )

        deadline.check("fact-set readiness")
        try:
            report = report_receipt_calculator_readiness(conn, receipt_public_id)
        except ReceiptNotFoundError as exc:
            raise _finalization_refused(
                "not_converted",
                "Receipt facts do not exist yet; run finalize to convert first.",
            ) from exc
        except ReceiptFactsIntegrityError as exc:
            raise _finalization_refused(
                "fact_set_integrity_refused", f"Receipt fact-set integrity refused: {exc}"
            ) from exc
        if not report.is_calculator_ready:
            raise _finalization_refused(
                "no_authoritative_item_facts",
                "Authorization requires an existing, durable, human-authored, "
                "calculator-ready IAF fact set (D1b).",
                extra_details={
                    "receipt_public_id": receipt_public_id,
                    "conversion_command_public_id": command_public_id,
                    "not_ready_reasons": ",".join(report.not_ready_reasons),
                },
            )

        deadline.check("finalization stage read")
        stage = read_receipt_finalization_stage(conn, receipt_public_id=receipt_public_id)
        if (
            stage.calculation_snapshot_hash is not None
            and stage.calculation_snapshot_hash != expected_snapshot_hash
        ):
            # Contract §5: STALE_SNAPSHOT is zero-write.  A durable snapshot
            # already exists for this receipt, so the mismatch is refused
            # before any preparation runs.
            raise errors.bridge_error(
                errors.STALE_SNAPSHOT,
                "expected_calculation_snapshot_hash does not match the durable "
                "prepared snapshot hash; review the current snapshot and retry.",
                errors.EXIT_AUTHORITY_REFUSED,
                details={
                    "receipt_public_id": receipt_public_id,
                    "calculation_snapshot_id": stage.calculation_snapshot_id or "",
                },
            )

        deadline.check("calculation preparation")
        try:
            prepared = prepare_receipt_calculation(conn, receipt_public_id)
        except BridgePreparationError as exc:
            raise _finalization_refused(
                "not_calculator_ready", f"Calculation preparation refused: {exc}"
            ) from exc
        if prepared.calculation_snapshot_hash != expected_snapshot_hash:
            # Defensive re-check: preparation may legitimately derive a new
            # snapshot identity (for example after a guarded fact-set
            # supersession); the human-reviewed hash no longer binds.
            raise errors.bridge_error(
                errors.STALE_SNAPSHOT,
                "expected_calculation_snapshot_hash does not match the durable "
                "prepared snapshot hash; review the current snapshot and retry.",
                errors.EXIT_AUTHORITY_REFUSED,
                details={
                    "receipt_public_id": receipt_public_id,
                    "calculation_snapshot_id": prepared.calculation_snapshot_id,
                },
            )

        deadline.check("guarded authorization")
        try:
            authorization = authorize_receipt_finalization(
                conn, prepared, actor_id=confirmed_actor, actor_type="human"
            )
        except Exception as exc:
            if _sqlite_busy(exc):
                raise _finalization_locked("Finalization authorization") from exc
            if isinstance(exc, BridgeAuthorizationActorError):
                raise _finalization_refused("authorization_refused", str(exc)) from exc
            if isinstance(
                exc,
                (
                    BridgeAuthorizationConflictError,
                    BridgeBindingAuthorityError,
                    BridgeCalculationRunConflictError,
                ),
            ):
                raise _finalization_refused("authorization_conflict", str(exc)) from exc
            if isinstance(exc, BridgePreparationError):
                raise _finalization_refused("not_calculator_ready", str(exc)) from exc
            raise
        return {
            "identity_kind": "authorize_finalization",
            "receipt_public_id": receipt_public_id,
            "authorization_id": authorization.authorization_id,
            "authorization_content_hash": authorization.content_hash,
            "calculation_snapshot_id": prepared.calculation_snapshot_id,
            "calculation_snapshot_hash": prepared.calculation_snapshot_hash,
        }, False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# apply_fact_set (S4/D1b)
# ---------------------------------------------------------------------------


def handle_apply_fact_set(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    """Persist one human-authored IAF fact-set command out-of-envelope (D1b).

    The envelope carries only the bounded command filename inside the
    workspace ``commands/`` directory; item, allocation, payer, membership,
    and monetary material never enter the envelope.  The bridge enforces
    structural personal-only bindings and delegates all monetary validation
    to the existing guarded IAF boundary unchanged.
    """
    _require_exact_arguments(request.arguments, required=_APPLY_FACT_SET_REQUIRED_FIELDS)
    proposal_public_id = _require_string(
        request.arguments["proposal_public_id"], "proposal_public_id", max_length=200
    )
    operator_actor_id = _require_string(
        request.arguments["operator_actor_id"], "operator_actor_id", max_length=_MAX_ACTOR_ID_LENGTH
    )
    command_filename = workspace_access.validate_command_filename(
        request.arguments["command_filename"]
    )
    _require_canonical_idempotency_key(request, canonical_apply_fact_set_key(proposal_public_id))

    workspace, conn = _open_context(request.arguments, deadline)
    try:
        deadline.check("apply_fact_set replay reconstruction")
        proposal = _fetch_proposal_by_public_id(conn, proposal_public_id)
        if str(proposal["parse_status"]) != CONFIRMED:
            raise _finalization_refused(
                "not_confirmed", "Fact sets apply only to confirmed proposals."
            )
        if not has_receipt_ocr_proposal_link(conn, int(proposal["id"])):
            raise _finalization_refused(
                "unsupported_path", "apply_fact_set supports receipt proposals only."
            )
        confirmed_actor = _require_durable_confirmed_actor(conn, proposal, operator_actor_id)
        self_participant_id = _require_single_self_participant(conn)
        payload, _version, durable_hash = _proposal_effective_state(conn, proposal)
        _require_personal_receipt_classification(payload)
        command_public_id, receipt_public_id = _receipt_identities_for(
            proposal_public_id, durable_hash
        )

        deadline.check("conversion lineage check")
        try:
            report_receipt_calculator_readiness(conn, receipt_public_id)
        except ReceiptNotFoundError as exc:
            raise _finalization_refused(
                "not_converted",
                "Receipt facts do not exist yet; run finalize to convert first.",
            ) from exc
        except ReceiptFactsIntegrityError as exc:
            raise _finalization_refused(
                "fact_set_integrity_refused", f"Receipt fact-set integrity refused: {exc}"
            ) from exc

        deadline.check("command file read")
        raw_command = workspace_access.read_command_file(workspace, command_filename)
        try:
            command_payload = json.loads(raw_command.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                f"Command file is not valid JSON: {exc}",
                errors.EXIT_VALIDATION_REFUSED,
            ) from exc
        if not isinstance(command_payload, dict):
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                "Command file must contain a JSON object.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        try:
            command = ReceiptItemAllocationFactsCommand.from_mapping(command_payload)
        except InvalidItemFactsCommandError as exc:
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                f"Human-authored fact-set command is malformed: {exc}",
                errors.EXIT_VALIDATION_REFUSED,
            ) from exc

        _require_personal_fact_set_bindings(
            command,
            receipt_public_id=receipt_public_id,
            command_public_id=command_public_id,
            confirmed_actor=confirmed_actor,
            self_participant_id=self_participant_id,
        )

        deadline.check("guarded fact-set persistence")
        try:
            result = persist_receipt_item_allocation_facts(conn, command)
        except Exception as exc:
            if _sqlite_busy(exc):
                raise _finalization_locked("Fact-set persistence") from exc
            if isinstance(exc, UnauthorizedItemFactsActorError):
                raise errors.bridge_error(
                    errors.ACTOR_MISMATCH,
                    f"Fact-set actor refused: {exc}",
                    errors.EXIT_AUTHORITY_REFUSED,
                ) from exc
            if isinstance(
                exc,
                (
                    ItemFactsIdempotencyConflictError,
                    ItemFactSetAlreadyExistsError,
                    StaleItemFactSetVersionError,
                    StaleItemFactsReceiptBindingError,
                ),
            ):
                raise errors.bridge_error(
                    errors.IDEMPOTENCY_CONFLICT,
                    f"Fact-set command conflicts with durable truth: {exc}",
                    errors.EXIT_AUTHORITY_REFUSED,
                ) from exc
            if isinstance(exc, ItemFactsReceiptNotFoundError):
                raise _finalization_refused("not_converted", str(exc)) from exc
            if isinstance(exc, ItemFactsStagingDatabaseRejectedError):
                raise errors.bridge_error(
                    errors.STAGING_REFUSED,
                    f"Fact-set staging database refused: {exc}",
                    errors.EXIT_AUTHORITY_REFUSED,
                ) from exc
            if isinstance(exc, ReceiptItemAllocationFactsError):
                raise errors.bridge_error(
                    errors.ARGUMENTS_REFUSED,
                    f"Human-authored fact-set command refused: {exc}",
                    errors.EXIT_VALIDATION_REFUSED,
                ) from exc
            raise
        return {
            "identity_kind": "apply_fact_set",
            "receipt_public_id": receipt_public_id,
            "fact_set_public_id": result.fact_set_public_id,
            "fact_set_version": result.fact_set_version,
            "fact_set_result_hash": result.fact_set_result_hash,
            "item_count": result.item_count,
            "allocation_count": result.allocation_count,
        }, bool(result.idempotent)
    finally:
        conn.close()


def _require_personal_fact_set_bindings(
    command: ReceiptItemAllocationFactsCommand,
    *,
    receipt_public_id: str,
    command_public_id: str,
    confirmed_actor: str,
    self_participant_id: str,
) -> None:
    """Structural personal-only guard for a human-authored IAF command.

    Checks identity/lineage/actor bindings and that every allocation
    references exactly the self participant; it performs no monetary
    arithmetic — amount and reconciliation validation belong to the IAF
    boundary alone.
    """
    if command.receipt_public_id != receipt_public_id:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Fact-set command receipt_public_id does not match this proposal's receipt.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if command.expected_conversion_command_public_id != command_public_id:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Fact-set command expected_conversion_command_public_id does not match "
            "this proposal's deterministic conversion identity.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if command.authenticated_actor_id != confirmed_actor:
        raise errors.bridge_error(
            errors.ACTOR_MISMATCH,
            "Fact-set command actor does not match the durable confirmed-decision actor.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    if command.actor_type != "human":
        raise errors.bridge_error(
            errors.ACTOR_MISMATCH,
            "Fact-set command actor_type must be human.",
            errors.EXIT_AUTHORITY_REFUSED,
        )
    if command.channel != BRIDGE_CONFIRMATION_CHANNEL:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            f"Fact-set command channel must be {BRIDGE_CONFIRMATION_CHANNEL!r}.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if command.adjustments:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Bridge fact sets do not support adjustments.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    if not command.allocations:
        raise errors.bridge_error(
            errors.ARGUMENTS_REFUSED,
            "Fact-set command requires at least one allocation entry.",
            errors.EXIT_VALIDATION_REFUSED,
        )
    for allocation in command.allocations:
        participants = allocation.get("participants")
        if not isinstance(participants, (list, tuple)) or not participants:
            raise errors.bridge_error(
                errors.ARGUMENTS_REFUSED,
                "Every allocation entry must carry explicit participants.",
                errors.EXIT_VALIDATION_REFUSED,
            )
        for entry in participants:
            if not isinstance(entry, dict) or entry.get("participant_public_id") != (
                self_participant_id
            ):
                raise errors.bridge_error(
                    errors.ARGUMENTS_REFUSED,
                    "Personal-only violation: every allocation must reference exactly "
                    "the self participant.",
                    errors.EXIT_VALIDATION_REFUSED,
                )


def dispatch(request: BridgeRequest, deadline: Deadline) -> HandlerResult:
    from finance_core.openclaw_staging_bridge import envelope

    handlers: dict[str, Callable[[BridgeRequest, Deadline], HandlerResult]] = {
        envelope.COMMAND_HEALTH: handle_health,
        envelope.COMMAND_GET_STATUS: handle_get_status,
        envelope.COMMAND_CAPTURE: handle_capture,
        envelope.COMMAND_PROCESS_CAPTURE_JOB: handle_process_capture_job,
        envelope.COMMAND_PROPOSE: handle_propose,
        envelope.COMMAND_GET_REVIEW: handle_get_review,
        envelope.COMMAND_CONFIRM: handle_confirm,
        envelope.COMMAND_EDIT: handle_edit,
        envelope.COMMAND_REJECT: handle_reject,
        envelope.COMMAND_ISSUE_HUMAN_ACTIONS: handle_issue_human_actions,
        envelope.COMMAND_REDEEM_HUMAN_ACTION: handle_redeem_human_action,
        envelope.COMMAND_GET_GUIDED_EDIT_SESSION: handle_get_guided_edit_session,
        envelope.COMMAND_APPLY_GUIDED_EDIT_UPDATE: handle_apply_guided_edit_update,
        envelope.COMMAND_COMPLETE_GUIDED_EDIT: handle_complete_guided_edit,
        envelope.COMMAND_APPLY_HUMAN_DRAFT_CARD: handle_apply_human_draft_card,
        envelope.COMMAND_GET_HUMAN_DRAFT_CARD: handle_get_human_draft_card,
        envelope.COMMAND_BEGIN_HUMAN_DRAFT_CARD_DELIVERY: (handle_begin_human_draft_card_delivery),
        envelope.COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME: (
            handle_record_human_draft_card_delivery_outcome
        ),
        envelope.COMMAND_REISSUE_HUMAN_DRAFT_CARD: handle_reissue_human_draft_card,
        envelope.COMMAND_PREPARE_POSTING_REVIEW: handle_prepare_posting_review,
        envelope.COMMAND_ISSUE_POSTING_REVIEW_ACTIONS: handle_issue_posting_review_actions,
        envelope.COMMAND_CONFIRM_AND_POST: handle_confirm_and_post,
        envelope.COMMAND_RESUME_POSTING: handle_resume_posting,
        envelope.COMMAND_FINALIZE: handle_finalize,
        envelope.COMMAND_PREPARE_RECEIPT_COMPLETION: handle_prepare_receipt_completion,
        envelope.COMMAND_GET_FINALIZATION_SNAPSHOT_REVIEW: handle_get_finalization_snapshot_review,
        envelope.COMMAND_AUTHORIZE_FINALIZATION: handle_authorize_finalization,
        envelope.COMMAND_APPLY_FACT_SET: handle_apply_fact_set,
        envelope.COMMAND_PREPARE_AI_FALLBACK: handle_prepare_ai_fallback,
        envelope.COMMAND_CLAIM_AI_FALLBACK_INVOCATION: handle_claim_ai_fallback_invocation,
        envelope.COMMAND_RECORD_AI_FALLBACK_RESULT: handle_record_ai_fallback_result,
        envelope.COMMAND_VERIFY_AI_MODEL_COMPATIBILITY_CASE_V2: (
            handle_verify_ai_model_compatibility_case_v2
        ),
        envelope.COMMAND_REGISTER_AI_MODEL_COMPATIBILITY_RECEIPT_V2: (
            handle_register_ai_model_compatibility_receipt_v2
        ),
        envelope.COMMAND_PREPARE_AI_FALLBACK_V2: handle_prepare_ai_fallback_v2,
        envelope.COMMAND_CLAIM_AI_FALLBACK_INVOCATION_V2: (handle_claim_ai_fallback_invocation_v2),
        envelope.COMMAND_RECORD_AI_FALLBACK_RESULT_V2: handle_record_ai_fallback_result_v2,
        envelope.COMMAND_GET_AI_PROCESSING_STATUS_V2: handle_get_ai_processing_status_v2,
    }
    handler = handlers.get(request.command)
    if handler is None:
        raise errors.bridge_error(
            errors.UNKNOWN_COMMAND,
            f"Command is not allowlisted: {request.command!r}",
            errors.EXIT_UNKNOWN_COMMAND,
        )
    return handler(request, deadline)


__all__ = [
    "BRIDGE_CONFIRMATION_CHANNEL",
    "Deadline",
    "canonical_claim_ai_fallback_key",
    "canonical_apply_fact_set_key",
    "canonical_authorize_finalization_key",
    "canonical_finalize_key",
    "canonical_finalization_snapshot_review_key",
    "canonical_prepare_receipt_completion_key",
    "canonical_human_action_issuance_key",
    "canonical_human_action_redemption_key",
    "canonical_prepare_posting_review_key",
    "canonical_prepare_initial_posting_review_key",
    "canonical_issue_posting_review_actions_key",
    "canonical_confirm_and_post_key",
    "canonical_resume_posting_key",
    "canonical_guided_edit_update_key",
    "canonical_guided_edit_complete_key",
    "canonical_human_draft_apply_key",
    "canonical_human_draft_delivery_key",
    "canonical_human_draft_observation_key",
    "canonical_human_draft_reissue_key",
    "canonical_prepare_ai_fallback_key",
    "canonical_record_ai_fallback_key",
    "canonical_register_ai_model_receipt_v2_key",
    "canonical_prepare_ai_fallback_v2_key",
    "canonical_claim_ai_fallback_v2_key",
    "canonical_record_ai_fallback_v2_key",
    "dispatch",
    "handle_apply_fact_set",
    "handle_apply_human_draft_card",
    "handle_begin_human_draft_card_delivery",
    "handle_authorize_finalization",
    "handle_capture",
    "handle_claim_ai_fallback_invocation",
    "handle_confirm",
    "handle_edit",
    "handle_finalize",
    "handle_get_finalization_snapshot_review",
    "handle_get_human_draft_card",
    "handle_record_human_draft_card_delivery_outcome",
    "handle_reissue_human_draft_card",
    "handle_prepare_receipt_completion",
    "handle_get_review",
    "handle_get_status",
    "handle_health",
    "handle_issue_human_actions",
    "handle_prepare_posting_review",
    "handle_issue_posting_review_actions",
    "handle_confirm_and_post",
    "handle_resume_posting",
    "handle_propose",
    "handle_prepare_ai_fallback",
    "handle_record_ai_fallback_result",
    "handle_verify_ai_model_compatibility_case_v2",
    "handle_register_ai_model_compatibility_receipt_v2",
    "handle_prepare_ai_fallback_v2",
    "handle_claim_ai_fallback_invocation_v2",
    "handle_record_ai_fallback_result_v2",
    "handle_get_ai_processing_status_v2",
    "handle_reject",
    "handle_redeem_human_action",
    "handle_get_guided_edit_session",
    "handle_apply_guided_edit_update",
    "handle_complete_guided_edit",
]
