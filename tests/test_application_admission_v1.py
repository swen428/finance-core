"""Synthetic durable admission ports; no test authority is shipped in runtime."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from finance_core.application import admission
from finance_core.application.review import get_proposal_review
from finance_core.intake.raw_text_repository import create_raw_intake_record, save_parser_proposal
from finance_core.parsers.text_expense_parser import parse_text_expense

pytestmark = pytest.mark.migrated_staging_snapshot

NOW = 2000
BINDING = admission.TrustedBinding(
    "synthetic-instance",
    "synthetic-human",
    "synthetic-submitter",
    "synthetic-source-domain",
    "synthetic-source-key",
    "synthetic-human-domain",
    "synthetic-human-key",
)
SOURCE_KEY = b"synthetic source root only in tests"
DECISION_KEY = b"independent synthetic human root only in tests"
ROOT = Path(__file__).resolve().parents[1]


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def signature(domain: str, value: object, key: bytes) -> str:
    return hmac.new(key, (domain + "\x00" + canonical(value)).encode(), hashlib.sha256).hexdigest()


def persist(conn, table, record_id, data, key):
    conn.execute(
        f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?)",
        (record_id, canonical(data), signature(table, data, key)),
    )


def load(conn, table, record_id, key):
    row = conn.execute(
        f"SELECT material,signature FROM {table} WHERE id=?", (record_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Missing synthetic durable evidence")
    data = json.loads(row[0])
    if not hmac.compare_digest(row[1], signature(table, data, key)):
        raise ValueError("Synthetic evidence signature mismatch")
    return data


class SyntheticSqliteSourceVerifier:
    def __init__(self, key=SOURCE_KEY):
        self.key = key
        self.calls = []

    def verify_persisted(self, connection, intake_public_id):
        self.calls.append((connection, intake_public_id, connection.in_transaction))
        rows = connection.execute("SELECT id FROM synthetic_sources").fetchall()
        matches = [load(connection, "synthetic_sources", row[0], self.key) for row in rows]
        matches = [data for data in matches if data["intake_public_id"] == intake_public_id]
        if len(matches) != 1:
            raise ValueError("Source association is absent or ambiguous")
        data = matches[0]
        events = [load(connection, "synthetic_sources", row[0], self.key) for row in rows]
        if sum(item["source_event_id"] == data["source_event_id"] for item in events) != 1:
            raise ValueError("Source event is not unique")
        raw = connection.execute(
            "SELECT raw_input,source_content_hash FROM raw_intake_records WHERE public_id=?",
            (intake_public_id,),
        ).fetchone()
        if (
            raw is None
            or hashlib.sha256(raw[0].encode()).hexdigest() != data["source_content_hash"]
        ):
            raise ValueError("Actual durable source content mismatch")
        if raw[1] != "sha256:" + data["source_content_hash"]:
            raise ValueError("Raw source hash mismatch")
        material = {key: value for key, value in data.items() if key != "evidence_digest"}
        if digest(material) != data["evidence_digest"]:
            raise ValueError("Source digest mismatch")
        return admission.VerifiedSource(**data)


class SyntheticSqliteHumanAuthority:
    def __init__(self, key=DECISION_KEY):
        self.key = key
        self.calls = []

    def verify_persisted(self, connection, decision_record_id, expected):
        self.calls.append((connection, decision_record_id, expected, connection.in_transaction))
        data = load(connection, "synthetic_decisions", decision_record_id, self.key)
        display = load(connection, "synthetic_displays", data["display_id"], self.key)
        material = {key: value for key, value in data.items() if key != "decision_digest"}
        if (
            digest(material) != data["decision_digest"]
            or digest(display) != data["display_evidence_digest"]
        ):
            raise ValueError("Decision/display digest mismatch")
        if (
            display["state"] != "delivered"
            or display["direct"] is not True
            or display["origin"] != "human"
            or display["private"] is not True
            or display["current"] is not True
            or display["human_principal_id"] != expected.binding.human_principal_id
            or display["instance_id"] != expected.binding.instance_id
            or display["reply_decision_id"] != decision_record_id
            or display["source_evidence_id"] != expected.source.evidence_id
            or display["source_evidence_digest"] != expected.source.evidence_digest
            or admission.review_projection_sha256(display["projection"])
            != expected.review_projection_hash
        ):
            raise ValueError("No exact private current delivered display/reply binding")
        return admission.VerifiedHumanDecision(**data)


@pytest.fixture()
def durable_synthetic(tmp_path, migrated_temp_db_connection):
    conn = migrated_temp_db_connection
    for table in ("synthetic_sources", "synthetic_displays", "synthetic_decisions"):
        conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY,material TEXT,signature TEXT)")
    raw = create_raw_intake_record(
        conn,
        "lunch SGD 12.50",
        source_type="manual_entry",
        source_channel="manual",
        public_id="synthetic-intake",
        received_at="2026-01-01T00:00:00Z",
    )
    parsed = parse_text_expense(
        raw["raw_input"], raw_input_reference=raw["public_id"], source_type="manual_entry"
    )
    proposal = save_parser_proposal(conn, raw["id"], parsed)
    conn.commit()
    projection = get_proposal_review(conn, proposal["public_id"])
    source = dict(
        schema=admission.SOURCE_SCHEMA,
        namespace=BINDING.source_namespace,
        key_id=BINDING.source_key_id,
        instance_id=BINDING.instance_id,
        submission_client_id=BINDING.submission_client_id,
        evidence_id="synthetic-source",
        intake_public_id=raw["public_id"],
        source_event_id="synthetic-event",
        source_content_hash=hashlib.sha256(raw["raw_input"].encode()).hexdigest(),
        source_occurred_at=1000,
        received_at=1001,
    )
    source["evidence_digest"] = digest(source)
    display = dict(
        state="delivered",
        direct=True,
        origin="human",
        private=True,
        current=True,
        human_principal_id=BINDING.human_principal_id,
        instance_id=BINDING.instance_id,
        reply_decision_id="synthetic-decision",
        source_evidence_id=source["evidence_id"],
        source_evidence_digest=source["evidence_digest"],
        projection=projection,
    )
    decision = dict(
        schema=admission.DECISION_SCHEMA,
        namespace=BINDING.decision_namespace,
        key_id=BINDING.decision_key_id,
        instance_id=BINDING.instance_id,
        human_principal_id=BINDING.human_principal_id,
        decision_id="synthetic-decision",
        source_evidence_id=source["evidence_id"],
        source_evidence_digest=source["evidence_digest"],
        proposal_public_id=proposal["public_id"],
        proposal_version=projection["proposal_version"],
        proposal_content_hash=projection["effective_content_hash"],
        review_projection_hash=admission.review_projection_sha256(projection),
        display_id="synthetic-display",
        display_evidence_digest=digest(display),
        action="confirm",
        issued_at=1500,
        expires_at=2500,
        consumed=False,
    )
    decision["decision_digest"] = digest(decision)
    persist(conn, "synthetic_sources", source["evidence_id"], source, SOURCE_KEY)
    persist(conn, "synthetic_displays", decision["display_id"], display, DECISION_KEY)
    persist(conn, "synthetic_decisions", decision["decision_id"], decision, DECISION_KEY)
    conn.commit()
    conn.execute("PRAGMA journal_mode=WAL")
    # Close/reopen the actual file; proof cannot depend on fixture object memory.
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    reopened = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    reopened.row_factory = sqlite3.Row
    yield reopened, proposal["public_id"], path
    reopened.close()


def service(conn, *, source=None, authority=None, binding=BINDING, clock=lambda: NOW):
    return admission.AdmissionService(
        connection=conn,
        source_verifier=source or SyntheticSqliteSourceVerifier(),
        human_decision_authority=authority or SyntheticSqliteHumanAuthority(),
        binding=binding,
        clock=clock,
    )


def check(instance, proposal):
    return instance.check_human_decision(proposal, "synthetic-decision")


def mutate(path, table, changes, *, sign=True):
    conn = sqlite3.connect(path)
    data = json.loads(conn.execute(f"SELECT material FROM {table}").fetchone()[0])
    data.update(changes)
    key = SOURCE_KEY if table == "synthetic_sources" else DECISION_KEY
    digest_key = {
        "synthetic_sources": "evidence_digest",
        "synthetic_decisions": "decision_digest",
    }.get(table)
    if digest_key and digest_key not in changes:
        data[digest_key] = digest({key: value for key, value in data.items() if key != digest_key})
    if sign:
        record_id = conn.execute(f"SELECT id FROM {table}").fetchone()[0]
        persist(conn, table, record_id, data, key)
    else:
        conn.execute(f"UPDATE {table} SET material=?", (canonical(data),))
    conn.commit()
    conn.close()


def test_actual_durable_ports_close_reopen_read_only_and_repeated_inspection(durable_synthetic):
    conn, proposal, _path = durable_synthetic
    source, authority = SyntheticSqliteSourceVerifier(), SyntheticSqliteHumanAuthority()
    instance = service(conn, source=source, authority=authority)
    before = conn.serialize()
    first = instance.admit_source("synthetic-intake")
    decision = check(instance, proposal)
    assert first.evidence_id == decision.source_evidence_id == "synthetic-source"
    assert decision == check(instance, proposal)
    assert conn.serialize() == before and conn.total_changes == 0 and not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM d2_posting_decisions").fetchone()[0] == 0
    assert all(call[0] is conn and call[-1] is True for call in source.calls + authority.calls)
    assert authority.calls[0][2].binding.human_principal_id != BINDING.submission_client_id
    assert not hasattr(decision, "authorization_id") and not hasattr(decision, "confirmed")


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "finance_d2_telegram_source_context_v1"),
        ("namespace", "legacy"),
        ("key_id", "wrong"),
        ("instance_id", "copy"),
        ("submission_client_id", "other"),
        ("intake_public_id", "other"),
        ("evidence_id", ""),
        ("source_event_id", ""),
        ("source_content_hash", "a" * 64),
        ("evidence_digest", "bad"),
        ("source_occurred_at", True),
        ("source_occurred_at", 1002),
        ("received_at", 2001),
        ("received_at", 0),
    ],
)
def test_source_invalid_durable_evidence_refused(durable_synthetic, field, value):
    conn, _proposal, path = durable_synthetic
    mutate(path, "synthetic_sources", {field: value})
    before = conn.serialize()
    with pytest.raises(admission.AdmissionError):
        service(conn).admit_source("synthetic-intake")
    assert conn.serialize() == before and conn.total_changes == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "d2_post_v1"),
        ("namespace", "legacy"),
        ("key_id", "wrong"),
        ("instance_id", "copy"),
        ("human_principal_id", BINDING.submission_client_id),
        ("decision_id", "other"),
        ("source_evidence_id", "other"),
        ("source_evidence_digest", "b" * 64),
        ("proposal_public_id", "other"),
        ("proposal_version", 2),
        ("proposal_version", True),
        ("proposal_content_hash", "c" * 64),
        ("review_projection_hash", "d" * 64),
        ("display_id", ""),
        ("display_evidence_digest", "e" * 64),
        ("action", "reject"),
        ("action", "edit"),
        ("action", "approved_by_ai"),
        ("issued_at", 2001),
        ("issued_at", False),
        ("expires_at", 2000),
        ("consumed", True),
        ("consumed", 0),
        ("decision_digest", "bad"),
    ],
)
def test_decision_durable_crossbindings_expiry_consumption_and_types_refused(
    durable_synthetic, field, value
):
    conn, proposal, path = durable_synthetic
    mutate(path, "synthetic_decisions", {field: value})
    before = conn.serialize()
    with pytest.raises(admission.AdmissionError):
        check(service(conn), proposal)
    assert conn.serialize() == before and conn.total_changes == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "UNKNOWN"},
        {"direct": False},
        {"origin": "ai"},
        {"private": False},
        {"current": False},
        {"human_principal_id": "other"},
        {"instance_id": "other"},
        {"reply_decision_id": "other"},
        {"source_evidence_id": "other"},
        {"source_evidence_digest": "a" * 64},
        {"projection": {"amount": "12.50"}},
    ],
)
def test_signed_display_must_be_exact_private_current_delivered_reply(durable_synthetic, changes):
    conn, proposal, path = durable_synthetic
    mutate(path, "synthetic_displays", changes)
    # An independent root may sign a valid but incompatible display. Bind its
    # new digest in the decision so refusal exercises display meaning itself.
    writable = sqlite3.connect(path)
    display = load(writable, "synthetic_displays", "synthetic-display", DECISION_KEY)
    writable.close()
    mutate(path, "synthetic_decisions", {"display_evidence_digest": digest(display)})
    with pytest.raises(admission.AdmissionError):
        check(service(conn), proposal)


@pytest.mark.parametrize(
    "table", ["synthetic_sources", "synthetic_displays", "synthetic_decisions"]
)
def test_unsigned_tamper_or_missing_durable_record_refused(durable_synthetic, table):
    conn, proposal, path = durable_synthetic
    mutate(path, table, {"untrusted_client_claim": True}, sign=False)
    with pytest.raises(admission.AdmissionError):
        check(service(conn), proposal)
    writable = sqlite3.connect(path)
    writable.execute(f"DELETE FROM {table}")
    writable.commit()
    writable.close()
    with pytest.raises(admission.AdmissionError):
        check(service(conn), proposal)


@pytest.mark.parametrize("root", ["source", "human"])
def test_composition_wrong_signing_root_fails(durable_synthetic, root):
    conn, proposal, _path = durable_synthetic
    instance = service(
        conn,
        source=SyntheticSqliteSourceVerifier(b"wrong") if root == "source" else None,
        authority=SyntheticSqliteHumanAuthority(b"wrong") if root == "human" else None,
    )
    with pytest.raises(admission.AdmissionError):
        check(instance, proposal)


def test_service_rechecks_port_claims_after_real_persistent_verification(durable_synthetic):
    conn, proposal, _path = durable_synthetic

    class MisboundAuthority(SyntheticSqliteHumanAuthority):
        def verify_persisted(self, connection, decision_record_id, expected):
            verified = super().verify_persisted(connection, decision_record_id, expected)
            return replace(verified, human_principal_id=BINDING.submission_client_id)

    with pytest.raises(admission.AdmissionError):
        check(service(conn, authority=MisboundAuthority()), proposal)


def test_consumption_change_and_expiry_are_rechecked_every_call(durable_synthetic):
    conn, proposal, path = durable_synthetic
    instance = service(conn)
    check(instance, proposal)
    mutate(path, "synthetic_decisions", {"consumed": True})
    with pytest.raises(admission.AdmissionError):
        check(instance, proposal)
    mutate(path, "synthetic_decisions", {"consumed": False})
    with pytest.raises(admission.AdmissionError):
        check(service(conn, clock=lambda: 2500), proposal)


@pytest.mark.parametrize("clock_value", [None, True, 0, -1, "2000"])
def test_invalid_trusted_clock_fails_closed(durable_synthetic, clock_value):
    conn, proposal, _path = durable_synthetic
    with pytest.raises(admission.AdmissionError):
        check(service(conn, clock=lambda: clock_value), proposal)


def test_operational_methods_accept_only_refs_and_no_forged_dto(durable_synthetic):
    conn, proposal, _path = durable_synthetic
    assert list(inspect.signature(admission.AdmissionService.admit_source).parameters) == [
        "self",
        "intake_public_id",
    ]
    assert list(inspect.signature(admission.AdmissionService.check_human_decision).parameters) == [
        "self",
        "proposal_public_id",
        "decision_record_id",
    ]
    instance = service(conn)
    for value in ("", " synthetic-intake", {"actor": "human", "verified": True}, None):
        with pytest.raises(admission.AdmissionError):
            instance.admit_source(value)
    with pytest.raises(TypeError):
        instance.check_human_decision(proposal, "synthetic-decision", actor="synthetic-human")
    with pytest.raises(TypeError):
        instance.admit_source("synthetic-intake", verifier=SyntheticSqliteSourceVerifier())
    with pytest.raises(admission.AdmissionError):
        service(conn, binding=replace(BINDING, instance_id=""))


def test_existing_caller_transaction_is_preserved(durable_synthetic):
    conn, proposal, _path = durable_synthetic
    conn.execute("BEGIN")
    check(service(conn), proposal)
    assert conn.in_transaction and conn.total_changes == 0
    with pytest.raises(admission.AdmissionError):
        check(service(conn, clock=lambda: 2500), proposal)
    assert conn.in_transaction and conn.total_changes == 0
    conn.rollback()


def test_projection_hash_binds_all_fields_and_separate_domain():
    first = {"amount": "12.50", "merchant": "lunch", "ambiguity_indicators": []}
    assert admission.review_projection_sha256(first) != digest(first)
    for key in first:
        assert admission.review_projection_sha256(first) != admission.review_projection_sha256(
            {**first, key: None}
        )


def test_old_telegram_source_is_still_verifiable_without_relabelling(migrated_temp_db_connection):
    from finance_core.telegram_source_context import (
        TelegramSourceContext,
        record_telegram_source_context,
        require_telegram_source_context,
    )

    conn = migrated_temp_db_connection
    raw = create_raw_intake_record(
        conn,
        "old synthetic lunch",
        public_id="old-synthetic-intake",
        source_metadata={
            "external_source_id": "telegram:chat:10",
            "source_message_id": "10",
        },
    )
    context = TelegramSourceContext("old-human", "old-account", "chat", "old-binding", "10")
    stored = record_telegram_source_context(
        conn, raw_intake_record_id=raw["id"], context=context, captured_at="2026-01-01T00:00:00Z"
    )
    conn.commit()
    before = conn.serialize()
    assert (
        require_telegram_source_context(conn, raw_intake_record_id=raw["id"], context=context)
        == stored
    )
    with pytest.raises(admission.AdmissionError):
        service(conn).admit_source(raw["public_id"])
    assert conn.serialize() == before


def test_cold_admission_import_and_durable_call_with_platform_imports_blocked(durable_synthetic):
    _conn, proposal, path = durable_synthetic
    script = """
