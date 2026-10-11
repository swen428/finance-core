"""A receipt's authenticated original is distinct from its local OCR attachment."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from test_application_posting_recovery_v1 import (
    BINDING,
    NOW,
    SOURCE_KEY,
    _digest,
    _DurablePostingDecisionAuthority,
    _load,
    _persist,
    _prepare_receipt_subject,
)
from test_independent_ai_invocation_v1 import ORIGINAL, DurableProvider
from test_independent_ai_v2 import (
    InputV2,
    cite,
    persist_reply,
    wire,
)

from finance_core.application.admission import SOURCE_SCHEMA, VerifiedSource
from finance_core.application.ai_invocation import AiInvocationService
from finance_core.application.ai_invocation_contract import AiInvocationError, InterpretationPolicy
from finance_core.application.posting import PostingService
from finance_core.parser_proposals.independent_ai_source_money import _v2_intent_source_text
from finance_core.receipt_staging_runner.local_intake import require_local_runner_receipt_proposal


class _OriginalReceiptSourceVerifier:
    """Signed generic source proof keeps both raw marker and original-image custody."""

    def __init__(self, original: Path, workspace: object, manifest: object) -> None:
        self.original, self.workspace, self.manifest = original, workspace, manifest

    def verify_persisted(self, connection, intake_public_id):
        source = _load(connection, "synthetic_sources", "original-ocr-source", SOURCE_KEY)
        if source["intake_public_id"] != intake_public_id:
            raise ValueError("Source intake differs")
        row = connection.execute(
            "SELECT raw_input,source_content_hash,attachment_hash,parser_output_id "
            "FROM raw_intake_records WHERE public_id=?",
            (intake_public_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Raw intake absent")
        raw_hash = hashlib.sha256(row["raw_input"].encode()).hexdigest()
        if row["source_content_hash"] != "sha256:" + raw_hash:
            raise ValueError("Raw marker integrity changed")
        original_hash = hashlib.sha256(self.original.read_bytes()).hexdigest()
        if (
            source["source_content_hash"] != original_hash
            or original_hash == row["attachment_hash"]
        ):
            raise ValueError("Original/normalized attachment identity changed")
        proposal = connection.execute(
            "SELECT public_id FROM parser_outputs WHERE id=?", (row["parser_output_id"],)
        ).fetchone()
        if proposal is None:
            raise ValueError("Receipt proposal absent")
        require_local_runner_receipt_proposal(
            connection, proposal["public_id"], workspace=self.workspace, manifest=self.manifest
        )
        unsigned = {key: value for key, value in source.items() if key != "evidence_digest"}
        if _digest(unsigned) != source["evidence_digest"]:
            raise ValueError("Signed original evidence changed")
        return VerifiedSource(**source)


@pytest.mark.parametrize("clarify_first", [False, True])
def test_v2_original_image_hash_distinct_from_ocr_attachment_and_raw_marker(
    tmp_path, clarify_first
):
    c, workspace, manifest, _, _, _proposal, intake, *_ = _prepare_receipt_subject(
        tmp_path,
        suffix="v2-distinct-original",
        prepare_review=False,
        retain_synthetic_authority=False,
        **(
            {"total_currency": None, "date_text": None, "ocr_confidence_scaled": 3000}
            if clarify_first
            else {}
        ),
    )
    try:
        original = tmp_path / "received-original.jpg"
        original.write_bytes(b"\xff\xd8\xff\xe0original-camera-image-v2")
        original.chmod(0o400)
        source = {
            "schema": SOURCE_SCHEMA,
            "namespace": BINDING.source_namespace,
            "key_id": BINDING.source_key_id,
            "instance_id": BINDING.instance_id,
            "submission_client_id": BINDING.submission_client_id,
            "evidence_id": "original-ocr-source",
            "intake_public_id": intake,
            "source_event_id": "original-ocr-event",
            "source_content_hash": hashlib.sha256(original.read_bytes()).hexdigest(),
            "source_occurred_at": NOW - 2,
            "received_at": NOW - 1,
        }
        source["evidence_digest"] = _digest(source)
        c.execute(
            "CREATE TABLE synthetic_sources "
            "(id TEXT PRIMARY KEY,material TEXT NOT NULL,signature TEXT NOT NULL)"
        )
        _persist(c, "synthetic_sources", source["evidence_id"], source, SOURCE_KEY)
        c.commit()
        raw = c.execute(
            "SELECT raw_input,attachment_hash FROM raw_intake_records WHERE public_id=?",
            (intake,),
        ).fetchone()
        assert raw is not None
        assert (
            len(
                {
                    source["source_content_hash"],
                    raw["attachment_hash"],
                    hashlib.sha256(raw["raw_input"].encode()).hexdigest(),
                }
            )
            == 3
        )
        text = (
            "EXAMPLE CAFE\nTOTAL\n12.50"
            if clarify_first
            else "EXAMPLE CAFE\n2026-10-08\nTOTAL\nSGD\n12.50"
        )
        fields = (
            {
                "amount": "12.50",
                "merchant": "EXAMPLE CAFE",
                "description": "Receipt total",
                "category": "food",
            }
            if clarify_first
            else {**ORIGINAL, "merchant": "EXAMPLE CAFE", "description": "Receipt total"}
        )
        evidence = {
            "amount": [cite(text, "12.50")],
        }
        if not clarify_first:
            evidence.update(
                {
                    "currency": [cite(text, "SGD")],
                    "transaction_date": [cite(text, "2026-10-08")],
                }
            )
        provider = DurableProvider(c, response=wire(text, fields, evidence=evidence))
        source_port = _OriginalReceiptSourceVerifier(original, workspace, manifest)
        ai = AiInvocationService(
            connection=c,
            binding=BINDING,
            source_verifier=source_port,
            interpretation_input_verifier=InputV2(),
            provider=provider,
            clock=lambda: NOW,
            interpretation_policy=InterpretationPolicy("UTC", "SGD"),
        )
        initial = ai.prepare_initial(intake, "original-ocr-initial")
        observed = ai.invoke(initial.attempt_id)
        if clarify_first:
            assert observed.state == "NEEDS_CLARIFICATION"
            question = ai.prepare_clarification(initial.attempt_id)
            assert question.projection["confirm_available"] is False
            assert {"currency", "transaction_date"} <= {
                field for item in question.projection["unresolved"] for field in item["fields"]
            }
            reply = "Use SGD and date 2026-10-09"
            proof = persist_reply(c, question, reply, identity="original-ocr-clarify-reply")
            provider.response = wire(
                reply, {"currency": "SGD", "transaction_date": "2026-10-09"}, ref="reply"
            )
            followup = ai.prepare_followup(
                question.request_id, proof, "original-ocr-clarify-followup"
            )
            assert ai.get_clarification(question.request_id).state == "CONSUMED"
            assert ai.invoke(followup.attempt_id).state == "VALID"
            published = ai.publish(followup.attempt_id)
            assert (
                ai.prepare_followup(question.request_id, proof, "original-ocr-clarify-followup")
                == published
            )
            with pytest.raises(AiInvocationError):
                ai.prepare_followup(question.request_id, proof, "duplicate-followup")
        else:
            assert observed.state == "VALID"
            published = ai.publish(initial.attempt_id)
        assert published.state == "PUBLISHED"
        assert ai.get_status(published.attempt_id) == published
        assert ai.recover(published.attempt_id) == published
        assert ai.publish(published.attempt_id) == published

        material = json.loads(
            c.execute(
                "SELECT material_json FROM independent_ai_attempts WHERE attempt_id=?",
                (initial.attempt_id,),
            ).fetchone()[0]
        )
        assert _v2_intent_source_text(c, material) == text
        for changed in (
            {**material, "intake_public_id": "foreign-intake"},
            {**material, "parent_id": -1},
            {**material, "source": {**material["source"], "intake_public_id": "foreign-intake"}},
            {**material, "projection": {**material["projection"], "source_text": "false OCR"}},
            {
                **material,
                "projection": {
                    **material["projection"],
                    "selection": {
                        "kind": "text",
                        "source_public_id": intake,
                    },
                },
            },
        ):
            with pytest.raises(AiInvocationError, match="Original V2 intent source integrity"):
                _v2_intent_source_text(c, changed)

        owner = PostingService(
            connection=c,
            binding=BINDING,
            source_verifier=source_port,
            human_decision_authority=_DurablePostingDecisionAuthority(),
            clock=lambda: NOW,
            ai_lineage_authority=ai,
        )
        review = owner.prepare(published.proposal_public_id)
        reply = "Change category to travel"
        proof = persist_reply(c, review, reply, identity="original-ocr-edit-reply")
        provider.response = wire(reply, {"category": "travel"}, ref="reply")
        edited = ai.prepare_edit(review.review_id, proof, "original-ocr-edit")
        assert ai.invoke(edited.attempt_id).state == "VALID"
        edited_published = ai.publish(edited.attempt_id)
        assert edited_published.state == "PUBLISHED"
        assert ai.get_status(edited.attempt_id) == edited_published
        assert ai.recover(edited.attempt_id) == edited_published
        edited_card = owner.prepare(edited_published.proposal_public_id)
        assert (
            edited_card.projection["expense_assessment"]["canonical_fields"]["category"] == "travel"
        )
        assert (
            edited_card.projection["source"]["source_content_hash"] == source["source_content_hash"]
        )
        assert provider.calls == (3 if clarify_first else 2)

        with pytest.raises(sqlite3.IntegrityError, match="source identity is frozen"):
            c.execute(
                "UPDATE raw_intake_records SET source_content_hash=? WHERE public_id=?",
                ("sha256:" + "0" * 64, intake),
            )
        c.rollback()
        assert _v2_intent_source_text(c, material) == text
        original.chmod(0o600)
        original.write_bytes(b"changed-original-image")
        with pytest.raises(ValueError, match="Original/normalized attachment identity"):
            source_port.verify_persisted(c, intake)
        with pytest.raises(ValueError, match="Original/normalized attachment identity"):
            ai.get_status(edited.attempt_id)
    finally:
        c.close()
