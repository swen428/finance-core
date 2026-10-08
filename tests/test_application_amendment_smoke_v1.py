"""Real staging flow for independent text edits, with durable signed evidence."""

from __future__ import annotations

import dataclasses

import pytest
from test_application_posting_recovery_v1 import (
    BINDING,
    DECISION_KEY,
    NOW,
    _decision_material,
    _digest,
    _DurableSourceVerifier,
    _load,
    _persist,
    _prepare_text_subject,
    _reply_material,
)

from finance_core.application.amendment import AmendmentService
from finance_core.application.amendment_contract import (
    AMENDMENT_SCHEMA,
    AmendmentBinding,
    VerifiedHumanAmendment,
    amendment_patch_sha256,
)


class SignedAmendmentPort:
    def verify_persisted(self, connection, amendment_evidence_id, expected):
        material = _load(connection, "synthetic_amendments", amendment_evidence_id, DECISION_KEY)
        display = _load(
            connection, "synthetic_amendment_displays", material["display_id"], DECISION_KEY
        )
        reply = _load(
            connection, "synthetic_amendment_replies", amendment_evidence_id, DECISION_KEY
        )
        assert display["projection"] == expected.review_projection
        assert display["state"] == "delivered" and display["private"] is True
        assert display["human"] == BINDING.human_principal_id
        assert reply["display_id"] == material["display_id"] and reply["direct"] is True
        assert _digest(display) == material["display_evidence_digest"]
        assert _digest(reply) == material["reply_evidence_digest"]
        return VerifiedHumanAmendment(**material)


def persist_edit(conn, review, patch, evidence_id="edit-one"):
    for table in (
        "synthetic_amendments",
        "synthetic_amendment_displays",
        "synthetic_amendment_replies",
    ):
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY,material TEXT NOT "
            f"NULL,signature TEXT NOT NULL)"
        )
    source = review.projection["source"]
    base = review.projection["proposal_review"]
    display = {
        "projection": review.projection,
        "state": "delivered",
        "private": True,
        "human": BINDING.human_principal_id,
    }
    reply = {"display_id": evidence_id + "-display", "direct": True, "patch": patch}
    proof = VerifiedHumanAmendment(
        AMENDMENT_SCHEMA,
        "synthetic-amendment",
        "synthetic-amendment-key",
        BINDING.instance_id,
        BINDING.human_principal_id,
        evidence_id,
        evidence_id,
        source["evidence_id"],
        source["evidence_digest"],
        source["intake_public_id"],
        source["source_event_id"],
        base["proposal_public_id"],
        base["proposal_version"],
        base["effective_content_hash"],
        review.review_id,
        review.review_hash,
        evidence_id + "-display",
        _digest(display),
        _digest(reply),
        evidence_id + "-display",
        "amend",
        patch,
        amendment_patch_sha256(patch),
        NOW,
        NOW,
        NOW + 500,
        False,
        False,
        "a" * 64,
    )
    _persist(conn, "synthetic_amendment_displays", proof.display_id, display, DECISION_KEY)
    _persist(conn, "synthetic_amendment_replies", evidence_id, reply, DECISION_KEY)
    _persist(conn, "synthetic_amendments", evidence_id, dataclasses.asdict(proof), DECISION_KEY)
    conn.commit()


def test_real_text_amount_edit_invalidates_old_review_and_needs_fresh_confirmation(
    migrated_temp_db_connection,
):
    from finance_core.application.posting import PostingError

    conn = migrated_temp_db_connection
    posting, old_review, proposal, _intake, old_decision, _display = _prepare_text_subject(conn)
    edits = AmendmentService(
        connection=conn,
        source_verifier=_DurableSourceVerifier(),
        human_amendment_authority=SignedAmendmentPort(),
        binding=AmendmentBinding(BINDING, "synthetic-amendment", "synthetic-amendment-key"),
        clock=lambda: NOW,
    )
    review = edits.prepare(proposal["public_id"])
    persist_edit(conn, review, {"amount": "14.00"})
    result = edits.amend(review.review_id, "edit-one", "amend-one")
    assert result.proposal_public_id != proposal["public_id"]
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    with pytest.raises(PostingError):
        posting.submit_post(old_review.review_id, old_decision)
    fresh = posting.prepare(result.proposal_public_id)
    assert fresh.projection["financial_projection"]["amount"] == "14.00"
    display, decision = _decision_material(fresh, result.proposal_public_id, "fresh-confirmation")
    _persist(conn, "synthetic_displays", decision["display_id"], display, DECISION_KEY)
    _persist(
        conn, "synthetic_replies", decision["reply_id"], _reply_material(decision), DECISION_KEY
    )
    _persist(conn, "synthetic_decisions", "fresh-confirmation", decision, DECISION_KEY)
    conn.commit()
    posted = posting.submit_post(fresh.review_id, "fresh-confirmation")
    assert posted.transaction_public_id is not None
    assert conn.execute("SELECT amount FROM transactions").fetchone()[0] == 14
    assert edits.amend(review.review_id, "edit-one", "amend-one") == dataclasses.replace(
        result, is_current=True
    )
