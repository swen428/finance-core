"""Typed manual receipt evidence for the shared daily expense lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import ClassVar

from .contracts import LifecycleError, ProposalRef, RuntimeBinding, canonical, digest
from .daily_v1._impacts_v1 import account_inventory
from .daily_v1.accounts import MAX_PROPOSALS, _require_current, _require_prefix
from .daily_v1.contracts import exact, sha, strict_json, text
from .daily_v1.movement_contracts import movement_record_id_v1
from .receipt_retained_manual_v1 import (
    ReceiptJournalReplyRefV1,
    ReceiptLedgerCutV1,
    RetainedManualReceiptExpenseV1,
    _copy_position,
    _reconstruct_manual_expense,
)
from .receipt_retained_ocr_v1 import (
    RetainedOcrReceiptExpenseV1,
    VerifiedReceiptOcrHistoryV1,
    _reconstruct_ocr_expense,
)
from .receipt_snapshot_v1 import _receipt_lookup
from .service import _event, _id

OBSERVATION_FORMAT = "finance-daily-manual-receipt-observation-v1"
REVIEW_FORMAT = "finance-daily-manual-receipt-review-v1"
OCR_OBSERVATION_FORMAT = "finance-daily-ocr-receipt-observation-v1"
OCR_REVIEW_FORMAT = "finance-daily-ocr-receipt-review-v1"
OBSERVATION_FORMATS = frozenset({OBSERVATION_FORMAT, OCR_OBSERVATION_FORMAT})


def _has_receipt_proposals(conn):
    return (
        conn.execute(
            "SELECT 1 FROM l2_proposals WHERE json_extract(observation_json,'$.format') "
            "IN (?,?) LIMIT 1",
            (OBSERVATION_FORMAT, OCR_OBSERVATION_FORMAT),
        ).fetchone()
        is not None
    )


def _integer(value, label):
    if type(value) is not int or not 0 < value < 2**63:
        raise LifecycleError("Exact " + label + " required")


@dataclass(frozen=True)
class VerifiedReceiptCardDeliveryV1:
    """Original authenticated delivery of the exact Core card."""

    evidence_id: str
    review_id: str
    display_sha256: str
    principal_id: str
    client_id: str
    sequence: int
    response_id: str
    response_sha256: str
    message_id: int
    timestamp: int

    def __post_init__(self):
        for value in (
            self.evidence_id,
            self.review_id,
            self.principal_id,
            self.client_id,
            self.response_id,
        ):
            text(value, "receipt card identity", 512)
        sha(self.display_sha256)
        sha(self.response_sha256)
        _integer(self.sequence, "receipt card journal sequence")
        _integer(self.message_id, "receipt card message id")
        _integer(self.timestamp, "receipt card delivery timestamp")


@dataclass(frozen=True)
class VerifiedReceiptConfirmActionV1:
    """Original authenticated confirmation to the exact delivered card."""

    evidence_id: str
    review_id: str
    review_sha256: str
    delivery_evidence_id: str
    principal_id: str
    client_id: str
    position: ReceiptJournalReplyRefV1
    quoted_response_id: str
    quoted_response_sha256: str
    quoted_message_id: int
    timestamp: int
    action: str = "confirm"

    def __post_init__(self):
        for value in (
            self.evidence_id,
            self.review_id,
            self.delivery_evidence_id,
            self.principal_id,
            self.client_id,
            self.quoted_response_id,
        ):
            text(value, "receipt confirmation identity", 512)
        sha(self.review_sha256)
        sha(self.quoted_response_sha256)
        if (
            type(self.position) is not ReceiptJournalReplyRefV1
            or type(self.action) is not str
            or self.action != "confirm"
        ):
            raise LifecycleError("Exact authenticated receipt confirmation required")
        _integer(self.quoted_message_id, "receipt quoted card message id")
        _integer(self.timestamp, "receipt confirmation timestamp")


@dataclass(frozen=True)
class _ReceiptExpectedMovementV1:
    binding: RuntimeBinding
    source_id: str
    record_id: str
    current_proposal_id: str | None
    expected_proposal_version: int
    expense: RetainedManualReceiptExpenseV1 | RetainedOcrReceiptExpenseV1
    accounts: list
    observation_format: str
    command_kind: ClassVar[str] = "movement_create"
    before_values: ClassVar[None] = None
    target_bundle_ref: ClassVar[None] = None
    expected_version: ClassVar[int] = 0

    @property
    def is_ocr(self) -> bool:
        return self.observation_format == OCR_OBSERVATION_FORMAT

    @property
    def review_format(self) -> str:
        return OCR_REVIEW_FORMAT if self.is_ocr else REVIEW_FORMAT


def _observation(expected):
    expense = expected.expense
    return {
        "binding": asdict(expected.binding),
        "ledger_cut": asdict(expense.ledger_cut),
        "fields_reply": asdict(expense.history.reply_positions[-1]),
        "history_sha256": digest(asdict(expense.history)),
        "preview_sha256": expense.preview.preview_sha256,
    }


def _observation_id(source_id, lookup, observation_format):
    prefix = "ocr-receipt-" if observation_format == OCR_OBSERVATION_FORMAT else "manual-receipt-"
    return prefix + digest([observation_format, source_id, asdict(lookup)])


def _query_previous(conn, source_id, version, *, _rows=None):
    return (
        _rows.one(conn, "proposal_version", source_id, version)
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_proposals WHERE source_id=? AND version=?", (source_id, version)
        ).fetchone()
    )


def _expected(
    service, conn, source_id, lookup, previous, observation_format, *, cut=None, _rows=None
):
    if (
        previous is not None
        and strict_json(previous["observation_json"]).get("format") != observation_format
    ):
        raise LifecycleError("Receipt proposal lacks exact typed predecessor")
    rebuild = (
        _reconstruct_ocr_expense
        if observation_format == OCR_OBSERVATION_FORMAT
        else _reconstruct_manual_expense
    )
    expense = rebuild(service, conn, source_id, lookup, ledger_cut=cut, _rows=_rows)
    selected = expense.preview.account.account_ref.account_id
    accounts = [
        value
        for value in account_inventory(conn, expense.ledger_cut.event_seq + 1, _rows=_rows)
        if value["fact_ref"]["record_id"] == selected
    ]
    return _ReceiptExpectedMovementV1(
        service.binding,
        source_id,
        movement_record_id_v1(source_id),
        previous["proposal_id"] if previous else None,
        previous["version"] if previous else 0,
        expense,
        accounts,
        observation_format,
    )


def _proposal_material(expected, observation, values):
    return [
        expected.source_id,
        expected.expected_proposal_version + 1,
        observation,
        values,
        expected.expected_version,
        expected.record_id,
    ]


def verify_proposal(service, conn, proposal, sealed, *, _rows=None):
    exact(sealed, {"format", "expected", "observation"}, "receipt observation envelope")
    observation_format = sealed["format"]
    if observation_format not in OBSERVATION_FORMATS:
        raise LifecycleError("Unsupported receipt observation")
    observation = exact(
        sealed["observation"],
        {"binding", "ledger_cut", "fields_reply", "history_sha256", "preview_sha256"},
        "receipt observation",
    )
    lookup = ReceiptJournalReplyRefV1(
        **exact(
            observation["fields_reply"],
            ReceiptJournalReplyRefV1.__dataclass_fields__,
            "receipt replies",
        )
    )
    events = (
        _rows.many(conn, "entity_events", "propose", proposal["proposal_id"])
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_events WHERE kind='propose' AND entity_id=?",
            (proposal["proposal_id"],),
        ).fetchall()
    )
    if len(events) != 1:
        raise LifecycleError("Receipt proposal lacks its exact original event")
    event = events[0]
    # The append-only proposal directly follows the historical account and ledger cut.
    cut = ReceiptLedgerCutV1(event["seq"] - 1, event["previous_sha256"])
    previous = _query_previous(conn, proposal["source_id"], proposal["version"] - 1, _rows=_rows)
    expected = _expected(
        service,
        conn,
        proposal["source_id"],
        lookup,
        previous,
        observation_format,
        cut=cut,
        _rows=_rows,
    )
    values_json = expected.expense.preview.candidate.values_json
    material = _proposal_material(expected, observation, strict_json(values_json))
    _require_prefix(conn, expected, proposal, "proposal", _rows=_rows)
    if (
        observation != _observation(expected)
        or sealed["expected"] != {"binding": observation["binding"]}
        or proposal["version"] != expected.expected_proposal_version + 1
        or proposal["expected_version"] != expected.expected_version
        or proposal["record_id"] != expected.record_id
        or proposal["observation_id"]
        != _observation_id(expected.source_id, lookup, observation_format)
        or proposal["values_json"] != values_json
        or proposal["proposal_sha256"] != digest(material)
        or event["payload_json"] != canonical(material)
    ):
        raise LifecycleError("Receipt proposal differs from independently reconstructed evidence")
    return expected


def _latest_reply(service, conn, expected, through=None):
    history = expected.expense.history
    wanted = history.reply_positions[-1]
    kind = "ocr" if expected.is_ocr else "manual"
    with _receipt_lookup(service.port, conn, service.binding) as read:
        position = _copy_position(through) if through is not None else None
        actual = read(f"receipt_{kind}_fields_reply_at_v1", history.source.evidence_id, position)
        if position != through or actual is None or _copy_position(actual) != wanted:
            raise LifecycleError(f"stale_receipt_{kind}_revision")


def require_current_receipt(service, conn, expected):
    if service.binding != expected.binding:
        raise LifecycleError("stale_receipt_binding")
    _latest_reply(service, conn, expected)
    rebuild = _reconstruct_ocr_expense if expected.is_ocr else _reconstruct_manual_expense
    current = rebuild(
        service, conn, expected.source_id, expected.expense.history.reply_positions[-1]
    )
    if current.history != expected.expense.history or current.preview != expected.expense.preview:
        kind = "ocr" if expected.is_ocr else "manual"
        raise LifecycleError(f"stale_receipt_{kind}_evidence")


def propose(
    service, conn, source_id, fields_reply, operation_id, *, observation_format=OBSERVATION_FORMAT
):
    text(source_id, "receipt source id")
    text(operation_id, "operation id")
    lookup = _copy_position(fields_reply)
    observation_id = _observation_id(source_id, lookup, observation_format)
    prior_operation = conn.execute(
        "SELECT kind,entity_id FROM l2_events WHERE operation_id=?", (operation_id,)
    ).fetchone()
    existing = conn.execute(
        "SELECT * FROM l2_proposals WHERE observation_id=?", (observation_id,)
    ).fetchone()
    if prior_operation is not None or existing is not None:
        if (
            existing is None
            or prior_operation is None
            or tuple(prior_operation) != ("propose", existing["proposal_id"])
        ):
            raise LifecycleError("Receipt proposal replay mismatch")
        verify_proposal(service, conn, existing, strict_json(existing["observation_json"]))
        return ProposalRef(
            existing["proposal_id"], source_id, existing["version"], existing["proposal_sha256"]
        )
    if conn.execute("SELECT count(*) FROM l2_proposals").fetchone()[0] >= MAX_PROPOSALS:
        raise LifecycleError("complete_read_limit_exceeded")
    previous = conn.execute(
        "SELECT * FROM l2_proposals WHERE source_id=? ORDER BY version DESC LIMIT 1", (source_id,)
    ).fetchone()
    expected = _expected(service, conn, source_id, lookup, previous, observation_format)
    _latest_reply(service, conn, expected)
    _require_current(conn, expected)
    candidate = expected.expense.preview.candidate
    observation = _observation(expected)
    sealed = {
        "format": observation_format,
        "expected": {"binding": observation["binding"]},
        "observation": observation,
    }
    material = _proposal_material(expected, observation, strict_json(candidate.values_json))
    proposal_id = _id("daily-receipt-proposal")
    proposal_sha256 = digest(material)
    version = expected.expected_proposal_version + 1
    conn.execute(
        "INSERT INTO l2_proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            proposal_id,
            source_id,
            version,
            observation_id,
            expected.expected_version,
            expected.record_id,
            candidate.values_json,
            canonical(sealed),
            proposal_sha256,
            service._now(),
        ),
    )
    _event(conn, operation_id, "propose", proposal_id, material)
    return ProposalRef(proposal_id, source_id, version, proposal_sha256)


def receipt_card_material(expected, created_at):
    if created_at < expected.expense.history.review_fields.reply.reply_timestamp:
        raise LifecycleError("Receipt card predates the completed receipt reply")
    candidate = expected.expense.preview.candidate
    return {
        "source_id": candidate.source_id,
        "source_sha256": candidate.source_sha256,
        "original_message_sha256": candidate.original_message_sha256,
        "original_media_sha256": candidate.original_media_sha256,
        "preview_sha256": expected.expense.preview.preview_sha256,
    }


def _read_by_id(service, conn, identity, name, dto):
    with _receipt_lookup(service.port, conn, service.binding) as read:
        actual = read(name, identity)
        if type(actual) is not dto or actual.evidence_id != identity:
            raise LifecycleError("Exact authenticated receipt card evidence required")
        if dto is VerifiedReceiptConfirmActionV1:
            return replace(actual, position=_copy_position(actual.position))
        return replace(actual)


def _earlier_transport(expected):
    history = expected.expense.history
    replies = (history.payment.selection, history.payment.debit, history.review_fields.reply)
    positions = (history.source_message, *history.reply_positions)
    responses = {p.response_id for p in positions} | {r.question_response_id for r in replies}
    messages = {p.message_id for p in positions} | {r.question_message_id for r in replies}
    if history.manual_replies:
        messages.add(history.manual_replies[0].question_message_id)
    if type(history) is VerifiedReceiptOcrHistoryV1:
        responses.update(
            (
                history.registration.prepared_evidence_id,
                history.registration.registration_evidence_id,
            )
        )
    return positions[-1], responses, messages


def read_delivery(service, conn, expected, review, identity):
    actual = _read_by_id(
        service, conn, identity, "receipt_card_delivery_v1", VerifiedReceiptCardDeliveryV1
    )
    fields, responses, messages = _earlier_transport(expected)
    material = strict_json(review["material_json"])
    if (
        actual.review_id != review["review_id"]
        or actual.display_sha256 != review["review_sha256"]
        or actual.principal_id != expected.binding.principal_id
        or actual.client_id != expected.binding.client_id
        or actual.sequence <= fields.sequence
        or actual.response_id in responses
        or actual.message_id in messages
        or not material["created_at"] <= actual.timestamp < material["expires_at"]
        or actual.timestamp < expected.expense.history.review_fields.reply.reply_timestamp
    ):
        raise LifecycleError("Receipt delivery differs from the exact complete card")
    return actual


def read_action(service, conn, expected, review, delivery, identity, accepted_at):
    actual = _read_by_id(
        service, conn, identity, "receipt_confirm_action_v1", VerifiedReceiptConfirmActionV1
    )
    fields, responses, messages = _earlier_transport(expected)
    published = strict_json(delivery["material_json"])
    if (
        actual.review_id != review["review_id"]
        or actual.review_sha256 != review["review_sha256"]
        or actual.delivery_evidence_id != delivery["evidence_id"]
        or actual.principal_id != expected.binding.principal_id
        or actual.client_id != expected.binding.client_id
        or (actual.quoted_response_id, actual.quoted_response_sha256, actual.quoted_message_id)
        != (published["response_id"], published["response_sha256"], published["message_id"])
        or actual.position.sequence <= published["sequence"]
        or actual.position.update_id <= fields.update_id
        or actual.position.message_id in (messages | {published["message_id"]})
        or actual.position.response_id in (responses | {published["response_id"]})
        or actual.evidence_id
        in (responses | {delivery["evidence_id"], expected.expense.history.source.evidence_id})
        or type(accepted_at) is not int
        or not published["timestamp"] <= actual.timestamp <= accepted_at
    ):
        raise LifecycleError("A separate original receipt confirmation is required")
    if conn.execute(
        "SELECT 1 FROM l2_decisions WHERE action_evidence_id<>? "
        "AND json_extract(material_json,'$.client_id')=? "
        "AND (json_extract(material_json,'$.position.update_id')=? "
        "OR json_extract(material_json,'$.position.message_id')=?) LIMIT 1",
        (
            actual.evidence_id,
            actual.client_id,
            actual.position.update_id,
            actual.position.message_id,
        ),
    ).fetchone():
        raise LifecycleError("Original receipt confirmation was already used")
    _latest_reply(service, conn, expected, through=actual.position)
    return actual
