"""Fresh admitted production sessions use unchanged financial business owners."""

from instance_admission_business_support_v1 import (
    production_fixture_persistence,
    run_installed_business_probe,
    run_production_fixture,
)


def test_fixture_transaction_groups_preserve_idle_owners_and_staging_calls():
    import sqlite3
    from types import ModuleType

    fixture = ModuleType("synthetic_fixture")
    observed = []

    def persist(connection, value):
        observed.append(connection.in_transaction)
        connection.execute("INSERT INTO evidence VALUES (?)", (value,))

    fixture._persist = persist
    production = sqlite3.connect(":memory:")
    staging = sqlite3.connect(":memory:")
    try:
        for connection in (production, staging):
            connection.execute("CREATE TABLE evidence(value TEXT)")

        def mixed_fixture(connection):
            assert connection.in_transaction
            fixture._persist(connection, "source")
            connection.commit()
            # The real business owner must receive the same idle connection.
            assert connection is production and not connection.in_transaction
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO evidence VALUES ('business')")
            connection.commit()
            fixture._persist(connection, "approval")
            connection.commit()

        def provider_dispatch():
            assert not production.in_transaction
            fixture._persist(production, "provider-observation")
            assert production.in_transaction
            production.commit()

        with production_fixture_persistence(production, fixture):
            run_production_fixture(production, mixed_fixture)
            assert not production.in_transaction
            # Provider dispatch checks idle before its evidence writer is reached.
            provider_dispatch()
            fixture._persist(staging, "staging-original")
            assert observed == [True, True, True, False]
            staging.rollback()
        assert fixture._persist is persist
        assert production.execute("SELECT value FROM evidence").fetchall() == [
            ("source",),
            ("business",),
            ("approval",),
            ("provider-observation",),
        ]
        assert staging.execute("SELECT value FROM evidence").fetchall() == []
    finally:
        production.close()
        staging.close()


def test_fixture_failure_rolls_back_only_its_owned_group():
    import sqlite3

    import pytest

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE evidence(value TEXT)")

        def failing_fixture(connection):
            connection.execute("INSERT INTO evidence VALUES ('uncommitted')")
            raise RuntimeError("synthetic fixture refusal")

        with pytest.raises(RuntimeError, match="fixture refusal"):
            run_production_fixture(connection, failing_fixture)
        assert not connection.in_transaction
        assert connection.execute("SELECT value FROM evidence").fetchall() == []

        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO evidence VALUES ('caller-owned')")
        with pytest.raises(AssertionError, match="caller-owned"):
            run_production_fixture(connection, failing_fixture)
        assert connection.in_transaction
        assert connection.execute("SELECT value FROM evidence").fetchall() == [("caller-owned",)]
        connection.rollback()
    finally:
        connection.close()


def test_actual_production_text_receipt_corrections_amendment_ai_and_readonly(tmp_path):
    assert run_installed_business_probe(tmp_path / "business") == {
        "text_posted": True,
        "receipt_posted": True,
        "current_versions": [1, 1],
        "economic_events": 2,
        "originals_unchanged": True,
        "readonly_verified": True,
    }


