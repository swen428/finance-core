"""Authenticated old owners, whole archive, and explicit historical negatives."""

from __future__ import annotations

import dataclasses
import json

import pytest
from application_amendment_support_v1 import DurableSyntheticAmendmentAuthority
from test_application_amendment_v1 import (
    _EDIT_BINDING,
    _apply,
    _persist_fresh_posting_confirmation,
)
from test_application_posting_recovery_v1 import (
    BINDING,
    NOW,
    _prepare_receipt_subject,
    _prepare_text_subject,
    _temporarily_drop_triggers,
)
from test_independent_ai_invocation_v1 import publish_initial
from test_independent_corrections_v1 import (
    _persist_correction_approval,
    _SyntheticCorrectionDecisionAuthority,
)

from finance_core.application.amendment import AmendmentService
from finance_core.application.amendment_contract import amendment_review_sha256
from finance_core.application.independent_correction_contract import correction_hash
from finance_core.application.independent_corrections import IndependentCorrectionService
from finance_core.application.posting import PostingService
from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text,
    canonical_json_value,
)
from finance_core.lifecycle_v2.legacy import (
    LEGACY_MIGRATION_PATHS,
    LegacyExportError,
    LegacyExportVerifier,
    LegacyReadAdapter,
    VerifiedLegacyExport,
    _archive,
    require_verified_legacy_export,
)
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS as NEW_MIGRATION_PATHS


@pytest.fixture(autouse=True)
def _old_receipt_migration_prefix(monkeypatch):
    from finance_core.reconciliation import migrations

    monkeypatch.setattr(migrations, "TEMP_DB_MIGRATION_PATHS", LEGACY_MIGRATION_PATHS)


@pytest.fixture()
def legacy_connection(temp_db_connection):
    from finance_core.reconciliation.migrations import apply_migration_paths

    apply_migration_paths(temp_db_connection, LEGACY_MIGRATION_PATHS)
    yield temp_db_connection


def _export_verifier(key=b"synthetic offline export root retained outside legacy database"):
    return LegacyExportVerifier(
        namespace=BINDING.source_namespace, instance_id=BINDING.instance_id, key=key
    )


def _authority_tables(c):
    return frozenset(
        row[0]
        for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'synthetic_%'"
        )
    )


def _compose(connection, original, *, ai=None, clock=NOW):
    authority = _SyntheticCorrectionDecisionAuthority()
    posting = PostingService(
        connection=connection,
        source_verifier=original._source_port,
        human_decision_authority=original._decision_port,
        binding=BINDING,
        clock=lambda: clock,
        ai_lineage_authority=ai,
        correction_decision_authority=authority,
    )
    amendment = AmendmentService(
        connection=connection,
        source_verifier=posting._source_port,
        human_amendment_authority=DurableSyntheticAmendmentAuthority(),
        binding=_EDIT_BINDING,
        clock=lambda: clock,
        ai_lineage_authority=ai,
    )
    corrections = IndependentCorrectionService(
        posting, human_decision_authority=authority, clock=lambda: clock + 5
    )
    return posting, amendment, corrections


def _subject(connection, *, edited=True, corrected=True, ai_origin=False):
    if ai_origin:
        ai, provider, publication, original, _, _, _ = publish_initial(connection, "legacy-ai")
        proposal_id = publication.proposal_public_id
    else:
        original, _, proposal, _, _, _ = _prepare_text_subject(connection, "legacy")
        ai = provider = None
        proposal_id = proposal["public_id"]
    posting, amendment, corrections = _compose(connection, original, ai=ai)
    unused = []
    if edited:
        # Two unused reviews of the real v0 survive two independent human edits.
        first = amendment.prepare(proposal_id)
        amendment._clock = lambda: NOW + 1
        unused.append(amendment.prepare(proposal_id).review_id)
        amendment._clock = lambda: NOW
        changed = _apply(
            connection,
            amendment,
            first,
            {"description": "Human first"},
            evidence_id="legacy-edit-one",
            amendment_id="legacy-edit-one",
        )
        second = amendment.prepare(changed.proposal_public_id)
        changed = _apply(
            connection,
            amendment,
            second,
            {"description": "Human second"},
            evidence_id="legacy-edit-two",
            amendment_id="legacy-edit-two",
        )
        proposal_id = changed.proposal_public_id
        unused.append(amendment.prepare(proposal_id).review_id)
    review = posting.prepare(proposal_id)
    _persist_fresh_posting_confirmation(connection, review, proposal_id, "legacy-final-confirm")
    posted = posting.submit_post(review.review_id, "legacy-final-confirm")
    if corrected:
        plan = corrections.prepare_correction(
            posted.transaction_public_id, {"amount": "15.75"}, "Correct signed amount"
        )
        decision = _persist_correction_approval(connection, plan, decision_id="legacy-correction")
        corrections.submit(plan.plan_id, decision)
    pending = corrections.prepare_correction(
        posted.transaction_public_id,
        {"description": "Pending never applied"},
        "Keep pending plan archived",
    )
    return (
        LegacyReadAdapter(
            posting=posting,
            amendment=amendment,
            corrections=corrections,
            authority_tables=_authority_tables(connection),
            export_verifier=_export_verifier(),
        ),
        posted,
        unused,
        pending,
        provider,
    )