import importlib
import importlib.abc
import sqlite3
import sys
from scripts.check_application_dependencies import is_platform
class BlockPlatform(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if is_platform(fullname):
            raise ImportError('Platform import blocked: ' + fullname)
sys.meta_path.insert(0, BlockPlatform())
sys.path.insert(0, 'tests')
import test_application_admission_v1 as synthetic
from finance_core.application.admission import AdmissionService
conn = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
before = conn.serialize()
result = synthetic.check(synthetic.service(conn), sys.argv[2])
assert result.decision_id == 'synthetic-decision'
assert conn.total_changes == 0 and conn.serialize() == before
for attempted in (
    'finance_core.receipt_staging_runner.cli',
    'finance_core.intake.telegram_text_adapter',
    'finance_core.intake.macos_vision_receipt_ocr',
):
    try:
        importlib.import_module(attempted)
    except ImportError as error:
        assert str(error).startswith('Platform import blocked: '), (attempted, error)
        blocked_name = str(error).removeprefix('Platform import blocked: ')
        assert is_platform(blocked_name), (attempted, blocked_name)
        assert attempted == blocked_name or attempted.startswith(blocked_name + '.'), (
            attempted, blocked_name
        )
    else:
        raise AssertionError('Platform import was not blocked: ' + attempted)
assert not any(is_platform(name) for name in sys.modules)
conn.close()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, path, proposal],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_proposal_and_adapter_reads_share_one_snapshot(durable_synthetic):
    conn, proposal, path = durable_synthetic

    class ConcurrentProposalSource(SyntheticSqliteSourceVerifier):
        changed = False

        def verify_persisted(self, connection, intake_public_id):
            if not self.changed:
                self.changed = True
                writer = sqlite3.connect(path)
                payload = json.loads(
                    writer.execute(
                        "SELECT parsed_payload FROM parser_outputs WHERE public_id=?", (proposal,)
                    ).fetchone()[0]
                )
                payload["description"] = "a concurrently changed visible description"
                writer.execute(
                    "UPDATE parser_outputs SET parsed_payload=?,normalized_payload=? "
                    "WHERE public_id=?",
                    (canonical(payload), canonical(payload), proposal),
                )
                writer.commit()
                writer.close()
            return super().verify_persisted(connection, intake_public_id)

    authority = SyntheticSqliteHumanAuthority()
    instance = service(conn, source=ConcurrentProposalSource(), authority=authority)
    observed = check(instance, proposal)
    assert observed.proposal_content_hash == authority.calls[0][2].proposal_content_hash
    # The first read snapshot observes a coherent predecessor. A later check
    # must observe the concurrent content change and refuse the old decision.
    with pytest.raises(admission.AdmissionError):
        check(instance, proposal)
    assert conn.total_changes == 0 and not conn.in_transaction