def test_production_connection_keeps_legacy_facades_and_wrong_instance_closed(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    import pytest
    from instance_admission_support_v1 import synthetic_config
    from test_application_posting_recovery_v1 import (
        BINDING,
        NOW,
        _DurablePostingDecisionAuthority,
        _DurableSourceVerifier,
    )

    from finance_core.application.posting import PostingService
    from finance_core.instance_admission import (
        acquire_production_owner,
        admit_production_instance,
        prepare_production_profile,
        production_read_operation,
        production_write_operation,
    )
    from finance_core.parser_proposals import service
    from finance_core.staging_guard import StagingDatabaseError

    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit", approval_ref="approved")
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            before = conn.total_changes
            for operation in (
                lambda: service.confirm_parser_proposal(conn, 999, authenticated_actor_id="human"),
                lambda: service.convert_confirmed_parser_proposal(conn, 999),
                lambda: service.verify_converted_parser_proposal(conn, 999),
            ):
                with pytest.raises(StagingDatabaseError):
                    operation()
                assert conn.total_changes == before and not conn.in_transaction
            wrong = PostingService(
                connection=conn,
                source_verifier=_DurableSourceVerifier(),
                human_decision_authority=_DurablePostingDecisionAuthority(),
                binding=replace(BINDING, instance_id="different-production-instance"),
                clock=lambda: NOW,
            )
            with pytest.raises(StagingDatabaseError, match="instance"):
                wrong.prepare("synthetic-proposal")
            assert conn.total_changes == before and not conn.in_transaction
        with production_read_operation(owner) as conn:
            actual = PostingService(
                connection=conn,
                source_verifier=_DurableSourceVerifier(),
                human_decision_authority=_DurablePostingDecisionAuthority(),
                binding=replace(BINDING, instance_id=config.instance_id),
                clock=lambda: NOW,
            )
            with pytest.raises(StagingDatabaseError):
                actual.prepare("synthetic-proposal")
            assert conn.total_changes == 0 and not conn.in_transaction
    config.close()


def test_old_instance_signed_human_decision_cannot_post_in_fresh_instance(tmp_path):
    """Move only intact signed approval artifacts; B has its own native source."""
    from contextlib import ExitStack
    from dataclasses import replace
    from unittest.mock import patch

    import pytest
    import test_application_posting_recovery_v1 as evidence
    from instance_admission_business_support_v1 import _production_config

    from finance_core.instance_admission import (
        acquire_production_owner,
        admit_production_instance,
        fence_production_instance,
        prepare_production_profile,
        production_write_operation,
    )

    approval_tables = ("synthetic_displays", "synthetic_replies", "synthetic_decisions")
    old_records = {}
    original_binding = evidence.BINDING
    root_a = tmp_path / "instance-a"
    root_a.mkdir()
    config_a, base_a = _production_config(root_a, instance_id="signed-decision-instance-a")
    with ExitStack() as lifetime:
        lifetime.enter_context(config_a)
        lifetime.enter_context(
            patch.dict("os.environ", {"FINANCE_RUNTIME_ROOT": str(base_a / "runtime")})
        )
        lifetime.enter_context(
            patch.object(
                evidence, "BINDING", replace(original_binding, instance_id=config_a.instance_id)
            )
        )
        prepare_production_profile(
            config_a, operation_id="prepare-a", approval_ref="approved"
        ).close()
        admit_production_instance(config_a, operation_id="admit-a", approval_ref="approved")
        with acquire_production_owner(config_a) as owner_a:
            with (
                production_write_operation(owner_a) as conn_a,
                production_fixture_persistence(conn_a, evidence),
            ):
                service_a, review_a, _proposal, _raw, old_decision, _display = (
                    run_production_fixture(conn_a, evidence._prepare_text_subject, "decision-a")
                )
                # The transported decision is first proved legitimate on A.
                assert service_a.submit_post(review_a.review_id, old_decision).state == "finalized"
                verified_old = evidence._load(
                    conn_a, "synthetic_decisions", old_decision, evidence.DECISION_KEY
                )
                assert verified_old["instance_id"] == config_a.instance_id
                for table in approval_tables:
                    old_records[table] = [
                        tuple(row) for row in conn_a.execute(f'SELECT * FROM "{table}"')
                    ]
            fence_production_instance(config_a, operation_id="fence-a", approval_ref="approved")

    root_b = tmp_path / "instance-b"
    root_b.mkdir()
    config_b, base_b = _production_config(root_b, instance_id="signed-decision-instance-b")
    with ExitStack() as lifetime:
        lifetime.enter_context(config_b)
        lifetime.enter_context(
            patch.dict("os.environ", {"FINANCE_RUNTIME_ROOT": str(base_b / "runtime")})
        )
        lifetime.enter_context(
            patch.object(
                evidence, "BINDING", replace(original_binding, instance_id=config_b.instance_id)
            )
        )
        prepare_production_profile(
            config_b, operation_id="prepare-b", approval_ref="approved"
        ).close()
        admit_production_instance(config_b, operation_id="admit-b", approval_ref="approved")
        with acquire_production_owner(config_b) as owner_b:
            with (
                production_write_operation(owner_b) as conn_b,
                production_fixture_persistence(conn_b, evidence),
            ):
                service_b, review_b, _proposal, _raw, current_decision, _display = (
                    run_production_fixture(conn_b, evidence._prepare_text_subject, "decision-b")
                )
                # No A database, source, authorization, or final fact is adopted.
                conn_b.execute("BEGIN IMMEDIATE")
                for table, records in old_records.items():
                    conn_b.executemany(f'INSERT INTO "{table}" VALUES (?,?,?)', records)
                conn_b.commit()
                assert (
                    evidence._load(
                        conn_b, "synthetic_decisions", old_decision, evidence.DECISION_KEY
                    )
                    == verified_old
                )
                before = conn_b.total_changes
                with pytest.raises(ValueError, match="exact current display"):
                    service_b.submit_post(review_b.review_id, old_decision)
                assert conn_b.total_changes == before and not conn_b.in_transaction
                for table in (
                    "application_posting_decisions",
                    "application_posting_attempts",
                    "parser_proposal_authorizations",
                    "parser_proposal_conversion_audit",
                    "transactions",
                ):
                    assert conn_b.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
                # B can still use its own exact current signed approval once.
                result_b = service_b.submit_post(review_b.review_id, current_decision)
                assert result_b.state == "finalized"
                assert conn_b.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
