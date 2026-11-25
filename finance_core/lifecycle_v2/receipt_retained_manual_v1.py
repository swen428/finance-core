"""Reconstruct one manual receipt from the original replies at a verified ledger prefix."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .contracts import LifecycleError, VerifiedReceiptSourceV1
from .daily_v1.contracts import sha, text
from .receipt_draft_v1 import (
    MAX_RECEIPT_MANUAL_REPLIES_V1,
    VerifiedReceiptManualReplyV1,
    _copy_reply,
    _project_manual_draft,
)
from .receipt_expense_candidate_v1 import (
    ReceiptExpenseConfirmationPreviewV1,
    _compile_receipt_expense_candidate,
)
from .receipt_review_fields_v1 import (
    VerifiedReceiptReviewFieldsReplyV1,
    _complete_review_fields,
    _copy_review_fields_reply,
)
from .receipt_review_preparation_v1 import (
    VerifiedReceiptPaymentV1,
    _copy_payment,
    _prepare_payment,
    _require_payment,
)
from .receipt_snapshot_v1 import _receipt_lookup, _receipt_snapshot_for


@dataclass(frozen=True)
class ReceiptJournalReplyRefV1:
    """Exact authenticated response and update in the original transport journal."""

    sequence: int
    response_id: str
    response_sha256: str
    message_id: int
    update_id: int

    def __post_init__(self):
        text(self.response_id, "receipt journal response id", 512)
        sha(self.response_sha256)
        if (
            type(self.sequence) is not int
            or not 0 < self.sequence < 2**63
            or type(self.message_id) is not int
            or not 0 < self.message_id < 2**63
            or type(self.update_id) is not int
            or not 0 <= self.update_id < 2**63
        ):
            raise LifecycleError("Exact receipt journal reply identity required")


@dataclass(frozen=True)
class VerifiedReceiptManualHistoryV1:
    """A receipt and its original related questions and replies at a journal cut.

    The installed port authenticates the original source message, all quoted
    questions, and their replies from the retained journal through the supplied
    fields update. It resolves the manual draft revisions and question targets
    from that journal prefix. `reply_positions` contains the manual replies in
    order, then selection, debit and fields. The last position is the lookup key.
    """

    source: VerifiedReceiptSourceV1
    source_message: ReceiptJournalReplyRefV1
    manual_replies: tuple[VerifiedReceiptManualReplyV1, ...]
    payment: VerifiedReceiptPaymentV1
    review_fields: VerifiedReceiptReviewFieldsReplyV1
    reply_positions: tuple[ReceiptJournalReplyRefV1, ...]


@dataclass(frozen=True)
class ReceiptLedgerCutV1:
    """Inclusive tail sequence and hash of an already audited Core ledger prefix."""

    event_seq: int
    event_sha256: str

    def __post_init__(self):
        if type(self.event_seq) is not int or not 0 < self.event_seq < 2**63 - 1:
            raise LifecycleError("Exact retained receipt ledger cut sequence required")
        sha(self.event_sha256)


@dataclass(frozen=True)
class RetainedManualReceiptExpenseV1:
    """Readback of the reviewed expense and its precise input evidence."""

    ledger_cut: ReceiptLedgerCutV1
    history: VerifiedReceiptManualHistoryV1
    preview: ReceiptExpenseConfirmationPreviewV1


def _copy_position(value):
    if type(value) is not ReceiptJournalReplyRefV1:
        raise LifecycleError("Exact receipt journal reply identity required")
    return replace(value)


def _read_history(port, conn, binding, original_evidence_id, fields_reply):
    lookup = _copy_position(fields_reply)
    requested = _copy_position(lookup)
    with _receipt_lookup(port, conn, binding) as read:
        actual = read("receipt_retained_manual_history_v1", original_evidence_id, requested)
        if requested != lookup:
            raise LifecycleError("Receipt callback changed its exact reply lookup")
        if (
            type(actual) is not VerifiedReceiptManualHistoryV1
            or type(actual.source) is not VerifiedReceiptSourceV1
            or type(actual.manual_replies) is not tuple
            or not 1 <= len(actual.manual_replies) <= MAX_RECEIPT_MANUAL_REPLIES_V1
            or type(actual.reply_positions) is not tuple
            or len(actual.reply_positions) != len(actual.manual_replies) + 3
        ):
            raise LifecycleError("Exact retained manual receipt history required")
        return VerifiedReceiptManualHistoryV1(
            replace(actual.source),
            _copy_position(actual.source_message),
            tuple(_copy_reply(reply) for reply in actual.manual_replies),
            _copy_payment(actual.payment),
            _copy_review_fields_reply(actual.review_fields),
            tuple(_copy_position(ref) for ref in actual.reply_positions),
        )


def _require_transport_prefix(history, source, lookup):
    if history.source != source or history.reply_positions[-1] != lookup:
        raise LifecycleError("Retained receipt history differs from source or reply cut")
    payment = history.payment
    review = history.review_fields.reply
    manual = history.manual_replies
    replies = (*manual, payment.selection, payment.debit, review)
    for ref, reply in zip(history.reply_positions, replies, strict=True):
        if (ref.response_id, ref.response_sha256, ref.message_id, ref.update_id) != (
            reply.response_id,
            reply.response_sha256,
            reply.reply_message_id,
            reply.reply_update_id,
        ):
            raise LifecycleError("Receipt journal reply position differs from original evidence")
    positions = (history.source_message, *history.reply_positions)
    manual_question = (manual[0].question_message_id,) if manual else ()
    questions = (
        *manual_question,
        payment.selection.question_message_id,
        payment.debit.question_message_id,
        review.question_message_id,
    )
    messages = (*questions, *(item.message_id for item in positions))
    if len(set(messages)) != len(messages):
        raise LifecycleError("Retained receipt questions and replies require distinct messages")
    envelopes = {}
    for ref in positions:
        position = (ref.sequence, ref.response_sha256)
        if envelopes.setdefault(ref.response_id, position) != position:
            raise LifecycleError("Receipt journal response has conflicting retained positions")
    for index, (left, right) in enumerate(zip(positions, positions[1:])):
        if (
            left.sequence > right.sequence
            or left.update_id >= right.update_id
            or (
                left.sequence == right.sequence
                and (not 1 <= index < len(manual) or left.response_id != right.response_id)
            )
        ):
            raise LifecycleError("Receipt replies are outside the exact journal prefix")


def _ledger_prefix(conn, source_id, ledger_cut, *, _rows=None):
    if ledger_cut is None:
        event = conn.execute(
            "SELECT seq,event_sha256 FROM l2_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if event is None:
            raise LifecycleError("Receipt requires an existing ledger capture")
        ledger_cut = ReceiptLedgerCutV1(*event)
    elif type(ledger_cut) is not ReceiptLedgerCutV1:
        raise LifecycleError("Exact retained ledger cut required")
    else:
        ledger_cut = replace(ledger_cut)
        event = (
            _rows.project(conn, "event", ("event_sha256",), ledger_cut.event_seq)
            if _rows is not None
            else conn.execute(
                "SELECT event_sha256 FROM l2_events WHERE seq=?", (ledger_cut.event_seq,)
            ).fetchone()
        )
        if event is None or event[0] != ledger_cut.event_sha256:
            raise LifecycleError("Receipt differs from the exact retained ledger cut")
    capture = (
        _rows.project_many(conn, "entity_events", ("seq",), "capture", source_id)
        if _rows is not None
        else conn.execute(
            "SELECT seq FROM l2_events WHERE kind='capture' AND entity_id=?", (source_id,)
        ).fetchall()
    )
    if len(capture) != 1 or capture[0][0] > ledger_cut.event_seq:
        raise LifecycleError("Original receipt capture is outside the ledger cut")
    return ledger_cut


def _receipt_history_origin(service, conn, source_id, fields_reply, ledger_cut, *, _rows=None):
    """Resolve the original receipt and exact account history cut without a nested audit."""
    from .daily_v1.accounts import _EvidenceReadPort

    scope = _receipt_snapshot_for(conn, service.port, service.binding)
    if not conn.in_transaction or (scope is None and type(service.port) is not _EvidenceReadPort):
        raise LifecycleError("Receipt history requires a protected ledger or exact snapshot cut")
    if scope is not None and ledger_cut is None:
        raise LifecycleError("Receipt snapshot requires the original ledger event predecessor")
    text(source_id, "original receipt source id", 512)
    lookup = _copy_position(fields_reply)
    cut = _ledger_prefix(conn, source_id, ledger_cut, _rows=_rows)
    source = service._receipt_source(conn, source_id, allow_archived=False)
    return cut, source, lookup


def _compile_retained_expense(service, conn, source, history, draft, cut, *, _rows=None):
    payment = _require_payment(source, draft, history.payment)
    preparation, opening = _prepare_payment(
        service.binding, conn, source, draft, payment, before_seq=cut.event_seq + 1, _rows=_rows
    )
    fields = _complete_review_fields(preparation, history.review_fields, opening)
    if fields.missing_movement_fields:
        raise LifecycleError("Complete retained receipt review fields required")
    return ReceiptExpenseConfirmationPreviewV1(
        _compile_receipt_expense_candidate(fields), preparation.payment.account
    )


def _reconstruct_manual_expense(
    service, conn, source_id, fields_reply, *, ledger_cut=None, _rows=None
) -> RetainedManualReceiptExpenseV1:
    """Read on the caller's audited connection without reentering the audit.

    A historical caller derives its ledger cut from an independently checked
    retained event. A current read lets Core derive the cut directly.
    """
    cut, source, lookup = _receipt_history_origin(
        service, conn, source_id, fields_reply, ledger_cut, _rows=_rows
    )
    history = _read_history(service.port, conn, service.binding, source.evidence_id, lookup)
    _require_transport_prefix(history, source, lookup)
    draft = _project_manual_draft(source_id, source, history.manual_replies)
    if draft.missing_receipt_fields:
        raise LifecycleError("Complete retained manual receipt draft required")
    preview = _compile_retained_expense(service, conn, source, history, draft, cut, _rows=_rows)
    return RetainedManualReceiptExpenseV1(cut, history, preview)
