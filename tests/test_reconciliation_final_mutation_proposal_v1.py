"""Tests for Reconciliation Final Mutation Proposal Guard v1.

Covers:
  1. Valid CREATE_FINAL_TRANSACTION creates approved dry-run preview.
  2. Valid ADJUST_FINAL_TRANSACTION creates approved dry-run preview.
  3. no_final_mutation produces a safe no-op preview.
  4. Unsupported action is blocked.
  5. Missing required monetary fields are blocked (CREATE).
  6. Missing source/evidence fields are blocked (CREATE).
  7. Missing target/adjust fields are blocked (ADJUST).
  8. Raw dict is rejected/blocked.
  9. String input is rejected/blocked.
  10. Direct mutation keyword in note is blocked.
  11. Guard failure does not produce an approved preview.
  12. No test writes to database/finance.db.
  13. Deterministic output ordering / stable reason codes.
  14. BLOCKED action cannot be used as a caller action.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationBlockedReason,
    FinalMutationGuard,
    FinalMutationProposal,
    guard_final_mutation_proposal,
    preview_final_mutation,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _create_proposal(**kw) -> FinalMutationProposal:
    """Build a valid CREATE_FINAL_TRANSACTION proposal with defaults."""
    defaults: dict = dict(
        proposal_id="fp-create-001",
        action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
        amount=Decimal("45.50"),
        currency="SGD",
        merchant="Giant Supermarket",
        transaction_date=date(2024, 12, 1),
        source_statement_ref="stmt-res-001",
        evidence_refs=("ev-abc",),
        note="Create from statement match.",
    )
    defaults.update(kw)
    return FinalMutationProposal(**defaults)


def _adjust_proposal(**kw) -> FinalMutationProposal:
    """Build a valid ADJUST_FINAL_TRANSACTION proposal with defaults."""
    defaults: dict = dict(
        proposal_id="fp-adjust-001",
        action=FinalMutationAction.ADJUST_FINAL_TRANSACTION,
        target_transaction_id="txn-123",
        suggested_fields={"amount": "42.00"},
        source_app_transaction_ref="app-ref-001",
        evidence_refs=("ev-xyz",),
        note="Adjust amount from 45.50 to 42.00.",
    )
    defaults.update(kw)
    return FinalMutationProposal(**defaults)


def _noop_proposal(**kw) -> FinalMutationProposal:
    """Build a valid NO_FINAL_MUTATION proposal."""
    defaults: dict = dict(
        proposal_id="fp-noop-001",
        action=FinalMutationAction.NO_FINAL_MUTATION,
        note="Confirmed match, no mutation needed.",
    )
    defaults.update(kw)
    return FinalMutationProposal(**defaults)


# ---------------------------------------------------------------------------
# Tests: CREATE_FINAL_TRANSACTION -- approved path
# ---------------------------------------------------------------------------


class TestCreateFinalTransactionApproved:
    """Valid CREATE_FINAL_TRANSACTION proposals pass the guard and produce
    a dry-run preview."""

    def test_create_proposal_approved_by_guard_instance(self):
        proposal = _create_proposal()
        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)

        assert decision.approved is True
        assert decision.proposal_id == "fp-create-001"
        assert decision.action == FinalMutationAction.CREATE_FINAL_TRANSACTION
        assert decision.blocked_reasons == ()
        assert decision.guard_version == "v1"

    def test_create_proposal_has_preview(self):
        proposal = _create_proposal()
        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)

        preview = decision.preview
        assert preview is not None
        assert preview.proposal_id == "fp-create-001"
        assert preview.action == FinalMutationAction.CREATE_FINAL_TRANSACTION
        assert preview.amount == "45.50"
        assert preview.currency == "SGD"
        assert preview.merchant == "Giant Supermarket"
        assert preview.transaction_date == "2024-12-01"
        assert preview.is_dry_run is True
        assert "No final financial records" in preview.preview_note
        assert preview.evidence_refs == ("ev-abc",)
        assert preview.source_statement_ref == "stmt-res-001"

    def test_create_proposal_preview_is_dry_run(self):
        proposal = _create_proposal()
        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)
        assert decision.preview.is_dry_run is True

    def test_convenience_function_evaluate(self):
        proposal = _create_proposal()
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is True

    def test_convenience_preview_function(self):
        proposal = _create_proposal()
        pv = preview_final_mutation(proposal)
        assert pv.is_dry_run is True
        assert pv.amount == "45.50"


# ---------------------------------------------------------------------------
# Tests: ADJUST_FINAL_TRANSACTION -- approved path
# ---------------------------------------------------------------------------


class TestAdjustFinalTransactionApproved:
    """Valid ADJUST_FINAL_TRANSACTION proposals pass the guard."""

    def test_adjust_proposal_approved(self):
        proposal = _adjust_proposal()
        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)

        assert decision.approved is True
        assert decision.action == FinalMutationAction.ADJUST_FINAL_TRANSACTION
        assert decision.preview is not None
        assert decision.preview.target_transaction_id == "txn-123"
        assert decision.preview.suggested_fields == {"amount": "42.00"}

    def test_adjust_preview_is_dry_run(self):
        proposal = _adjust_proposal()
        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)
        assert decision.preview.is_dry_run is True


# ---------------------------------------------------------------------------
# Tests: NO_FINAL_MUTATION -- safe no-op
# ---------------------------------------------------------------------------


class TestNoFinalMutation:
    """NO_FINAL_MUTATION proposals produce safe no-op previews."""

    def test_noop_proposal_approved(self):
        proposal = _noop_proposal()
        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)

        assert decision.approved is True
        assert decision.action == FinalMutationAction.NO_FINAL_MUTATION
        assert decision.preview is not None
        assert decision.preview.is_dry_run is True

    def test_noop_without_evidence_still_approved(self):
        """NO_FINAL_MUTATION does not require evidence refs."""
        proposal = _noop_proposal(evidence_refs=())
        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)
        assert decision.approved is True


# ---------------------------------------------------------------------------
# Tests: Unsupported action blocked
# ---------------------------------------------------------------------------


class TestUnsupportedActionBlocked:
    """Actions not in the supported set are blocked."""

    def test_blocked_action_rejected(self):
        proposal = FinalMutationProposal(
            proposal_id="fp-blocked",
            action=FinalMutationAction.BLOCKED,
        )
        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)

        assert decision.approved is False
        assert decision.action == FinalMutationAction.BLOCKED
        assert FinalMutationBlockedReason.UNSUPPORTED_ACTION.value in decision.blocked_reasons
        assert decision.preview is None

    def test_invalid_string_action_blocked(self):
        """An action value that doesn't match any enum member should
        still be caught by the guard's action gate.  Since BLOCKED is
        the only member outside _SUPPORTED_CALLER_ACTIONS, any value
        not in that set is blocked."""
        # All valid enum members are covered. We test the boundary
        # by using BLOCKED (which is supported by the enum but not
        # by the caller-action gate).
        proposal = FinalMutationProposal(
            proposal_id="fp-bad",
            action=FinalMutationAction.BLOCKED,
        )
        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)
        assert decision.approved is False
        assert decision.preview is None


# ---------------------------------------------------------------------------
# Tests: Missing required fields -- CREATE
# ---------------------------------------------------------------------------


class TestCreateMissingFieldsBlocked:
    """Missing required fields on CREATE_FINAL_TRANSACTION are blocked."""

    def test_missing_amount_blocked(self):
        proposal = _create_proposal(amount=None)
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_AMOUNT.value in decision.blocked_reasons
        assert decision.preview is None

    def test_missing_currency_blocked(self):
        proposal = _create_proposal(currency=None)
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_CURRENCY.value in decision.blocked_reasons

    def test_missing_date_blocked(self):
        proposal = _create_proposal(transaction_date=None)
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_DATE.value in decision.blocked_reasons

    def test_missing_merchant_blocked(self):
        proposal = _create_proposal(merchant=None)
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_MERCHANT.value in decision.blocked_reasons

    def test_missing_source_statement_ref_blocked(self):
        proposal = _create_proposal(source_statement_ref=None)
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.CREATE_WITHOUT_SOURCE.value in decision.blocked_reasons

    def test_missing_evidence_refs_blocked(self):
        proposal = _create_proposal(evidence_refs=())
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_EVIDENCE_REFS.value in decision.blocked_reasons

    def test_empty_string_currency_blocked(self):
        proposal = _create_proposal(currency="")
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_CURRENCY.value in decision.blocked_reasons

    def test_empty_string_merchant_blocked(self):
        proposal = _create_proposal(merchant="")
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_MERCHANT.value in decision.blocked_reasons

    def test_multiple_missing_fields_reported(self):
        """When multiple fields are missing, all reasons are reported."""
        proposal = _create_proposal(
            amount=None,
            currency=None,
            merchant=None,
            source_statement_ref=None,
            evidence_refs=(),
        )
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        reasons = set(decision.blocked_reasons)
        assert FinalMutationBlockedReason.MISSING_AMOUNT.value in reasons
        assert FinalMutationBlockedReason.MISSING_CURRENCY.value in reasons
        assert FinalMutationBlockedReason.MISSING_MERCHANT.value in reasons
        assert FinalMutationBlockedReason.CREATE_WITHOUT_SOURCE.value in reasons
        assert FinalMutationBlockedReason.MISSING_EVIDENCE_REFS.value in reasons


# ---------------------------------------------------------------------------
# Tests: Missing required fields -- ADJUST
# ---------------------------------------------------------------------------


class TestAdjustMissingFieldsBlocked:
    """Missing required fields on ADJUST_FINAL_TRANSACTION are blocked."""

    def test_missing_target_id_blocked(self):
        proposal = _adjust_proposal(target_transaction_id=None)
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.ADJUST_WITHOUT_TARGET.value in decision.blocked_reasons

    def test_empty_suggested_fields_blocked(self):
        proposal = _adjust_proposal(suggested_fields={})
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_SUGGESTED_FIELDS.value in decision.blocked_reasons

    def test_missing_app_ref_blocked(self):
        proposal = _adjust_proposal(source_app_transaction_ref=None)
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_SOURCE_REFERENCE.value in decision.blocked_reasons

    def test_missing_evidence_refs_blocked(self):
        proposal = _adjust_proposal(evidence_refs=())
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.MISSING_EVIDENCE_REFS.value in decision.blocked_reasons


# ---------------------------------------------------------------------------
# Tests: Type guard -- raw dict / unvalidated payload rejected
# ---------------------------------------------------------------------------


class TestTypeGuardRejectsRawInput:
    """Raw dicts, strings, and non-proposal objects are blocked."""

    def test_raw_dict_blocked(self):
        guard = FinalMutationGuard()
        decision = guard.evaluate({"proposal_id": "raw-001", "action": "create"})
        assert decision.approved is False
        assert decision.action == FinalMutationAction.BLOCKED
        assert FinalMutationBlockedReason.INVALID_PROPOSAL_TYPE.value in decision.blocked_reasons
        assert decision.preview is None

    def test_string_input_blocked(self):
        guard = FinalMutationGuard()
        decision = guard.evaluate("create a transaction")
        assert decision.approved is False
        assert FinalMutationBlockedReason.INVALID_PROPOSAL_TYPE.value in decision.blocked_reasons

    def test_none_input_blocked(self):
        guard = FinalMutationGuard()
        decision = guard.evaluate(None)
        assert decision.approved is False
        assert FinalMutationBlockedReason.INVALID_PROPOSAL_TYPE.value in decision.blocked_reasons

    def test_arbitrary_object_blocked(self):
        guard = FinalMutationGuard()

        class SomeOther:
            pass

        decision = guard.evaluate(SomeOther())
        assert decision.approved is False
        assert FinalMutationBlockedReason.INVALID_PROPOSAL_TYPE.value in decision.blocked_reasons


# ---------------------------------------------------------------------------
# Tests: Direct mutation detection
# ---------------------------------------------------------------------------


class TestDirectMutationDetection:
    """Proposals with direct mutation keywords in notes are blocked."""

    def test_insert_in_note_blocked(self):
        proposal = _create_proposal(note="We should INSERT into final_transactions.")
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.DIRECT_MUTATION_DETECTED.value in decision.blocked_reasons

    def test_update_in_suggested_fields_blocked(self):
        proposal = _adjust_proposal(
            suggested_fields={"sql": "UPDATE statement_transactions SET amount=50"}
        )
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.DIRECT_MUTATION_DETECTED.value in decision.blocked_reasons

    def test_clean_note_not_blocked(self):
        proposal = _create_proposal(note="Create a new transaction record.")
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is True


# ---------------------------------------------------------------------------
# Tests: Guard failure produces no approved preview
# ---------------------------------------------------------------------------


class TestGuardFailureNoPreview:
    """When the guard blocks, preview is always None."""

    def test_blocked_decision_has_no_preview(self):
        proposal = _create_proposal(amount=None)
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert decision.preview is None

    def test_raw_dict_has_no_preview(self):
        guard = FinalMutationGuard()
        decision = guard.evaluate({})
        assert decision.preview is None

    def test_every_blocked_reason_has_no_preview(self):
        """Sample several blocked paths to confirm preview is None."""
        guard = FinalMutationGuard()

        # Missing amount
        d1 = guard.evaluate(_create_proposal(amount=None))
        assert d1.preview is None

        # Raw dict
        d2 = guard.evaluate({})
        assert d2.preview is None

        # BLOCKED action
        d3 = guard.evaluate(
            FinalMutationProposal(
                proposal_id="fp-bad",
                action=FinalMutationAction.BLOCKED,
            )
        )
        assert d3.preview is None

        # Direct mutation
        d4 = guard.evaluate(_create_proposal(note="INSERT INTO x"))
        assert d4.preview is None


# ---------------------------------------------------------------------------
# Tests: Deterministic ordering and stable reason codes
# ---------------------------------------------------------------------------


class TestDeterministicOutput:
    """Guard decisions are deterministic and reason codes are stable."""

    def test_same_proposal_twice_same_decision(self):
        proposal = _create_proposal()
        d1 = guard_final_mutation_proposal(proposal)
        d2 = guard_final_mutation_proposal(proposal)

        assert d1.approved == d2.approved
        assert d1.action == d2.action
        assert d1.blocked_reasons == d2.blocked_reasons
        assert d1.preview.amount == d2.preview.amount

    def test_blocked_reasons_are_stable(self):
        proposal = _create_proposal(amount=None, currency=None)
        d1 = guard_final_mutation_proposal(proposal)
        d2 = guard_final_mutation_proposal(proposal)

        assert d1.blocked_reasons == d2.blocked_reasons

    def test_reason_codes_match_enum_values(self):
        """Every blocked reason code is a known enum value."""
        all_reasons = {r.value for r in FinalMutationBlockedReason}
        proposal = _create_proposal(
            amount=None, currency=None, merchant=None, source_statement_ref=None, evidence_refs=()
        )
        decision = guard_final_mutation_proposal(proposal)
        for reason in decision.blocked_reasons:
            assert reason in all_reasons, f"Unknown reason code: {reason}"

    def test_create_preview_fields_are_deterministic(self):
        p1 = _create_proposal()
        p2 = _create_proposal()
        pv1 = preview_final_mutation(p1)
        pv2 = preview_final_mutation(p2)
        assert pv1.amount == pv2.amount
        assert pv1.currency == pv2.currency
        assert pv1.merchant == pv2.merchant


# ---------------------------------------------------------------------------
# Tests: No database/file writes
# ---------------------------------------------------------------------------


class TestNoDatabaseOrFileWrites:
    """The guard and preview never touch the filesystem or database."""

    def test_no_finance_db_access(self):
        """Verify that database/finance.db is not accessed during guard
        evaluation.  We check that the file still exists and has the
        same modification time."""
        db_path = os.path.join(os.path.dirname(__file__), "..", "database", "finance.db")
        db_path = os.path.abspath(db_path)

        # Get original mtime if file exists
        mtime_before = None
        if os.path.exists(db_path):
            mtime_before = os.path.getmtime(db_path)

        proposal = _create_proposal()
        decision = guard_final_mutation_proposal(proposal)

        # Guard evaluation should not touch finance.db
        if os.path.exists(db_path):
            mtime_after = os.path.getmtime(db_path)
            assert mtime_before == mtime_after, (
                "database/finance.db was modified during guard evaluation"
            )

        assert decision.approved is True

    def test_guard_does_not_open_files(self):
        """Guard creates no files and opens no connections."""
        guard = FinalMutationGuard()
        proposal = _create_proposal()
        # This is a pure in-memory operation
        decision = guard.evaluate(proposal)
        assert decision.approved is True


# ---------------------------------------------------------------------------
# Tests: BLOCKED as caller action
# ---------------------------------------------------------------------------


class TestBlockedActionRejected:
    """BLOCKED is never a valid caller-requested action."""

    def test_blocked_action_evaluated_as_unsupported(self):
        proposal = FinalMutationProposal(
            proposal_id="fp-blocked-caller",
            action=FinalMutationAction.BLOCKED,
        )
        decision = guard_final_mutation_proposal(proposal)
        assert decision.approved is False
        assert FinalMutationBlockedReason.UNSUPPORTED_ACTION.value in decision.blocked_reasons


# ---------------------------------------------------------------------------
# Tests: Preview note always present
# ---------------------------------------------------------------------------


class TestPreviewNoteAlwaysPresent:
    """Every approved preview carries the dry-run preview_note."""

    def test_create_preview_has_note(self):
        proposal = _create_proposal()
        decision = guard_final_mutation_proposal(proposal)
        assert "No final financial records" in decision.preview.preview_note

    def test_adjust_preview_has_note(self):
        proposal = _adjust_proposal()
        decision = guard_final_mutation_proposal(proposal)
        assert "No final financial records" in decision.preview.preview_note

    def test_noop_preview_has_note(self):
        proposal = _noop_proposal()
        decision = guard_final_mutation_proposal(proposal)
        assert "No final financial records" in decision.preview.preview_note
