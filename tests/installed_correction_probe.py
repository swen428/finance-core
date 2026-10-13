"""Offline probe for correction behavior in installed wheel and sdist bytes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _correction_posting(connection, original, authority):
    from finance_core.application.posting import PostingService
    from tests import test_application_posting_recovery_v1 as posting

    return PostingService(
        connection=connection,
        source_verifier=original._source_port,
        human_decision_authority=original._decision_port,
        binding=original._binding,
        clock=lambda: posting.NOW + 5,
        correction_decision_authority=authority,
    )


def _record_original_transaction(connection, transaction_id):
    return tuple(
        connection.execute(
            "SELECT * FROM transactions WHERE public_id=?", (transaction_id,)
        ).fetchone()
    )


def _run_text(root, connection_factory, posting, correction):
    from finance_core.application.independent_corrections import IndependentCorrectionService

    path = root / "installed-text.sqlite"
    connection = connection_factory(path)
    service, posted, authority = correction._posted_text(connection, "installed-text")
    current_time = {"value": posting.NOW + 5}
    corrections = correction._correction_service(
        service, authority=authority, clock=lambda: current_time["value"]
    )
    original_effective = corrections.read_effective(posted.transaction_public_id)
    original_transaction = _record_original_transaction(connection, posted.transaction_public_id)
    first = corrections.prepare_correction(
        posted.transaction_public_id,
        correction._six_field_patch(),
        "installed text correction",
    )
    first_decision = correction._persist_correction_approval(
        connection, first, decision_id="installed-text-first"
    )
    current_time["value"] += 1
    first_result = corrections.submit(first.plan_id, first_decision)
    second_decision_id = "installed-text-second"
    correction._close_correction_display(
        connection,
        first_decision,
        superseded_at=current_time["value"] + 1,
        replacement_decision_id=second_decision_id,
    )
    current_time["value"] += 2
    second = corrections.prepare_correction(
        posted.transaction_public_id,
        {"category": "travel"},
        "installed metadata-only correction",
    )
    second_decision = correction._persist_correction_approval(
        connection, second, decision_id=second_decision_id
    )
    current_time["value"] += 1
    second_result = corrections.submit(second.plan_id, second_decision)
    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    connection.close()

    reopened = connection_factory(database_path)
    try:
        reopened_posting = _correction_posting(reopened, service, authority)
        reopened_corrections = IndependentCorrectionService(
            reopened_posting,
            human_decision_authority=authority,
            clock=lambda: current_time["value"],
        )
        recovered = reopened_corrections.recover(first.plan_id)
        replay = reopened_corrections.submit_correction(first.plan_id, first_decision)
        status = reopened_posting.get_status(posted.attempt_id)
        reopened_effective = reopened_corrections.read_effective(posted.transaction_public_id)
        assert recovered is not None
        assert recovered.applied.version == 1 and recovered.current.version == 2
        assert replay.recovered and replay.applied.version == 1 and replay.current.version == 2
        assert recovered.applied.fields.as_dict() == first_result.applied.fields.as_dict()
        assert status.applied.version == 0 and status.current.version == 2
        assert status.applied.fields.as_dict() == original_effective.fields.as_dict()
        assert status.current.fields.category == "travel"
        assert (
            reopened_effective.original_source.source_json
            == original_effective.original_source.source_json
        )
        assert (
            reopened_effective.original_source.source_hash
            == original_effective.original_source.source_hash
        )
        assert _record_original_transaction(reopened, posted.transaction_public_id) == (
            original_transaction
        )
        assert reopened.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
        assert (
            reopened.execute(
                "SELECT count(*) FROM application_correction_versions WHERE target_id=?",
                (posted.transaction_public_id,),
            ).fetchone()[0]
            == 2
        )
        assert second_result.current.version == 2
        return {
            "route": "text",
            "applied": status.applied.version,
            "current": status.current.version,
            "fields": status.current.fields.as_dict(),
        }
    finally:
        reopened.close()


def _run_receipt(root, connection_factory, posting, correction):
    from finance_core.application.independent_corrections import IndependentCorrectionService

    values = posting._prepare_receipt_subject(root, "installed-receipt")
    (
        connection,
        _workspace,
        _manifest,
        original,
        review,
        _proposal,
        _intake,
        decision_id,
        _display,
        _source_verifier,
        _decision_authority,
    ) = values
    posted = original.submit_post(review.review_id, decision_id)
    authority = correction._SyntheticCorrectionDecisionAuthority()
    service = _correction_posting(connection, original, authority)
    current_time = {"value": posting.NOW + 5}
    corrections = correction._correction_service(
        service, authority=authority, clock=lambda: current_time["value"]
    )
    original_effective = corrections.read_effective(posted.transaction_public_id)
    original_transaction = _record_original_transaction(connection, posted.transaction_public_id)
    first = corrections.prepare_correction(
        posted.transaction_public_id,
        correction._six_field_patch(),
        "installed receipt correction",
    )
    first_decision = correction._persist_correction_approval(
        connection, first, decision_id="installed-receipt-first"
    )
    current_time["value"] += 1
    first_result = corrections.submit(first.plan_id, first_decision)
    second_decision_id = "installed-receipt-second"
    correction._close_correction_display(
        connection,
        first_decision,
        superseded_at=current_time["value"] + 1,
        replacement_decision_id=second_decision_id,
    )
    current_time["value"] += 2
    second = corrections.prepare_correction(
        posted.transaction_public_id,
        {"description": "Second receipt memo"},
        "installed receipt metadata-only correction",
    )
    second_decision = correction._persist_correction_approval(
        connection, second, decision_id=second_decision_id
    )
    current_time["value"] += 1
    second_result = corrections.submit(second.plan_id, second_decision)
    database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
    connection.close()

    reopened = connection_factory(database_path)
    try:
        reopened_posting = _correction_posting(reopened, original, authority)
        reopened_corrections = IndependentCorrectionService(
            reopened_posting,
            human_decision_authority=authority,
            clock=lambda: current_time["value"],
        )
        recovered = reopened_corrections.recover(first.plan_id)
        replay = reopened_corrections.submit_correction(first.plan_id, first_decision)
        status = reopened_posting.get_status(posted.attempt_id)
        reopened_effective = reopened_corrections.read_effective(posted.transaction_public_id)
        assert recovered is not None
        assert recovered.applied.version == 1 and recovered.current.version == 2
        assert replay.recovered and replay.applied.version == 1 and replay.current.version == 2
        assert status.applied.version == 0 and status.current.version == 2
        assert status.applied.fields.as_dict() == original_effective.fields.as_dict()
        assert status.current.fields.description == "Second receipt memo"
        assert status.applied.snapshot_hash == original_effective.snapshot_hash
        assert status.current.snapshot_hash != status.applied.snapshot_hash
        assert (
            reopened_effective.original_source.source_json
            == original_effective.original_source.source_json
        )
        assert (
            reopened_effective.original_source.source_hash
            == original_effective.original_source.source_hash
        )
        receipt_material = json.loads(
            reopened.execute(
                "SELECT material_json FROM application_correction_receipt_facts "
                "WHERE correction_id=(SELECT correction_id FROM application_correction_plans "
                "WHERE plan_id=?)",
                (second.plan_id,),
            ).fetchone()[0]
        )["value"]
        receipt_fact = json.loads(receipt_material["fact_json"])["value"]
        assert receipt_fact["bookkeeping_metadata"] == {
            "version": "application_bookkeeping_metadata_v1",
            "description": "Second receipt memo",
            "category": "meals",
        }
        snapshot = reopened.execute(
            "SELECT input_payload_json,previous_snapshot_public_id "
            "FROM authoritative_calculation_snapshots WHERE snapshot_public_id=?",
            (second_result.current.snapshot_id,),
        ).fetchone()
        snapshot_inputs = json.loads(snapshot["input_payload_json"])["value"]
        assert snapshot_inputs["bookkeeping_metadata"] == receipt_fact["bookkeeping_metadata"]
        assert snapshot["previous_snapshot_public_id"] == first_result.current.snapshot_id
        assert _record_original_transaction(reopened, posted.transaction_public_id) == (
            original_transaction
        )
        assert reopened.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
        assert (
            reopened.execute(
                "SELECT count(*) FROM application_correction_receipt_facts WHERE target_id=?",
                (posted.transaction_public_id,),
            ).fetchone()[0]
            == 2
        )
        assert second_result.current.version == 2
        return {
            "route": "receipt",
            "applied": status.applied.version,
            "current": status.current.version,
            "fields": status.current.fields.as_dict(),
        }
    finally:
        reopened.close()


def main() -> None:
    install_dir, helper_root, outside_root, runtime_root, source_root = map(Path, sys.argv[1:6])
    os.environ["FINANCE_RUNTIME_ROOT"] = str(runtime_root)
    os.environ["FINANCE_TEST_MODE"] = "1"
    sys.path.insert(0, str(helper_root))
    sys.path.insert(0, str(helper_root / "runtime-dependencies"))
    sys.path.insert(0, str(install_dir))

    import finance_core

    assert Path(finance_core.__file__).resolve().is_relative_to(install_dir.resolve())
    assert not any(Path(item).resolve() == source_root.resolve() for item in sys.path if item)
    from tests import test_application_posting_recovery_v1 as posting
    from tests import test_independent_corrections_v1 as correction
    from tests.conftest import connect_temp_db

    data_root = outside_root / "installed-correction-data" / install_dir.name
    data_root.mkdir(parents=True, exist_ok=True)
    result = {
        "module_file": finance_core.__file__,
        "sys_path_has_checkout": any(
            Path(item).resolve() == source_root.resolve() for item in sys.path if item
        ),
        "text": _run_text(data_root, connect_temp_db, posting, correction),
        "receipt": _run_receipt(data_root, connect_temp_db, posting, correction),
    }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
