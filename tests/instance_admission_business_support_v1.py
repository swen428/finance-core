"""Reusable business proof on fresh production profiles and real issued sessions.

All transport/approval artifacts are signed synthetic fixtures. Receipt custody
is prepared by the qualified staging fixture, then only its initial source rows
are inserted into the new production DB. No existing DB is attached or adopted.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

# Fixed trusted test composition, declared before an empty profile is prepared.
# No schema is learned from a subject database or an operational request.
APPLICATION_EVIDENCE_TABLES = (
    "synthetic_sources",
    "synthetic_displays",
    "synthetic_decisions",
    "synthetic_replies",
    "synthetic_display_closures",
    "synthetic_correction_displays",
    "synthetic_correction_replies",
    "synthetic_correction_decisions",
    "synthetic_correction_display_closures",
    "synthetic_amendment_displays_v1",
    "synthetic_amendment_replies_v1",
    "synthetic_amendment_evidence_v1",
    "synthetic_ai_receipts",
    "synthetic_interpretation_replies",
    "synthetic_interpretation_displays",
)
APPLICATION_EVIDENCE_DDL = tuple(
    f"CREATE TABLE {table} (id TEXT PRIMARY KEY, material TEXT NOT NULL, signature TEXT NOT NULL)"
    for table in APPLICATION_EVIDENCE_TABLES
)


def _copy_initial_receipt_source(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    """Copy synthetic initial custody only; forbid any financial result rows."""
    forbidden = (
        "transactions",
        "receipts",
        "parser_proposal_authorizations",
        "application_posting_attempts",
        "application_posting_reviews",
        "receipt_item_allocation_fact_sets",
        "authoritative_calculation_snapshots",
        "receipt_finalization_authorizations",
        "financial_audit_events",
    )
    for table in forbidden:
        assert source.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0, table
    target.execute("BEGIN IMMEDIATE")
    target.execute("PRAGMA defer_foreign_keys=ON")
    try:
        # Explicit synthetic custody allow-list. Authority and migration/control
        # records are never transferred. Production remains a fresh native DB.
        tables = (
            "participants",
            "raw_intake_records",
            "attachments",
            "local_attachment_source",
            "receipt_ocr_extractions",
            "receipt_ocr_blocks",
            "parser_outputs",
            "parser_proposal_field_evidence",
            "receipt_ocr_proposal_links",
            "synthetic_sources",
            "raw_intake_evidence",
        )
        for table in tables:
            assert (
                target.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                is not None
            ), table
            if table == "local_attachment_source":
                # Preserve the original ID allocation: raw-input evidence is
                # first, migration040's attachment-evidence trigger is second.
                for evidence in source.execute(
                    "SELECT * FROM raw_intake_evidence WHERE evidence_type='raw_input' ORDER BY id"
                ):
                    placeholders = ",".join("?" for _ in evidence)
                    target.execute(
                        f"INSERT INTO raw_intake_evidence VALUES ({placeholders})", evidence
                    )
            rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
            columns = source.execute(f'PRAGMA table_info("{table}")').fetchall()
            names = ",".join('"' + str(column[1]) + '"' for column in columns)
            placeholders = ",".join("?" for _ in columns)
            for row in rows:
                if table == "raw_intake_evidence":
                    existing = target.execute(
                        "SELECT * FROM raw_intake_evidence WHERE public_id=?", (row[1],)
                    ).fetchone()
                    if existing is not None:
                        assert tuple(existing) == tuple(row), "trigger evidence changed"
                        continue
                target.execute(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', row)
            assert [
                tuple(row) for row in target.execute(f'SELECT * FROM "{table}" ORDER BY id')
            ] == [tuple(row) for row in source.execute(f'SELECT * FROM "{table}" ORDER BY id')], (
                table
            )
        assert (
            target.execute(
                "SELECT 1 FROM sqlite_master WHERE name='_staging_authorization'"
            ).fetchone()
            is None
        )
        target.commit()
    except BaseException:
        target.rollback()
        raise


def _production_config(root: Path, *, instance_id="synthetic-production-business"):
    from finance_core.instance_admission import ApplicationSchemaContract, TrustedInstanceConfig

    layout = "linux" if sys.platform == "linux" else "mac"
    support = root / ("finance-codex" if layout == "linux" else "Application Support")
    support.mkdir(mode=0o700)
    product = support if layout == "linux" else support / "Finance-Codex"
    base = product / "profiles" / "business"
    for directory in (
        *(() if product == support else (product,)),
        base.parent,
        base,
        base / "runtime",
        base / "runtime/database",
        base / "workspace",
        base / "workspace/database",
        base / "backups",
        base / "work",
        base / "restore",
    ):
        directory.mkdir(mode=0o700)
    manifest = base / "profile.json"
    manifest.write_text(
        json.dumps(
            {
                "profile_id": "business",
                "runtime_root": str(base / "runtime"),
                "workspace_root": str(base / "workspace"),
            }
        )
    )
    manifest.chmod(0o600)
    control = root / "control"
    control.mkdir(mode=0o700)
    return TrustedInstanceConfig(
        control,
        support,
        "business",
        layout=layout,
        instance_id=instance_id,
        application_schema=ApplicationSchemaContract(
            "synthetic-business-evidence-v1", APPLICATION_EVIDENCE_DDL
        ),
        config_identity="synthetic-config-v1",
        core_identity="synthetic-core-v1",
        approval_verifier=lambda ref, action, binding: ref == "approved",
    ), base


def run_installed_business_probe(root: Path) -> dict[str, object]:
    """Run actual text/receipt posting, correction, amendment, AI, and ro reopen."""
    import application_amendment_support_v1 as amendment_support
    import test_application_amendment_v1 as amendment_fixtures
    import test_application_posting_recovery_v1 as posting_fixtures
    import test_independent_ai_invocation_v1 as ai_fixtures
    import test_independent_corrections_v1 as correction_fixtures

    from finance_core.application.amendment_contract import AmendmentBinding
    from finance_core.application.posting import PostingService
    from finance_core.instance_admission import (
        acquire_production_owner,
        admit_production_instance,
        fence_production_instance,
        prepare_production_profile,
        production_read_operation,
        production_write_operation,
    )
    from finance_core.staging_guard import StagingDatabaseError, require_staging_database

    root.mkdir(exist_ok=True)
    config, base = _production_config(root)
    binding = replace(posting_fixtures.BINDING, instance_id=config.instance_id)
    edit_binding = AmendmentBinding(
        binding, "synthetic-amendment-authority", "synthetic-amendment-key-v1"
    )
    with ExitStack() as stack:
        stack.enter_context(config)
        stack.enter_context(
            patch.dict("os.environ", {"FINANCE_RUNTIME_ROOT": str(base / "runtime")})
        )
        for module in {
            posting_fixtures,
            correction_fixtures.posting_fixtures,
            amendment_support,
            amendment_fixtures,
            ai_fixtures,
        }:
            stack.enter_context(patch.object(module, "BINDING", binding))
        stack.enter_context(patch.object(amendment_fixtures, "_EDIT_BINDING", edit_binding))
        stack.enter_context(patch.object(ai_fixtures, "_EDIT_BINDING", edit_binding))
        prepare_production_profile(
            config, operation_id="prepare-business", approval_ref="approved"
        ).close()
        admit_production_instance(config, operation_id="admit-business", approval_ref="approved")
        with acquire_production_owner(config) as owner:
            with production_write_operation(owner) as conn:
                for table in APPLICATION_EVIDENCE_TABLES:
                    assert (
                        conn.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ).fetchone()
                        is not None
                    ), table
                try:
                    require_staging_database(conn)
                except StagingDatabaseError:
                    pass
                else:
                    raise AssertionError("Production must never be registered as staging")
                source_root = root / "receipt-source"
                source_root.mkdir()
                source, _workspace, _manifest, _service, _review, proposal_id, *_ = (
                    posting_fixtures._prepare_receipt_subject(
                        source_root, "production-receipt", prepare_review=False
                    )
                )
                try:
                    _copy_initial_receipt_source(source, conn)
                finally:
                    source.close()
                service, review, proposal, intake, decision, _ = (
                    posting_fixtures._prepare_text_subject(conn, "production-text")
                )
                assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
                posted = service.submit_post(review.review_id, decision)
                assert (
                    service.submit_post(review.review_id, decision).attempt_id == posted.attempt_id
                )
                correction_service = correction_fixtures._correction_service(service)
                original = correction_service.read_effective(posted.transaction_public_id)
                original_row = tuple(
                    conn.execute(
                        "SELECT * FROM transactions WHERE public_id=?",
                        (posted.transaction_public_id,),
                    ).fetchone()
                )
                source_row = tuple(
                    conn.execute(
                        "SELECT * FROM synthetic_sources "
                        "WHERE id='synthetic-source-production-text'"
                    ).fetchone()
                )
                plan = correction_service.prepare_correction(
                    posted.transaction_public_id,
                    correction_fixtures._six_field_patch(),
                    "Synthetic correction",
                )
                correction_decision = correction_fixtures._persist_correction_approval(
                    conn, plan, decision_id="production-correction"
                )
                correction = correction_service.submit(plan.plan_id, correction_decision)
                assert correction.applied.version == correction.current.version == 1
                assert (
                    correction_service.submit(plan.plan_id, correction_decision).current
                    == correction.current
                )
                assert correction.current.original_source == original.original_source
                assert (
                    tuple(
                        conn.execute(
                            "SELECT * FROM transactions WHERE public_id=?",
                            (posted.transaction_public_id,),
                        ).fetchone()
                    )
                    == original_row
                )
                assert (
                    tuple(
                        conn.execute(
                            "SELECT * FROM synthetic_sources "
                            "WHERE id='synthetic-source-production-text'"
                        ).fetchone()
                    )
                    == source_row
                )
                receipt_service = PostingService(
                    connection=conn,
                    source_verifier=posting_fixtures._DurableSourceVerifier(),
                    human_decision_authority=posting_fixtures._DurablePostingDecisionAuthority(),
                    binding=binding,
                    clock=lambda: posting_fixtures.NOW,
                )
                receipt_review = receipt_service.prepare(proposal_id)
                amendment_fixtures._persist_fresh_posting_confirmation(
                    conn, receipt_review, proposal_id, "production-receipt-confirm"
                )
                assert conn.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
                receipt_posted = receipt_service.submit_post(
                    receipt_review.review_id, "production-receipt-confirm"
                )
                assert receipt_posted.state == "finalized"
                assert receipt_service.resume_post(receipt_posted.attempt_id) == receipt_posted
                receipt_corrector = correction_fixtures._correction_service(receipt_service)
                receipt_original = receipt_corrector.read_effective(
                    receipt_posted.transaction_public_id
                )
                receipt_original_row = tuple(
                    conn.execute(
                        "SELECT * FROM transactions WHERE public_id=?",
                        (receipt_posted.transaction_public_id,),
                    ).fetchone()
                )
                receipt_plan = receipt_corrector.prepare_correction(
                    receipt_posted.transaction_public_id,
                    correction_fixtures._six_field_patch(),
                    "Synthetic receipt correction",
                )
                receipt_decision = correction_fixtures._persist_correction_approval(
                    conn, receipt_plan, decision_id="production-receipt-correction"
                )
                receipt_correction = receipt_corrector.submit(
                    receipt_plan.plan_id, receipt_decision
                )
                assert receipt_correction.current.version == 1
                assert (
                    receipt_correction.current.original_source == receipt_original.original_source
                )
                assert (
                    tuple(
                        conn.execute(
                            "SELECT * FROM transactions WHERE public_id=?",
                            (receipt_posted.transaction_public_id,),
                        ).fetchone()
                    )
                    == receipt_original_row
                )
                # Amendment reissues a complete reviewed child without final facts.
                edit_service, _, edit_proposal, _, _, _ = posting_fixtures._prepare_text_subject(
                    conn, "production-amend"
                )
                amender = amendment_fixtures._amendment_service(conn)
                edit_review = amender.prepare(edit_proposal["public_id"])
                edited = amendment_fixtures._apply(
                    conn,
                    amender,
                    edit_review,
                    {"amount": "13.50"},
                    evidence_id="production-amend-evidence",
                    amendment_id="production-amend",
                )
                assert amender.get_status(edited.amendment_id) == edited
                before_ai = conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
                _oldposting, _oldreview, _parent, ai_intake, *_ = ai_fixtures.subject(
                    conn, "production-ai"
                )
                ai, provider = ai_fixtures.service(conn)
                ai_attempt = ai.prepare_initial(ai_intake["public_id"], "production-ai-initial")
                assert ai.invoke(ai_attempt.attempt_id).state == "VALID"
                published = ai.publish(ai_attempt.attempt_id)
                assert published.state == "PUBLISHED"
                assert provider.calls == 1 and ai.recover(published.attempt_id) == published
                # Durable clarification/edit publication still creates no final.
                ai_edit = ai_fixtures.amendment(conn, ai).prepare(published.proposal_public_id)
                reply_text = "Actually SGD 5.80 at Cafe Two; change and confirm"
                reply = ai_fixtures.persist_interpretation_reply(
                    conn, ai_edit, reply_text, identity="production-ai-reply"
                )
                provider.response = ai_fixtures.response(
                    "patch", {"amount": "5.80", "merchant": "Cafe Two"}, reply_text
                )
                edit_attempt = ai.prepare_edit(ai_edit.review_id, reply, "production-ai-edit")
                assert ai.invoke(edit_attempt.attempt_id).state == "VALID"
                ai_edited = ai.publish(edit_attempt.attempt_id)
                assert ai_edited.state == "PUBLISHED" and provider.calls == 2
                assert ai.recover(ai_edited.attempt_id) == ai_edited
                assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == before_ai
                snapshots = tuple(
                    tuple(row)
                    for row in conn.execute(
                        "SELECT * FROM authoritative_calculation_snapshots "
                        "ORDER BY snapshot_public_id"
                    )
                )
            fence_production_instance(
                config, operation_id="fence-business", approval_ref="approved"
            )
            with production_read_operation(owner) as conn:
                reader = PostingService(
                    connection=conn,
                    source_verifier=posting_fixtures._DurableSourceVerifier(),
                    human_decision_authority=posting_fixtures._DurablePostingDecisionAuthority(),
                    binding=binding,
                    clock=lambda: posting_fixtures.NOW + 5,
                    correction_decision_authority=correction_fixtures._SyntheticCorrectionDecisionAuthority(),
                )
                status = reader.get_status(posted.attempt_id)
                assert status.applied.version == 0 and status.current.version == 1
                assert (
                    correction_fixtures._correction_service(reader)
                    .read_effective(posted.transaction_public_id)
                    .version
                    == 1
                )
                receipt_status = reader.get_status(receipt_posted.attempt_id)
                assert receipt_status.applied.version == 0 and receipt_status.current.version == 1
                assert (
                    tuple(
                        tuple(row)
                        for row in conn.execute(
                            "SELECT * FROM authoritative_calculation_snapshots "
                            "ORDER BY snapshot_public_id"
                        )
                    )
                    == snapshots
                )
                economic_events = int(
                    conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
                )
                assert economic_events == 2
                readonly_verified = conn.total_changes == 0 and not conn.in_transaction
                assert readonly_verified
        return {
            "text_posted": posted.state == "finalized",
            "receipt_posted": receipt_posted.state == "finalized",
            "current_versions": [status.current.version, receipt_status.current.version],
            "economic_events": economic_events,
            "originals_unchanged": correction.current.original_source == original.original_source
            and receipt_correction.current.original_source == receipt_original.original_source,
            "readonly_verified": readonly_verified,
        }
