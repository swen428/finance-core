"""Journal bidirectional integrity and append-only controls on synthetic clones."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest
from lifecycle_recovery_material_support_v1 import make_service, stage_expense

from finance_core.lifecycle_v2._finalization_journal_v1 import (
    PreparedFinalizationError,
    _verify_journal,
)
from finance_core.lifecycle_v2.contracts import canonical, digest
from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1
from finance_core.sqlite_connection import configure_sqlite_connection


@pytest.fixture(scope="module")
def journal_copies(tmp_path_factory):
    directory = tmp_path_factory.mktemp("prepared-journal")
    patch = pytest.MonkeyPatch()
    service, port, cfg, auth = make_service(directory, patch)
    try:
        _, _, review, _ = stage_expense(service, port, "expense", stage="delivery")
        port.register_action(service, review, "delivery-expense", "action-expense")
        api = PreparedFinalizationServiceV1(service, archive_source=port, authenticator=auth)
        ref = api.prepare(review.review_id, "action-expense", "finalize")
        source = cfg.locator_root
        from pathlib import Path

        database = Path(cfg._binding["profile_root"]) / "runtime/database/finance.db"
        prepared = directory / "prepared.sqlite"
        shutil.copyfile(database, prepared)
        api.apply_offline("finalize", expected_intent_sha256=ref.intent_sha256)
        applied = directory / "applied.sqlite"
        shutil.copyfile(database, applied)
        yield SimpleNamespace(prepared=prepared, applied=applied, ref=ref, root=source)
    finally:
        service.owner.close()
        cfg.close()
        patch.undo()


def open_copy(case, tmp_path, *, applied=False, tamper=False):
    path = tmp_path / "synthetic-copy.sqlite"
    shutil.copyfile(case.applied if applied else case.prepared, path)
    conn = sqlite3.connect(path)
    configure_sqlite_connection(conn)
    if tamper:
        conn.execute("PRAGMA foreign_keys=OFF")
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'l2_%'"
            )
        ]
        for name in names:
            conn.execute("DROP TRIGGER " + name)
    return conn


@pytest.mark.parametrize(
    "damage",
    [
        "missing-package",
        "orphan-package",
        "body-hash",
        "before-frontier",
        "fake-applied",
        "fake-cancel",
        "half-fact",
        "receipt",
        "event-bytes",
    ],
)
def test_journal_cannot_downgrade_corruption_to_absence(journal_copies, tmp_path, damage):
    applied = damage in ("fake-cancel", "half-fact", "receipt", "event-bytes")
    conn = open_copy(journal_copies, tmp_path, applied=applied, tamper=True)
    try:
        row = conn.execute("SELECT * FROM l2_finalization_intents").fetchone()
        body = json.loads(row["body_json"])
        if damage == "missing-package":
            conn.execute("DELETE FROM l2_finalization_materials")
        elif damage == "orphan-package":
            payload = b"{}"
            conn.execute(
                "INSERT INTO l2_finalization_materials VALUES(?,?)",
                (hashlib.sha256(payload).hexdigest(), payload),
            )
        elif damage == "body-hash":
            conn.execute("UPDATE l2_finalization_intents SET intent_sha256=?", ("f" * 64,))
        elif damage == "before-frontier":
            body["before"]["event_seq"] -= 1
            conn.execute(
                "UPDATE l2_finalization_intents SET body_json=?,intent_sha256=?",
                (canonical(body), digest(body)),
            )
        elif damage == "fake-applied":
            fact = body["append"]["fact"]
            receipt = {
                "record_id": fact["record_id"],
                "version": fact["version"],
                "fact_sha256": fact["fact_sha256"],
                "commit_seq": fact["commit_seq"],
                "status": "LOCAL_POSTED",
            }
            conn.execute(
                "INSERT INTO l2_finalization_outcomes VALUES(?,?,?,?,?)",
                (
                    "finalize",
                    row["intent_sha256"],
                    "APPLIED_OFFLINE",
                    body["prepared_at"],
                    canonical(receipt),
                ),
            )
        elif damage == "fake-cancel":
            conn.execute(
                "UPDATE l2_finalization_outcomes SET outcome='CANCELLED_OFFLINE',receipt_json=NULL"
            )
        elif damage == "half-fact":
            conn.execute("DELETE FROM l2_facts")
        elif damage == "receipt":
            conn.execute("UPDATE l2_finalization_outcomes SET receipt_json='{}'")
        else:
            conn.execute(
                "UPDATE l2_events SET event_sha256=? WHERE operation_id='finalize'", ("e" * 64,)
            )
        with pytest.raises(PreparedFinalizationError, match="."):
            _verify_journal(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("table", ["intents", "materials", "outcomes"])
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE", "INSERT OR IGNORE", "INSERT OR REPLACE"])
def test_schema_preserves_control_rows_even_conflict_algorithms(
    journal_copies, tmp_path, table, verb
):
    conn = open_copy(journal_copies, tmp_path, applied=True)
    name = "l2_finalization_" + table
    try:
        before = tuple(tuple(row) for row in conn.execute("SELECT * FROM " + name))
        if verb == "UPDATE":
            column = {"intents": "body_json", "materials": "payload", "outcomes": "receipt_json"}[
                table
            ]
            sql = f"UPDATE {name} SET {column}={column}"
        elif verb == "DELETE":
            sql = "DELETE FROM " + name
        else:
            sql = f"{verb} INTO {name} SELECT * FROM {name}"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
        assert tuple(tuple(row) for row in conn.execute("SELECT * FROM " + name)) == before
        _verify_journal(conn)
    finally:
        conn.close()


def test_journal_capacity_rejects_actual_one_over_hard_intent_count(journal_copies, tmp_path):
    conn = open_copy(journal_copies, tmp_path, tamper=True)
    try:
        conn.execute("DELETE FROM l2_finalization_intents")
        conn.executemany(
            "INSERT INTO l2_finalization_intents VALUES(?,?,?,?,?,?)",
            (
                (f"op-{index}", f"review-{index}", f"action-{index}", f"{index:064x}", "{}", 1)
                for index in range(1025)
            ),
        )
        with pytest.raises(PreparedFinalizationError) as error:
            _verify_journal(conn)
        assert error.value.code == "journal_limit_exceeded"
    finally:
        conn.close()


def test_same_hash_with_changed_actual_package_bytes_rejects(journal_copies, tmp_path):
    conn = open_copy(journal_copies, tmp_path, tamper=True)
    try:
        conn.execute("UPDATE l2_finalization_materials SET payload=x'7b7d'")
        with pytest.raises(PreparedFinalizationError) as error:
            _verify_journal(conn)
        assert error.value.code == "journal_mismatch"
    finally:
        conn.close()


def test_global_stored_bytes_limit_is_inclusive_and_cannot_split_bypass(journal_copies, tmp_path):
    conn = open_copy(journal_copies, tmp_path, tamper=True)
    try:
        conn.execute("DELETE FROM l2_finalization_intents")
        conn.execute("DELETE FROM l2_finalization_materials")
        for marker in (b"a", b"b", b"c", b"d"):
            payload = marker * (16 * 1024 * 1024)
            conn.execute(
                "INSERT INTO l2_finalization_materials VALUES(?,?)",
                (hashlib.sha256(payload).hexdigest(), payload),
            )
        # Exactly 64MiB reaches content/orphan checks, rather than capacity refusal.
        with pytest.raises(PreparedFinalizationError) as exact:
            _verify_journal(conn)
        assert exact.value.code == "journal_mismatch"
        payload = b"+"  # Distinct fifth object exceeds the global cap by one byte.
        conn.execute(
            "INSERT INTO l2_finalization_materials VALUES(?,?)",
            (hashlib.sha256(payload).hexdigest(), payload),
        )
        with pytest.raises(PreparedFinalizationError) as over:
            _verify_journal(conn)
        assert over.value.code == "journal_limit_exceeded"
    finally:
        conn.close()


def test_global_intent_count_exact_cap_reaches_content_checks(journal_copies, tmp_path):
    conn = open_copy(journal_copies, tmp_path, tamper=True)
    try:
        conn.execute("DELETE FROM l2_finalization_intents")
        conn.executemany(
            "INSERT INTO l2_finalization_intents VALUES(?,?,?,?,?,?)",
            (
                (f"op-{index}", f"review-{index}", f"action-{index}", f"{index:064x}", "{}", 1)
                for index in range(1024)
            ),
        )
        with pytest.raises(PreparedFinalizationError) as exact:
            _verify_journal(conn)
        assert exact.value.code == "journal_mismatch"
    finally:
        conn.close()


def test_actual_session_gate_rejects_corrupt_bytes_with_original_schema(tmp_path, monkeypatch):
    from pathlib import Path

    from finance_core.instance_admission import acquire_production_owner
    from finance_core.instance_admission.sessions import production_write_operation

    service, port, cfg, auth = make_service(tmp_path, monkeypatch)
    try:
        _, _, review, _ = stage_expense(service, port, "expense", stage="delivery")
        port.register_action(service, review, "delivery-expense", "action-expense")
        api = PreparedFinalizationServiceV1(service, archive_source=port, authenticator=auth)
        api.prepare(review.review_id, "action-expense", "finalize")
        service.owner.close()
        path = Path(cfg._binding["profile_root"]) / "runtime/database/finance.db"
        conn = sqlite3.connect(path)
        try:
            name = "l2_finalization_materials_no_update"
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            conn.execute("DROP TRIGGER " + name)
            conn.execute("UPDATE l2_finalization_materials SET payload=x'7b7d'")
            conn.execute(sql)
            conn.commit()
        finally:
            conn.close()
        service.owner = acquire_production_owner(cfg)
        with pytest.raises(PreparedFinalizationError, match="bytes differ"):
            with production_write_operation(service.owner):
                raise AssertionError("corrupt journal escaped actual session gate")
    finally:
        service.owner.close()
        cfg.close()


def test_cancelled_old_review_fact_cannot_hide_behind_removed_decision(journal_copies, tmp_path):
    conn = open_copy(journal_copies, tmp_path, applied=True, tamper=True)
    try:
        conn.execute(
            "UPDATE l2_finalization_outcomes SET outcome='CANCELLED_OFFLINE',receipt_json=NULL"
        )
        conn.execute("DELETE FROM l2_events WHERE operation_id='finalize'")
        conn.execute("DELETE FROM l2_decisions WHERE operation_id='finalize'")
        with pytest.raises(PreparedFinalizationError, match="Unapplied intent"):
            _verify_journal(conn)
    finally:
        conn.close()


def test_journal_append_exact_integer_rejects_boolean_alias(journal_copies, tmp_path):
    conn = open_copy(journal_copies, tmp_path, tamper=True)
    try:
        row = conn.execute("SELECT * FROM l2_finalization_intents").fetchone()
        body = json.loads(row["body_json"])
        body["append"]["fact"]["version"] = True
        body["append"]["decision"]["version"] = True
        conn.execute(
            "UPDATE l2_finalization_intents SET body_json=?,intent_sha256=?",
            (canonical(body), digest(body)),
        )
        with pytest.raises(PreparedFinalizationError):
            _verify_journal(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "damage",
    [
        "missing-owned-prefix-row",
        "low-seq-orphan-fact",
        "missing-prefix-event",
        "pending-unowned-source",
    ],
)
def test_fixed_historical_prefix_and_pending_inventory_still_reject_orphans(
    journal_copies, tmp_path, damage
):
    conn = open_copy(
        journal_copies, tmp_path, applied=damage != "pending-unowned-source", tamper=True
    )
    try:
        body = json.loads(
            conn.execute("SELECT body_json FROM l2_finalization_intents").fetchone()[0]
        )
        if damage == "missing-owned-prefix-row":
            conn.execute("DELETE FROM l2_proposals")
        elif damage == "missing-prefix-event":
            conn.execute("DELETE FROM l2_events WHERE seq=1")
        elif damage == "low-seq-orphan-fact":
            original = body["append"]["fact"]
            conn.execute(
                "INSERT INTO l2_facts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "orphan-record",
                    1,
                    "orphan-economic",
                    original["source_id"],
                    None,
                    original["values_json"],
                    None,
                    "b" * 64,
                    1,
                ),
            )
        else:
            columns = list(conn.execute("SELECT * FROM l2_sources LIMIT 1").fetchone())
            columns[0] = "unowned-source"
            columns[1] = "unowned-source-key"
            columns[2] = "unowned-evidence"
            conn.execute("INSERT INTO l2_sources VALUES(?,?,?,?,?,?,?,?,?,?)", columns)
        with pytest.raises(PreparedFinalizationError):
            _verify_journal(conn)
    finally:
        conn.close()