@pytest.mark.parametrize("ai_origin", [False, True], ids=["deterministic", "actual-provider"])
@pytest.mark.parametrize("corrected", [False, True], ids=["original", "corrected"])
def test_old_authenticated_fact_and_complete_non_executable_archive(
    legacy_connection, corrected, ai_origin
):
    c = legacy_connection
    adapter, posted, unused, pending, provider = _subject(
        c, corrected=corrected, ai_origin=ai_origin
    )
    before = _archive(c)
    calls = None if provider is None else provider.calls
    exported = adapter.verify_export()
    assert require_verified_legacy_export(exported) is exported
    assert _archive(c) == before and not c.in_transaction
    assert provider is None or provider.calls == calls == 1
    manifest = json.loads(exported.manifest_json)
    assert len(manifest["records"]) == 1
    record = manifest["records"][0]
    assert record["legacy_record_id"] == posted.transaction_public_id
    assert record["original"] == {
        "amount": "12.50",
        "currency": "SGD",
        "transaction_date": "2026-10-08",
        "merchant": "Example Cafe",
        "description": "Human second",
        "category": "food",
    }
    assert record["current"]["amount"] == ("15.75" if corrected else "12.50")
    assert record["current"]["description"] == "Human second"
    assert record["version"] == (1 if corrected else 0)
    inventory = manifest["archive_inventory"]
    assert all(row["state"] == "archived_non_executable" for row in inventory)
    assert set(unused) <= {row["id"] for row in inventory}
    assert pending.plan_id in {row["id"] for row in inventory}
    assert json.loads(exported.archive_json) == before
    assert "synthetic_sources" in before["tables"]
    assert "financial_audit_events" in before["tables"]
    assert manifest["archive_rights"] == "read_only_non_executable"
    with pytest.raises(dataclasses.FrozenInstanceError):
        exported.instance_id = "another-instance"
    with pytest.raises(LegacyExportError):
        VerifiedLegacyExport(**dataclasses.asdict(exported))
    with pytest.raises(LegacyExportError):
        require_verified_legacy_export({"verified": True, **dataclasses.asdict(exported)})


def _mutate(c, table, operation):
    triggers = tuple(
        row[0]
        for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
        )
    )
    _temporarily_drop_triggers(c, triggers, operation)


