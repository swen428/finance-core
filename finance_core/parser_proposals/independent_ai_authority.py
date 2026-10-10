"Shared fail-closed authenticated gate for actual new-AI owning operations."

from __future__ import annotations

import sqlite3
from typing import Any

from finance_core.application.admission import TrustedBinding, VerifiedSource
from finance_core.application.ai_invocation_contract import AiInvocationError, ExpectedAiLineage
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.independent_ai_lineage import (
    has_independent_ai_ancestry,
    verify_independent_ai_lineage,
)
from finance_core.parser_proposals.repository import ParserProposalRepository


def require_ai_authority(conn: sqlite3.Connection, proposal: Any, authority: Any) -> None:
    """Preserve the invocation gate's typed error while retaining native cause."""
    from finance_core.financial_audit import AuditVerificationError
    from finance_core.parser_proposals.ai_fallback import AiFallbackServiceError
    from finance_core.parser_proposals.amendment_lineage import AmendmentLineageError
    from finance_core.parser_proposals.human_revision import HumanRevisionLineageError

    try:
        _require_ai_authority(conn, proposal, authority)
    except (
        AiFallbackServiceError,
        AmendmentLineageError,
        HumanRevisionLineageError,
        AuditVerificationError,
    ) as exc:
        raise AiInvocationError(str(exc)) from exc


def _require_ai_authority(conn: sqlite3.Connection, proposal: Any, authority: Any) -> None:
    full = ParserProposalRepository(conn).get_lineage_row_by_id(proposal["id"])
    if full is None:
        raise AiInvocationError("AI authority proposal absent")
    if not has_independent_ai_ancestry(conn, full):
        return
    content_hash = compute_effective_proposal_content_hash(conn, full)
    version = resolve_effective_payload(conn, full)[2]
    lineage = verify_independent_ai_lineage(
        conn, full, content_hash=content_hash, proposal_version=version, require_resolved=False
    )
    if authority is None or lineage is None:
        raise AiInvocationError("New AI origin requires fixed authenticated lineage authority")
    import json

    row = conn.execute(
        "SELECT material_json FROM independent_ai_attempts WHERE attempt_id=?",
        (lineage["attempt_ids"][-1],),
    ).fetchone()
    if row is None:
        raise AiInvocationError("AI authority source binding absent")
    material = json.loads(row[0])
    expected = ExpectedAiLineage(
        TrustedBinding(**material["binding"]),
        VerifiedSource(**material["source"]),
        full["public_id"],
        version,
        content_hash,
    )
    from collections.abc import Mapping

    authenticated = authority.verify_in_snapshot(conn, expected)
    from finance_core.parser_proposals.independent_ai_validation import canonical_text

    if not isinstance(authenticated, Mapping) or canonical_text(
        dict(authenticated)
    ) != canonical_text(lineage):
        raise AiInvocationError("New AI origin authentication unavailable")


def require_receipt_ai_authority(
    conn: sqlite3.Connection, receipt_id: str, authority: Any, receipt_posting_authorization=None
) -> None:
    cursor = conn.execute(
        (
            "SELECT p.* FROM receipt_proposal_conversions c JOIN receipts r ON"
            " r.id=c.receipt_id JOIN parser_outputs p ON p.id=c.parser_output_"
            "id WHERE r.public_id=?"
        ),
        (receipt_id,),
    )
    from finance_core.parser_proposals.repository import row_to_dict

    for row in cursor.fetchall():
        require_ai_authority(conn, row_to_dict(row, cursor.description), authority)

    from finance_core.application.receipt_posting_authorization import (
        require_receipt_posting_authority,
    )

    require_receipt_posting_authority(conn, receipt_id, receipt_posting_authorization)
