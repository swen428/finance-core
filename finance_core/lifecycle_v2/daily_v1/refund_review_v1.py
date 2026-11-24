"""Build one authenticated refund review with exact account and original-currency effects."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..contracts import LifecycleError, RuntimeBinding, VerifiedSource, canonical, digest
from ._impacts_v1 import resolve_transfer_effect_v1
from .accounts import DailyAccountServiceV1, _audit, _fingerprint, _source_instruction
from .contracts import BUNDLE_FORMATS, BundleFactRefV1, strict_json, text
from .refund_allowance_v1 import _fact_at
from .refund_dependencies_v1 import refund_posting_state_v1
from .refund_evidence_v1 import verify_refund_evidence_v1
from .refund_facts_v1 import project_refund_fact_v1
from .transfer_review_v1 import _change_totals

REVIEW_MATERIAL_FORMAT = "finance-daily-refund-review-material-v1"


@dataclass(frozen=True)
class RefundReviewPreviewV1:
    material_json: str
    material_sha256: str


def _resolved_credit(conn, values, fact_ref, before_seq):
    effect = project_refund_fact_v1(values).credit_effect
    if effect is None:
        return []
    row = _fact_at(conn, effect["account_id"], before_seq)
    account = strict_json(row["values_json"]) if row is not None else None
    if account is None or account.get("format") not in BUNDLE_FORMATS:
        raise LifecycleError("Refund review requires a current credit account")
    return [resolve_transfer_effect_v1(conn, effect, fact_ref, account, before_seq)]


def _refund_review_material(
    service,
    conn,
    binding: RuntimeBinding,
    source_id: str,
    values: dict[str, Any],
    replaced: BundleFactRefV1 | None,
    *,
    before_seq: int | None = None,
) -> dict[str, Any]:
    fingerprint = _fingerprint(conn)
    dependencies = refund_posting_state_v1(conn, values, replaced, before_seq=before_seq)
    source = service._source(
        conn, source_id, allow_archived=before_seq is not None or replaced is not None
    )
    if replaced is None:
        if source["generation"] != binding.generation:
            raise LifecycleError("Refund source belongs to another capture generation")
        instruction = VerifiedSource(**_source_instruction(source))
        before_values = None
        before_effect = []
    else:
        previous = _fact_at(conn, replaced.record_id, before_seq)
        if previous is None or previous["source_id"] != source_id:
            raise LifecycleError("Refund predecessor belongs to another capture")
        instruction = service.port.source(conn, values["instruction_ref"]["evidence_id"])
        before_values = strict_json(previous["values_json"])
        before_effect = _resolved_credit(conn, before_values, asdict(replaced), before_seq)

    evidence = verify_refund_evidence_v1(service, conn, binding, instruction, values, replaced)
    after_effect = _resolved_credit(conn, values, None, before_seq)
    amount_changes = _change_totals(before_effect, after_effect)
    # Each account reference is exact even when a reversal credits no account.
    # Null opening balance remains unknown while the recorded change is known.
    associations = [
        {key: value for key, value in effect.items() if key not in {"format", "transfer_fact_ref"}}
        for effect in before_effect + after_effect
    ]
    if _fingerprint(conn) != fingerprint:
        raise LifecycleError("Refund evidence callback changed protected state")
    return {
        "format": REVIEW_MATERIAL_FORMAT,
        "binding": asdict(binding),
        "source_id": source_id,
        "source_sha256": source["source_sha256"],
        "before_values": before_values,
        "after_values": values,
        "dependencies": asdict(dependencies),
        "account_effects_before": associations[: len(before_effect)],
        "account_effects_after": associations[len(before_effect) :],
        "account_impact_changes": [asdict(change) for change in amount_changes],
        "evidence": evidence,
    }


def read_refund_review_v1(
    service,
    source_id: str,
    values: dict[str, Any],
    *,
    replaced: BundleFactRefV1 | None = None,
) -> RefundReviewPreviewV1:
    """Read exact review material without creating a proposal, decision or fact."""
    text(source_id, "captured refund source id")
    original = strict_json(canonical(values))
    with DailyAccountServiceV1(service)._read() as (bound, conn):
        _audit(bound, conn)
        material = _refund_review_material(
            bound, conn, bound.binding, source_id, original, replaced
        )
        return RefundReviewPreviewV1(canonical(material), digest(material))
