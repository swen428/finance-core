"""Read receipt draft fields from authenticated manual evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date

from .contracts import LifecycleError, VerifiedReceiptSourceV1, digest

_LABELS = {"商家": "merchant", "日期": "date", "总额": "amount", "币种": "currency"}
_LINE = re.compile(r"(商家|日期|总额|币种)\s*[:：]\s*(.+)")


@dataclass(frozen=True)
class VerifiedReceiptManualReplyV1:
    """Exact actor, original-source and reply custody from the installed evidence port."""

    source_evidence_id: str
    source_sha256: str
    principal_id: str
    client_id: str
    request_id: str
    question_message_id: int
    reply_message_id: int
    reply_update_id: int
    reply_timestamp: int
    response_id: str
    response_sha256: str
    raw_text: str


@dataclass(frozen=True)
class ReceiptManualFieldV1:
    name: str
    value: str
    raw_codepoint_span: tuple[int, int]


@dataclass(frozen=True)
class ReceiptManualDraftV1:
    draft_id: str
    source_id: str
    source_evidence_id: str
    source_sha256: str
    original_message_sha256: str
    original_image_sha256: str
    manual_reply: VerifiedReceiptManualReplyV1
    reply_text_sha256: str
    fields: tuple[ReceiptManualFieldV1, ...]
    missing_receipt_fields: tuple[str, ...]


def _manual_fields(raw):
    fields = {}
    offset = 0
    for line in raw.splitlines(keepends=True):
        pair = _LINE.fullmatch(line.strip())
        if pair is None:
            raise LifecycleError("Exact manual receipt field lines required")
        name, value = _LABELS[pair[1]], pair[2]
        if name in fields or not 1 <= len(value) <= 160 or not value.isprintable():
            raise LifecycleError("Malformed or repeated manual receipt field")
        if name == "date":
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError:
                raise LifecycleError("Invalid explicit manual receipt date") from None
        if name == "amount" and not re.fullmatch(r"(0|[1-9][0-9]{0,11})(\.[0-9]{1,4})?", value):
            raise LifecycleError("Invalid explicit manual receipt amount")
        if name == "currency" and not re.fullmatch(r"[A-Z]{3}", value):
            raise LifecycleError("Invalid explicit manual receipt currency")
        start = offset + len(line) - len(line.lstrip()) + pair.start(2)
        fields[name] = ReceiptManualFieldV1(name, value, (start, start + len(value)))
        offset += len(line)
    return fields


def _project_manual_draft(source_id, source, reply):
    if (
        type(source) is not VerifiedReceiptSourceV1
        or type(reply) is not VerifiedReceiptManualReplyV1
    ):
        raise LifecycleError("Exact authenticated receipt source and manual reply required")
    source_sha256 = digest(asdict(source))
    if (
        reply.source_evidence_id != source.evidence_id
        or reply.source_sha256 != source_sha256
        or reply.principal_id != source.principal_id
        or reply.client_id != source.client_id
        or any(
            type(value) is not str or not 1 <= len(value) <= 512
            for value in (source_id, reply.request_id, reply.response_id)
        )
        or any(
            type(value) is not int or not 0 < value < 2**63
            for value in (reply.question_message_id, reply.reply_message_id, reply.reply_timestamp)
        )
        or reply.question_message_id == reply.reply_message_id
        or type(reply.reply_update_id) is not int
        or not 0 <= reply.reply_update_id < 2**63
        or reply.reply_timestamp < source.source_timestamp
        or type(reply.response_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", reply.response_sha256) is None
        or type(reply.raw_text) is not str
        or not 1 <= len(reply.raw_text) <= 4096
    ):
        raise LifecycleError("Manual receipt reply differs from original source custody")
    fields = _manual_fields(reply.raw_text)
    ordered = tuple(_LABELS.values())
    return ReceiptManualDraftV1(
        draft_id="receipt_draft_" + digest(["receipt-manual-draft-v1", source_id, asdict(reply)]),
        source_id=source_id,
        source_evidence_id=source.evidence_id,
        source_sha256=source_sha256,
        original_message_sha256=source.original_message_sha256,
        original_image_sha256=source.original_sha256,
        manual_reply=reply,
        reply_text_sha256=hashlib.sha256(reply.raw_text.encode()).hexdigest(),
        fields=tuple(fields[name] for name in ordered if name in fields),
        missing_receipt_fields=tuple(name for name in ordered if name not in fields),
    )