def test_duplicate_native_event_is_not_durable_unique_source(durable_synthetic):
    conn, _proposal, path = durable_synthetic
    writer = sqlite3.connect(path)
    existing = load(writer, "synthetic_sources", "synthetic-source", SOURCE_KEY)
    copy = {**existing, "evidence_id": "second-evidence", "intake_public_id": "second-intake"}
    copy["evidence_digest"] = digest(
        {key: value for key, value in copy.items() if key != "evidence_digest"}
    )
    persist(writer, "synthetic_sources", "second-evidence", copy, SOURCE_KEY)
    writer.commit()
    writer.close()
    with pytest.raises(admission.AdmissionError):
        service(conn).admit_source("synthetic-intake")


@pytest.mark.parametrize("which", ["source", "decision"])
def test_port_must_return_the_exact_new_evidence_type(durable_synthetic, which):
    conn, proposal, _path = durable_synthetic

    class WrongSourceType(SyntheticSqliteSourceVerifier):
        def verify_persisted(self, connection, intake_public_id):
            return vars(super().verify_persisted(connection, intake_public_id))

    class WrongDecisionType(SyntheticSqliteHumanAuthority):
        def verify_persisted(self, connection, decision_record_id, expected):
            return vars(super().verify_persisted(connection, decision_record_id, expected))

    with pytest.raises(admission.AdmissionError):
        check(
            service(
                conn,
                source=WrongSourceType() if which == "source" else None,
                authority=WrongDecisionType() if which == "decision" else None,
            ),
            proposal,
        )