@pytest.mark.parametrize("damage", ["editable", "proposal_review", "payload"])
def test_rehashed_unused_historical_card_is_not_authentic(legacy_connection, damage):
    c = legacy_connection
    adapter, _, unused, _, provider = _subject(c, ai_origin=True)
    review_id = unused[0]
    row = c.execute(
        "SELECT material_json FROM application_amendment_reviews WHERE review_id=?", (review_id,)
    ).fetchone()
    card = canonical_json_value(row[0], label="card")
    if damage == "editable":
        card["editable_values"]["amount"] = "999.00"
    elif damage == "proposal_review":
        card["proposal_review"]["merchant"] = "Authenticated-looking wrong merchant"
    else:
        payload = json.loads(card["effective_payload_json"])
        payload["amount"] = "999.00"
        card["effective_payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    new_hash = amendment_review_sha256(card)
    _mutate(
        c,
        "application_amendment_reviews",
        lambda: c.execute(
            "UPDATE application_amendment_reviews SET review_id=?,material_json=?,review_hash=? "
            "WHERE review_id=?",
            ("aer_" + new_hash, canonical_json_text(card), new_hash, review_id),
        ),
    )
    before, calls = _archive(c), provider.calls
    with pytest.raises(LegacyExportError, match="Historical amendment card"):
        adapter.verify_export()
    assert _archive(c) == before and provider.calls == calls == 1


@pytest.mark.parametrize("damage", ["before", "source", "predecessor", "version"])
def test_rehashed_pending_plan_is_not_authenticated_predecessor(legacy_connection, damage):
    c = legacy_connection
    adapter, _, _, pending, _ = _subject(c)
    row = c.execute(
        "SELECT material_json FROM application_correction_plans WHERE plan_id=?", (pending.plan_id,)
    ).fetchone()
    material = canonical_json_value(row[0], label="plan")
    if damage == "before":
        material["before"]["amount"] = "888.00"
    elif damage == "source":
        material["source"]["evidence_id"] = "wrong-source"
    elif damage == "predecessor":
        material["predecessor_hash"] = "e" * 64
    else:
        material["expected_version"] = 50
    _mutate(
        c,
        "application_correction_plans",
        lambda: c.execute(
            "UPDATE application_correction_plans SET material_json=?,plan_hash=?,"
            "predecessor_hash=?,expected_version=? WHERE plan_id=?",
            (
                canonical_json_text(material),
                correction_hash(material),
                material["predecessor_hash"],
                material["expected_version"],
                pending.plan_id,
            ),
        ),
    )
    before = _archive(c)
    with pytest.raises(LegacyExportError, match="authenticated predecessor"):
        adapter.verify_export()
    assert _archive(c) == before


@pytest.mark.parametrize(
    "table",
    [
        "synthetic_sources",
        "synthetic_decisions",
        "synthetic_amendment_evidence_v1",
        "synthetic_correction_decisions",
        "synthetic_ai_receipts",
    ],
)
def test_missing_actual_trust_root_evidence_refuses(legacy_connection, table):
    c = legacy_connection
    adapter, _, _, _, provider = _subject(c, ai_origin=True)
    c.execute(f"DELETE FROM {table}")
    c.commit()
    before, calls = _archive(c), provider.calls
    with pytest.raises(LegacyExportError):
        adapter.verify_export()
    assert _archive(c) == before and provider.calls == calls == 1


def test_real_old_receipt_original_bytes_are_retained_and_missing_bytes_refused(tmp_path):
    values = _prepare_receipt_subject(tmp_path, "legacy-original")
    c, workspace, _, original, review, proposal_id, _, decision_id, *_ = values
    try:
        posting, amendment, corrections = _compose(c, original)
        posted = posting.submit_post(review.review_id, decision_id)
        path = c.execute("SELECT file_path FROM attachments").fetchone()[0]
        from pathlib import Path

        root = Path(path).parent
        adapter = LegacyReadAdapter(
            posting=posting,
            amendment=amendment,
            corrections=corrections,
            attachment_root=root,
            authority_tables=_authority_tables(c),
            export_verifier=_export_verifier(),
        )
        before = _archive(c)
        exported = adapter.verify_export()
        assert _archive(c) == before
        assert len(exported.originals) == 1
        assert exported.originals[0][2] == Path(path).read_bytes()
        assert (
            json.loads(exported.manifest_json)["records"][0]["legacy_record_id"]
            == posted.transaction_public_id
        )
        Path(path).unlink()
        with pytest.raises(LegacyExportError):
            adapter.verify_export()
        assert _archive(c) == before
    finally:
        c.close()


def test_verified_old_export_imports_through_actual_new_owner_and_survives_restart(
    legacy_connection, tmp_path, monkeypatch
):
    from instance_admission_support_v1 import synthetic_config
    from test_lifecycle_v2 import Port

    from finance_core.instance_admission import (
        acquire_production_owner,
        admit_production_instance,
        prepare_production_profile,
    )
    from finance_core.instance_admission.sessions import lifecycle_read_operation
    from finance_core.lifecycle_v2 import LedgerService, RuntimeBinding
    from finance_core.reconciliation import migrations

    # The retained source fixture is actual old 001–060; only the fresh target
    # uses the current new manifest. No real runtime or database is involved.
    adapter, _, _, _, provider = _subject(legacy_connection, ai_origin=True)
    export = adapter.verify_export()
    old_calls = provider.calls
    monkeypatch.setattr(migrations, "TEMP_DB_MIGRATION_PATHS", NEW_MIGRATION_PATHS)
    root = tmp_path / "new-owner"
    root.mkdir(mode=0o700)
    cfg = synthetic_config(root, monkeypatch)
    prepare_production_profile(cfg, operation_id="prepare-new", approval_ref="approved").close()
    admit_production_instance(cfg, operation_id="admit-new", approval_ref="approved")
    owner = acquire_production_owner(cfg)
    binding = RuntimeBinding(cfg.instance_id, "generation-one", "wen", "chat")
    verifier = _export_verifier()
    service = LedgerService(owner, Port(), binding, legacy_verifier=verifier)
    try:
        imported = service.import_verified(export)
        assert len(imported) == 1 and imported[0].version == 2
        record = service.reader().get(imported[0].record_id)
        assert record["values"]["amount"] == "15.75"
        assert record["values"]["description"] == "Human second"
        assert service.reader().pending() == []
        audit = service.reader().full_audit()
        assert audit["fact_version_count"] == 2
        assert service.import_verified(export) == imported
        assert service.reader().full_audit() == audit
        # A new export containing the same economic fact plus a new archived
        # pending proposal cannot duplicate the original or its corrections.
        adapter._corrections.prepare_correction(
            json.loads(export.manifest_json)["records"][0]["legacy_record_id"],
            {"category": "another-pending-category"},
            "Keep second pending plan",
        )
        later = adapter.verify_export()
        assert later.snapshot_sha256 != export.snapshot_sha256
        assert service.import_verified(later) == imported
        assert service.reader().full_audit()["fact_version_count"] == 2
        assert provider.calls == old_calls == 1
        with lifecycle_read_operation(owner) as conn:
            assert conn.execute("SELECT count(*) FROM l2_imports").fetchone()[0] == 2
            assert conn.execute("SELECT count(*) FROM l2_reviews").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM l2_decisions").fetchone()[0] == 0
        # Recreate service and verifier from separately retained root only;
        # the old source/authority/provider objects are not passed to the reader.
        restarted = LedgerService(owner, Port(), binding, legacy_verifier=_export_verifier())
        assert restarted.reader().get(imported[0].record_id)["values"] == record["values"]
        assert restarted.reader().full_audit()["fact_version_count"] == 2
        wrong = LedgerService(
            owner,
            Port(),
            binding,
            legacy_verifier=_export_verifier(b"different independent root" * 2),
        )
        with pytest.raises(LegacyExportError):
            wrong.reader().get(imported[0].record_id)
        with pytest.raises(LegacyExportError):
            wrong.reader().full_audit()
        # A new authenticated reply/card/action can correct an imported fact;
        # old pending plans remain non-executable and grant no decision.
        import hashlib

        from test_lifecycle_v2 import response

        from finance_core.lifecycle_v2 import LifecycleError, VerifiedSource
        from finance_core.lifecycle_v2.contracts import canonical, digest

        with pytest.raises(LifecycleError):
            service.confirm("old-unexecuted-card", "old-approval", "cannot-revive-old-card")
        reply = "change amount to SGD 19.25 reason: Correct imported expense"
        service.port.sources["fresh-correction-source"] = VerifiedSource(
            "fresh-correction-source",
            "wen",
            "chat",
            "fresh-correction-message",
            reply,
            hashlib.sha256(reply.encode()).hexdigest(),
            source_timestamp=1792800000,
        )
        service.port.raw = response(reply, "reply", {"amount": "19.25", "currency": "SGD"})
        correction = service.prepare_correction(
            imported[0].record_id,
            2,
            "fresh-after-import-observation",
            "fresh-after-import",
            reply_evidence_id="fresh-correction-source",
            reason="Correct imported expense",
        )
        service.record_delivery(correction.review_id, "fresh-after-import-delivery")
        receipt = service.confirm(
            correction.review_id, "fresh-after-import-action", "fresh-confirm"
        )
        assert receipt.version == 3
        assert service.reader().get(receipt.record_id)["values"]["amount"] == "19.25"
        assert service.reader().full_audit()["fact_version_count"] == 3
        # Rehashing a modified complete archive cannot manufacture the separate
        # export-root signature, even with otherwise self-consistent DB hashes.
        import sqlite3

        from finance_core.lifecycle_v2.legacy import verify_import_archives

        snapshot_path = tmp_path / "import-proof-snapshot.sqlite"
        service.seal_snapshot(snapshot_path)
        with sqlite3.connect(snapshot_path) as conn:
            conn.row_factory = sqlite3.Row
            archived = conn.execute(
                "SELECT * FROM l2_imports ORDER BY export_id LIMIT 1"
            ).fetchone()
            material = json.loads(archived["material_json"])
            material["archive_json"] += " "
            _mutate(
                conn,
                "l2_imports",
                lambda: conn.execute(
                    "UPDATE l2_imports SET material_json=?,export_sha256=? WHERE export_id=?",
                    (canonical(material), digest(material), archived["export_id"]),
                ),
            )
            with pytest.raises(LegacyExportError):
                verify_import_archives(conn, verifier)
    finally:
        owner.close()
        cfg.close()


def test_receipt_import_retains_original_without_access_to_old_file(tmp_path, monkeypatch):
    import hashlib
    import sqlite3
    from pathlib import Path

    from instance_admission_support_v1 import synthetic_config
    from test_lifecycle_v2 import Port

    from finance_core.instance_admission import (
        acquire_production_owner,
        admit_production_instance,
        prepare_production_profile,
    )
    from finance_core.lifecycle_v2 import LedgerService, RuntimeBinding
    from finance_core.reconciliation import migrations

    values = _prepare_receipt_subject(tmp_path, "legacy-import-original")
    old, _, _, original, review, _, _, decision_id, *_ = values
    owner = cfg = None
    try:
        posting, amendment, corrections = _compose(old, original)
        posting.submit_post(review.review_id, decision_id)
        original_path = Path(old.execute("SELECT file_path FROM attachments").fetchone()[0])
        original_bytes = original_path.read_bytes()
        adapter = LegacyReadAdapter(
            posting=posting,
            amendment=amendment,
            corrections=corrections,
            attachment_root=original_path.parent,
            authority_tables=_authority_tables(old),
            export_verifier=_export_verifier(),
        )
        export = adapter.verify_export()
        monkeypatch.setattr(migrations, "TEMP_DB_MIGRATION_PATHS", NEW_MIGRATION_PATHS)
        root = tmp_path / "fresh-receipt-owner"
        root.mkdir(mode=0o700)
        cfg = synthetic_config(root, monkeypatch)
        prepare_production_profile(cfg, operation_id="prepare", approval_ref="approved").close()
        admit_production_instance(cfg, operation_id="admit", approval_ref="approved")
        owner = acquire_production_owner(cfg)
        binding = RuntimeBinding(cfg.instance_id, "new-receipt-generation", "wen", "chat")
        service = LedgerService(owner, Port(), binding, legacy_verifier=_export_verifier())
        result = service.import_verified(export)
        original_path.unlink()
        old.close()
        old = None
        assert service.reader().get(result[0].record_id)["values"]["amount"] == "12.50"
        assert service.reader().full_audit()["fact_version_count"] == 1
        snapshot = tmp_path / "independent-receipt-snapshot.sqlite"
        service.seal_snapshot(snapshot)
        with sqlite3.connect(snapshot) as detached:
            material = json.loads(
                detached.execute("SELECT material_json FROM l2_imports").fetchone()[0]
            )
            originals = material["originals"]
            assert len(originals) == 1
            assert bytes.fromhex(originals[0]["data_hex"]) == original_bytes
            assert originals[0]["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    finally:
        if old is not None:
            old.close()
        if owner is not None:
            owner.close()
        if cfg is not None:
            cfg.close()


@pytest.fixture(scope="module")
def authenticated_import_inventory(tmp_path_factory):
    """One genuine owner-built import cut, reused only through detached copies."""
    import hashlib

    from conftest import connect_temp_db
    from instance_admission_support_v1 import synthetic_config
    from test_lifecycle_v2 import Port, response

    from finance_core.instance_admission import (
        acquire_production_owner,
        admit_production_instance,
        prepare_production_profile,
    )
    from finance_core.lifecycle_v2 import LedgerService, RuntimeBinding, VerifiedSource
    from finance_core.reconciliation import migrations

    root = tmp_path_factory.mktemp("legacy-authenticated-inventory")
    patch = pytest.MonkeyPatch()
    old = connect_temp_db(root / "old.sqlite")
    owner = cfg = None
    try:
        migrations.apply_migration_paths(old, LEGACY_MIGRATION_PATHS)
        adapter, _, _, _, _ = _subject(old, ai_origin=True)
        export = adapter.verify_export()
        patch.setattr(migrations, "TEMP_DB_MIGRATION_PATHS", NEW_MIGRATION_PATHS)
        target = root / "target"
        target.mkdir(mode=0o700)
        cfg = synthetic_config(target, patch)
        prepare_production_profile(cfg, operation_id="prepare", approval_ref="approved").close()
        admit_production_instance(cfg, operation_id="admit", approval_ref="approved")
        owner = acquire_production_owner(cfg)
        service = LedgerService(
            owner,
            Port(),
            RuntimeBinding(cfg.instance_id, "inventory-generation", "wen", "chat"),
            legacy_verifier=_export_verifier(),
        )
        receipt = service.import_verified(export)[0]
        adapter._corrections.prepare_correction(
            json.loads(export.manifest_json)["records"][0]["legacy_record_id"],
            {"category": "additional-unexecuted-plan"},
            "Retain another plan",
        )
        assert service.import_verified(adapter.verify_export())[0] == receipt
        assert service.reader().full_audit()["fact_version_count"] == 2
        before_native = root / "authenticated-before-native.sqlite"
        service.seal_snapshot(before_native)
        reply = "change amount to SGD 19.25 reason: Correct imported expense"
        service.port.sources["inventory-fresh-reply"] = VerifiedSource(
            "inventory-fresh-reply",
            "wen",
            "chat",
            "inventory-fresh-message",
            reply,
            hashlib.sha256(reply.encode()).hexdigest(),
            source_timestamp=1792800000,
        )
        service.port.raw = response(reply, "reply", {"amount": "19.25", "currency": "SGD"})
        review = service.prepare_correction(
            receipt.record_id,
            2,
            "inventory-observation",
            "inventory-correction",
            reply_evidence_id="inventory-fresh-reply",
            reason="Correct imported expense",
        )
        service.record_delivery(review.review_id, "inventory-delivery")
        service.confirm(review.review_id, "inventory-action", "inventory-confirm")
        assert service.reader().full_audit()["fact_version_count"] == 3
        # Completeness scans belong to full audit/snapshot/held restore, not a
        # bounded point read of an unchanged imported record.
        from finance_core.lifecycle_v2 import legacy

        with patch.context() as point_patch:

            def forbid_archive_scan(*_args):
                raise AssertionError("Point reader attempted the entire legacy archive")

            point_patch.setattr(legacy, "verify_import_archives", forbid_archive_scan)
            assert service.reader().get(receipt.record_id)["values"]["amount"] == "19.25"
        after_native = root / "authenticated-after-native.sqlite"
        service.seal_snapshot(after_native)
        yield before_native, after_native, receipt.record_id
    finally:
        old.close()
        if owner is not None:
            owner.close()
        if cfg is not None:
            cfg.close()
        patch.undo()


def _unchecked_snapshot_mutation(conn, operation):
    """Adversarial detached copy only; original owner and evidence stay intact."""
    conn.execute("PRAGMA foreign_keys=OFF")
    triggers = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
    for name, _sql in triggers:
        conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    operation()
    for _name, sql in triggers:
        conn.execute(sql)
    conn.commit()


def _rehash_remaining_events(conn):
    """Make surviving ordinary events fully self-consistent, without HMAC roots."""
    from finance_core.lifecycle_v2.contracts import canonical, digest

    events = conn.execute("SELECT * FROM l2_events ORDER BY seq").fetchall()
    conn.execute("DELETE FROM l2_events")
    conn.execute("UPDATE l2_facts SET commit_seq=-commit_seq")
    previous = None
    for sequence, event in enumerate(events, 1):
        payload = json.loads(event["payload_json"])
        sha = digest(
            [sequence, event["operation_id"], event["kind"], event["entity_id"], payload, previous]
        )
        conn.execute(
            "UPDATE l2_facts SET commit_seq=? WHERE commit_seq=?", (sequence, -event["seq"])
        )
        conn.execute(
            "INSERT INTO l2_events VALUES(?,?,?,?,?,?,?)",
            (
                sequence,
                event["operation_id"],
                event["kind"],
                event["entity_id"],
                canonical(payload),
                previous,
                sha,
            ),
        )
        previous = sha


@pytest.mark.parametrize(
    "damage",
    [
        "whole_record",
        "latest_version",
        "initial_version_relabelled",
        "record_capsule",
        "import_row",
        "import_event",
        "archive_event",
        "archive_event_entity",
        "archive_event_export_hash",
        "duplicate_archive_event",
        "forged_fact_hashes",
        "no_import_rows_but_event",
        "no_import_rows_but_capsule",
    ],
)
def test_signed_import_inventory_rejects_missing_or_forged_rows(
    authenticated_import_inventory, tmp_path, damage
):
    import shutil
    import sqlite3

    from finance_core.lifecycle_v2.contracts import canonical, digest
    from finance_core.lifecycle_v2.legacy import verify_import_archives
    from finance_core.lifecycle_v2.reader import audit_connection
    from finance_core.lifecycle_v2.service import _fact_material

    original, _, record_id = authenticated_import_inventory
    path = tmp_path / (damage + ".sqlite")
    shutil.copyfile(original, path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row

        def corrupt():
            if damage == "whole_record":
                conn.execute("DELETE FROM l2_facts WHERE record_id=?", (record_id,))
                conn.execute(
                    "DELETE FROM l2_events WHERE kind='import' AND entity_id=?", (record_id,)
                )
            elif damage == "latest_version":
                seq = conn.execute(
                    "SELECT commit_seq FROM l2_facts WHERE record_id=? AND version=2", (record_id,)
                ).fetchone()[0]
                conn.execute("DELETE FROM l2_facts WHERE record_id=? AND version=2", (record_id,))
                conn.execute("DELETE FROM l2_events WHERE seq=?", (seq,))
            elif damage == "initial_version_relabelled":
                first = conn.execute(
                    "SELECT * FROM l2_facts WHERE record_id=? AND version=1", (record_id,)
                ).fetchone()
                second = conn.execute(
                    "SELECT * FROM l2_facts WHERE record_id=? AND version=2", (record_id,)
                ).fetchone()
                conn.execute("DELETE FROM l2_facts WHERE record_id=? AND version=1", (record_id,))
                conn.execute("DELETE FROM l2_events WHERE seq=?", (first["commit_seq"],))
                fields = json.loads(second["values_json"])
                sha = digest(
                    _fact_material(
                        record_id, 1, second["economic_event_id"], second["source_id"], fields, None
                    )
                )
                conn.execute(
                    "UPDATE l2_facts SET version=1,predecessor_sha256=NULL,fact_sha256=? "
                    "WHERE record_id=?",
                    (sha, record_id),
                )
                event = conn.execute(
                    "SELECT * FROM l2_events WHERE seq=?", (second["commit_seq"],)
                ).fetchone()
                payload = json.loads(event["payload_json"])
                payload.update(version=1, legacy_version=0, fact_sha256=sha)
                conn.execute(
                    "UPDATE l2_events SET operation_id=?,payload_json=? WHERE seq=?",
                    (
                        event["operation_id"].rsplit(":", 1)[0] + ":1",
                        canonical(payload),
                        event["seq"],
                    ),
                )
            elif damage == "record_capsule":
                conn.execute(
                    "DELETE FROM l2_archives WHERE kind='record' AND export_id="
                    "(SELECT min(export_id) FROM l2_imports)"
                )
            elif damage == "import_row":
                export_id = conn.execute("SELECT min(export_id) FROM l2_imports").fetchone()[0]
                conn.execute("DELETE FROM l2_archives WHERE export_id=?", (export_id,))
                conn.execute("DELETE FROM l2_imports WHERE export_id=?", (export_id,))
            elif damage in {"import_event", "archive_event"}:
                kind = "import" if damage == "import_event" else "legacy_archive"
                conn.execute(
                    "DELETE FROM l2_events WHERE seq=(SELECT min(seq) FROM l2_events WHERE kind=?)",
                    (kind,),
                )
            elif damage == "archive_event_entity":
                conn.execute(
                    "UPDATE l2_events SET entity_id='wrong-entity' WHERE kind='legacy_archive'"
                )
            elif damage == "archive_event_export_hash":
                event = conn.execute(
                    "SELECT * FROM l2_events WHERE kind='legacy_archive' LIMIT 1"
                ).fetchone()
                payload = json.loads(event["payload_json"])
                payload["export_sha256"] = "f" * 64
                conn.execute(
                    "UPDATE l2_events SET payload_json=? WHERE seq=?",
                    (canonical(payload), event["seq"]),
                )
            elif damage == "duplicate_archive_event":
                event = conn.execute(
                    "SELECT * FROM l2_events WHERE kind='legacy_archive' LIMIT 1"
                ).fetchone()
                seq = conn.execute("SELECT max(seq)+1 FROM l2_events").fetchone()[0]
                conn.execute(
                    "INSERT INTO l2_events VALUES(?,?,?,?,?,?,?)",
                    (
                        seq,
                        "duplicate-archive-event",
                        event["kind"],
                        event["entity_id"],
                        event["payload_json"],
                        event["previous_sha256"],
                        "f" * 64,
                    ),
                )
            elif damage == "forged_fact_hashes":
                previous = None
                for fact in conn.execute(
                    "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version", (record_id,)
                ).fetchall():
                    fields = json.loads(fact["values_json"])
                    fields["amount"] = "999.00"
                    sha = digest(
                        _fact_material(
                            record_id,
                            fact["version"],
                            fact["economic_event_id"],
                            fact["source_id"],
                            fields,
                            previous,
                        )
                    )
                    conn.execute(
                        "UPDATE l2_facts SET values_json=?,predecessor_sha256=?,fact_sha256=? "
                        "WHERE record_id=? AND version=?",
                        (canonical(fields), previous, sha, record_id, fact["version"]),
                    )
                    event = conn.execute(
                        "SELECT payload_json FROM l2_events WHERE seq=?", (fact["commit_seq"],)
                    ).fetchone()
                    payload = json.loads(event[0])
                    payload["fact_sha256"] = sha
                    conn.execute(
                        "UPDATE l2_events SET payload_json=? WHERE seq=?",
                        (canonical(payload), fact["commit_seq"]),
                    )
                    previous = sha
            else:
                # A complete valid prefix rollback has no retained authenticated
                # head and is outside this claim. These retain orphan evidence.
                for table in ("l2_facts", "l2_sources", "l2_imports", "l2_events"):
                    conn.execute("DELETE FROM " + table)
                if damage == "no_import_rows_but_event":
                    conn.execute("DELETE FROM l2_archives")
                    conn.execute(
                        "INSERT INTO l2_events VALUES(1,'orphan','legacy_archive',"
                        "'orphan',?,NULL,?)",
                        (canonical({"export_id": "orphan"}), "a" * 64),
                    )
            _rehash_remaining_events(conn)

        _unchecked_snapshot_mutation(conn, corrupt)
        if damage in {
            "whole_record",
            "latest_version",
            "initial_version_relabelled",
            "forged_fact_hashes",
        }:
            # Ordinary chain/material/inventory checks pass for the surviving
            # self-consistent rows. Independent signed expectations must refuse.
            audit_connection(conn)
        with pytest.raises(LegacyExportError):
            verify_import_archives(conn, _export_verifier())


def test_authenticated_import_union_allows_dedup_and_new_native_versions(
    authenticated_import_inventory,
):
    import sqlite3

    from finance_core.lifecycle_v2.legacy import verify_import_archives
    from finance_core.lifecycle_v2.reader import audit_connection

    _, path, record_id = authenticated_import_inventory
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        verify_import_archives(conn, _export_verifier())
        assert audit_connection(conn)["fact_version_count"] == 3
        assert conn.execute("SELECT count(*) FROM l2_imports").fetchone()[0] == 2
        latest = conn.execute(
            "SELECT values_json FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        assert json.loads(latest[0])["amount"] == "19.25"
