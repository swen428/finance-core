"""Observable production schema-contract regressions on synthetic profiles."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from finance_core.instance_admission import (
    PENDING_FILENAME,
    STATE_FILENAME,
    ApplicationSchemaContract,
    InstanceAdmissionError,
    TrustedInstanceConfig,
    acquire_production_owner,
    admit_production_instance,
    inspect_instance_admission,
    prepare_production_profile,
    production_read_operation,
    production_write_operation,
    resume_authority_transition,
)
from finance_core.staging_guard import StagingDatabaseError
from tests.instance_admission_support_v1 import synthetic_config

_CONFIG_IDENTITY = "synthetic-config-v1"
_CORE_IDENTITY = "synthetic-core-v1"
_INSTANCE_ID = "synthetic-instance-v1"
_PROFILE_ID = "synthetic"
_APP_SCHEMA_ID = "synthetic-application-schema-v1"


def _application_ddl(*, trigger_value: str = "a  b") -> tuple[str, ...]:
    return (
        "CREATE TABLE synthetic_app_events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
        "CREATE TABLE synthetic_app_event_audit ("
        "event_id TEXT NOT NULL REFERENCES SYNTHETIC_APP_EVENTS(event_id), "
        "action TEXT NOT NULL)",
        "CREATE INDEX synthetic_app_events_payload_idx ON synthetic_app_events(payload)",
        "CREATE TRIGGER synthetic_app_events_capture "
        "AFTER INSERT ON synthetic_app_events BEGIN "
        "INSERT INTO synthetic_app_event_audit(event_id, action) "
        f"VALUES(NEW.event_id, '{trigger_value}'); END",
        "CREATE TABLE synthetic_app_sequence_probe ("
        "sequence_id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)",
    )


def _config_like(
    original: TrustedInstanceConfig, application_schema: ApplicationSchemaContract
) -> TrustedInstanceConfig:
    return TrustedInstanceConfig(
        control_root=original.control_root,
        locator_root=original.locator_root,
        profile_id=original.profile_id,
        layout=original.layout,
        instance_id=original.instance_id,
        config_identity=original.config_identity,
        core_identity=original.core_identity,
        approval_verifier=original.approval_verifier,
        application_schema=application_schema,
    )


def _child_config(
    control_root: Path,
    locator_root: Path,
    schema_id: str,
    ddl_statements: tuple[str, ...],
) -> TrustedInstanceConfig:
    layout = "linux" if Path(locator_root).name == "finance-codex" else "mac"
    return TrustedInstanceConfig(
        control_root=Path(control_root),
        locator_root=Path(locator_root),
        profile_id=_PROFILE_ID,
        layout=layout,
        instance_id=_INSTANCE_ID,
        config_identity=_CONFIG_IDENTITY,
        core_identity=_CORE_IDENTITY,
        approval_verifier=lambda reference, _action, _binding: reference == "approved",
        application_schema=ApplicationSchemaContract(schema_id, ddl_statements),
    )


def _crash_fence_after_pending_sync(
    control_root: Path,
    locator_root: Path,
    schema_id: str,
    ddl_statements: tuple[str, ...],
    sender,
) -> None:
    """Exit after the exact pending journal is durably created, before publish."""
    config = _child_config(control_root, locator_root, schema_id, ddl_statements)
    control_stat = Path(control_root).stat()
    control_identity = (control_stat.st_dev, control_stat.st_ino)
    real_fsync = os.fsync

    def crash_after_control_sync(fd: int) -> None:
        real_fsync(fd)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) == control_identity:
            os._exit(89)

    os.fsync = crash_after_control_sync
    try:
        from finance_core.instance_admission import fence_production_instance

        fence_production_instance(
            config, operation_id="schema-fence-pending", approval_ref="approved"
        )
    except BaseException as exc:
        sender.send(("error", type(exc).__name__))
        raise
    sender.send(("returned", None))


def _spawn(target, args: tuple[object, ...]):
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(*args, sender))
    process.start()
    sender.close()
    return process, receiver


def _expect_read_rejected(config: TrustedInstanceConfig) -> None:
    with acquire_production_owner(config) as owner:
        with production_read_operation(owner):
            pytest.fail("schema or ledger drift opened a production read session")


def _expect_write_rejected(config: TrustedInstanceConfig) -> None:
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner):
            pytest.fail("schema or ledger drift opened a production write session")


def _read_valid_extension(config: TrustedInstanceConfig) -> tuple[int, str]:
    with acquire_production_owner(config) as owner:
        with production_read_operation(owner) as conn:
            row = conn.execute(
                "SELECT event_id, action FROM synthetic_app_event_audit ORDER BY rowid LIMIT 1"
            ).fetchone()
            ledger_count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            return int(ledger_count), f"{row['event_id']}:{row['action']}"


def _read_sequence_value(config: TrustedInstanceConfig) -> int:
    with acquire_production_owner(config) as owner:
        with production_read_operation(owner) as conn:
            row = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name=?",
                ("synthetic_app_sequence_probe",),
            ).fetchone()
            assert row is not None
            return int(row[0])


def _write_tamper(database: Path, mutate) -> None:
    connection = sqlite3.connect(database)
    try:
        mutate(connection)
        connection.commit()
    finally:
        connection.close()


def _mutate_core_foreign_key(connection: sqlite3.Connection) -> None:
    candidate = None
    for table, sql in connection.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        if (
            sql
            and connection.execute(
                f'PRAGMA foreign_key_list("{str(table).replace(chr(34), chr(34) * 2)}")'
            ).fetchone()
        ):
            candidate = (str(table), str(sql))
            break
    assert candidate is not None, "Core migration fixture should include a foreign key"
    table, original_sql = candidate
    reference = re.search(
        r"\bREFERENCES\s+(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))",
        original_sql,
        flags=re.IGNORECASE,
    )
    assert reference is not None
    changed_sql = (
        original_sql[: reference.start()]
        + "REFERENCES pr11_tampered_target"
        + original_sql[reference.end() :]
    )
    assert changed_sql != original_sql
    schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute(
        "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
        (changed_sql, table),
    )
    connection.execute(f"PRAGMA schema_version={schema_version + 1}")
    connection.execute("PRAGMA writable_schema=OFF")


def _mutate_sqlite_sequence_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()
    assert row is not None and row[0], "AUTOINCREMENT fixture must create sqlite_sequence"
    original_sql = str(row[0])
    assert original_sql.endswith(")"), original_sql
    changed_sql = original_sql[:-1] + ", pr11_unexpected TEXT)"
    assert changed_sql != original_sql
    schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute(
        "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='sqlite_sequence'",
        (changed_sql,),
    )
    connection.execute(f"PRAGMA schema_version={schema_version + 1}")
    connection.execute("PRAGMA writable_schema=OFF")


def _tamper_cases() -> tuple[tuple[str, object], ...]:
    def core_index(connection: sqlite3.Connection) -> None:
        columns = connection.execute('PRAGMA table_xinfo("transactions")').fetchall()
        assert columns
        column = str(columns[0][1]).replace('"', '""')
        connection.execute(f'CREATE INDEX pr11_untrusted_core_index ON transactions("{column}")')

    return (
        (
            "unknown object",
            lambda conn: conn.execute("CREATE TABLE pr11_unknown_object (id INTEGER PRIMARY KEY)"),
        ),
        (
            "unknown object whose name contains the SQLite prefix wildcard",
            lambda conn: conn.execute("CREATE TABLE sqlitex_probe (id INTEGER PRIMARY KEY)"),
        ),
        (
            "missing extension table",
            lambda conn: conn.execute("DROP TABLE synthetic_app_event_audit"),
        ),
        (
            "Core column",
            lambda conn: conn.execute(
                "ALTER TABLE transactions ADD COLUMN pr11_unexpected_core TEXT"
            ),
        ),
        (
            "extension column",
            lambda conn: conn.execute(
                "ALTER TABLE synthetic_app_events ADD COLUMN pr11_unexpected_app TEXT"
            ),
        ),
        (
            "Core foreign key",
            _mutate_core_foreign_key,
        ),
        (
            "extension foreign key",
            lambda conn: (
                conn.execute("DROP TABLE synthetic_app_event_audit"),
                conn.execute(
                    "CREATE TABLE synthetic_app_event_audit ("
                    "event_id TEXT NOT NULL, action TEXT NOT NULL)"
                ),
            ),
        ),
        (
            "Core index",
            core_index,
        ),
        (
            "extension index",
            lambda conn: (
                conn.execute("DROP INDEX synthetic_app_events_payload_idx"),
                conn.execute(
                    "CREATE INDEX synthetic_app_events_payload_idx "
                    "ON synthetic_app_events(event_id)"
                ),
            ),
        ),
        (
            "Core trigger",
            lambda conn: conn.execute(
                "CREATE TRIGGER pr11_untrusted_core_trigger AFTER UPDATE ON transactions "
                "BEGIN SELECT 1; END"
            ),
        ),
        (
            "missing extension trigger",
            lambda conn: conn.execute("DROP TRIGGER synthetic_app_events_capture"),
        ),
        (
            "extension trigger literal semantics",
            lambda conn: (
                conn.execute("DROP TRIGGER synthetic_app_events_capture"),
                conn.execute(_application_ddl(trigger_value="a b")[3]),
            ),
        ),
        (
            "SQLite internal sequence-table columns",
            _mutate_sqlite_sequence_schema,
        ),
        (
            "hidden staging authorization object",
            lambda conn: conn.execute("CREATE TABLE _staging_authorization (id TEXT PRIMARY KEY)"),
        ),
        (
            "migration ledger structure",
            lambda conn: conn.execute(
                "ALTER TABLE schema_migrations ADD COLUMN pr11_unexpected_ledger TEXT"
            ),
        ),
        (
            "migration ledger row checksum",
            lambda conn: conn.execute(
                "UPDATE schema_migrations SET checksum_sha256=? WHERE migration_id='060'",
                ("0" * 64,),
            ),
        ),
        (
            "migration ledger schema fingerprint",
            lambda conn: conn.execute(
                "UPDATE schema_migrations SET schema_fingerprint=? WHERE migration_id='060'",
                ("0" * 64,),
            ),
        ),
        (
            "migration ledger applied time",
            lambda conn: conn.execute(
                "UPDATE schema_migrations SET applied_at='pr11-tampered' WHERE migration_id='060'"
            ),
        ),
    )


def _restore_database(database: Path, contents: bytes) -> None:
    with database.open("r+b") as stream:
        stream.seek(0)
        stream.write(contents)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def test_application_schema_contract_is_immutable_and_bounded() -> None:
    contract = ApplicationSchemaContract("synthetic-boundary", ("CREATE TABLE x (id)",))
    with pytest.raises(FrozenInstanceError):
        contract.schema_id = "changed"  # type: ignore[misc]

    max_statements = tuple(f"CREATE TABLE synthetic_bound_{index} (id)" for index in range(256))
    assert len(ApplicationSchemaContract("max-statements", max_statements).ddl_statements) == 256
    with pytest.raises(ValueError):
        ApplicationSchemaContract(
            "too-many-statements",
            tuple(f"CREATE TABLE synthetic_over_{index} (id)" for index in range(257)),
        )

    prefix = "CREATE TABLE synthetic_size_bound (value TEXT)"
    at_limit = prefix + (" " * (262144 - len(prefix.encode("utf-8"))))
    assert len(ApplicationSchemaContract("max-bytes", (at_limit,)).ddl_statements[0].encode()) == (
        262144
    )
    with pytest.raises(ValueError):
        ApplicationSchemaContract("too-many-bytes", (at_limit + " ",))


@pytest.mark.parametrize(
    "label,ddl_statements",
    [
        (
            "DML initializer",
            (
                "CREATE TABLE synthetic_bad_init (id INTEGER)",
                "INSERT INTO synthetic_bad_init VALUES (1)",
            ),
        ),
        ("Core table mutation", ("CREATE TABLE transactions (id INTEGER)",)),
        (
            "Core index mutation",
            ("CREATE INDEX synthetic_bad_core_idx ON transactions(public_id)",),
        ),
        (
            "TEMP shadow",
            ("CREATE TEMP TABLE transactions (id INTEGER)",),
        ),
        ("attached database", ("ATTACH DATABASE 'synthetic.db' AS secondary",)),
        (
            "virtual table",
            ("CREATE VIRTUAL TABLE synthetic_bad_virtual USING fts5(body)",),
        ),
        (
            "hidden staging object",
            ("CREATE TABLE _staging_authorization (id TEXT PRIMARY KEY)",),
        ),
        (
            "case-insensitive hidden staging object",
            ("CREATE TABLE _STAGING_AUTHORIZATION (id TEXT PRIMARY KEY)",),
        ),
        (
            "trigger mutates Core",
            (
                "CREATE TABLE synthetic_bad_trigger_source (id INTEGER)",
                "CREATE TRIGGER synthetic_bad_core_trigger "
                "AFTER INSERT ON synthetic_bad_trigger_source BEGIN "
                "DELETE FROM transactions; END",
            ),
        ),
        (
            "extension foreign key targets Core",
            ("CREATE TABLE synthetic_bad_core_fk (id TEXT REFERENCES transactions(public_id))",),
        ),
        (
            "extension foreign key targets undeclared table",
            (
                "CREATE TABLE synthetic_bad_unknown_fk ("
                "id TEXT REFERENCES synthetic_missing_parent(id))",
            ),
        ),
    ],
)
def test_unsafe_application_ddl_fails_before_database_or_authority_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, ddl_statements: tuple[str, ...]
) -> None:
    schema_id = f"synthetic-reject-{label.replace(' ', '-')}"
    contract = ApplicationSchemaContract(schema_id, ddl_statements)
    try:
        config = synthetic_config(tmp_path, monkeypatch, application_schema=contract)
    except InstanceAdmissionError:
        pass
    else:
        try:
            with pytest.raises(InstanceAdmissionError):
                prepare_production_profile(
                    config, operation_id="prepare-rejected-schema", approval_ref="approved"
                )
        finally:
            config.close()

    control_root = tmp_path / "control"
    profile = tmp_path / ("Application Support" if config_layout_is_mac() else "finance-codex")
    database = _expected_database_path(profile)
    assert not (control_root / STATE_FILENAME).exists(), label
    assert not (control_root / PENDING_FILENAME).exists(), label
    assert not database.exists(), label


def config_layout_is_mac() -> bool:
    import sys

    return sys.platform == "darwin"


def _expected_database_path(locator_root: Path) -> Path:
    if locator_root.name == "Application Support":
        profile_root = locator_root / "Finance-Codex/profiles/synthetic"
    else:
        profile_root = locator_root / "profiles/synthetic"
    return profile_root / "runtime/database/finance.db"


def test_declared_schema_is_bound_reopens_and_seals_complete_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = ApplicationSchemaContract(_APP_SCHEMA_ID, _application_ddl())
    config = synthetic_config(tmp_path, monkeypatch, application_schema=schema)
    profile = prepare_production_profile(
        config, operation_id="schema-prepare", approval_ref="approved"
    )
    database = profile.live_database
    profile.close()
    prepared = inspect_instance_admission(config)
    assert prepared["state"] == "PREPARED"
    assert len(prepared["ledger_sha256"]) == 64

    admitted = admit_production_instance(
        config, operation_id="schema-admit", approval_ref="approved"
    )
    assert admitted["state"] == "ACTIVE"
    with acquire_production_owner(config) as owner:
        with production_write_operation(owner) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO synthetic_app_events(event_id, payload) VALUES (?, ?)",
                ("event-1", "synthetic payload"),
            )
            conn.commit()

    # sqlite_sequence is mutable AUTOINCREMENT data; a normal increasing value
    # must remain writable even though the table's declared SQL is sealed.
    for expected_sequence in (1, 2):
        with acquire_production_owner(config) as owner:
            with production_write_operation(owner) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO synthetic_app_sequence_probe(payload) VALUES (?)",
                    (f"sequence-{expected_sequence}",),
                )
                conn.commit()
        assert _read_sequence_value(config) == expected_sequence

    # A real owner/connection close and a new owner/session retain the pinned
    # application table, its declared foreign key/index, and its trigger effect.
    assert _read_valid_extension(config) == (61, "event-1:a  b")
    with acquire_production_owner(config) as owner:
        with production_read_operation(owner) as conn:
            foreign_keys = conn.execute(
                'PRAGMA foreign_key_list("synthetic_app_event_audit")'
            ).fetchall()
    assert [(row[2].casefold(), row[3], row[4]) for row in foreign_keys] == [
        ("synthetic_app_events", "event_id", "event_id")
    ]
    database_bytes = database.read_bytes()

    variants = (
        ApplicationSchemaContract("other-schema-id", _application_ddl()),
        ApplicationSchemaContract(
            _APP_SCHEMA_ID,
            (
                _application_ddl()[0].replace(
                    "payload TEXT NOT NULL", "payload TEXT NOT NULL CHECK(length(payload)<1024)"
                ),
                *_application_ddl()[1:],
            ),
        ),
    )
    for variant in variants:
        alternate = _config_like(config, variant)
        try:
            with pytest.raises(InstanceAdmissionError):
                inspect_instance_admission(alternate)
        finally:
            alternate.close()
        assert database.read_bytes() == database_bytes

    # Prove the real internal-table drift is rejected on both ACTIVE read and
    # write opens. The DB inode and sealed authority record stay unchanged.
    active_authority_path = config.control_root / STATE_FILENAME
    active_authority_bytes = active_authority_path.read_bytes()
    database_identity_before_internal_tamper = (database.stat().st_dev, database.stat().st_ino)
    _write_tamper(database, _mutate_sqlite_sequence_schema)
    try:
        with pytest.raises(InstanceAdmissionError):
            _expect_read_rejected(config)
        with pytest.raises(InstanceAdmissionError):
            _expect_write_rejected(config)
    finally:
        _restore_database(database, database_bytes)
    assert (database.stat().st_dev, database.stat().st_ino) == (
        database_identity_before_internal_tamper
    )
    assert active_authority_path.read_bytes() == active_authority_bytes
    assert _read_valid_extension(config) == (61, "event-1:a  b")
    assert _read_sequence_value(config) == 2

    # Process death after pending-journal sync produces no successful fence
    # acknowledgement or state publication.
    state_path = config.control_root / STATE_FILENAME
    pending_path = config.control_root / PENDING_FILENAME
    prior_state = state_path.read_bytes()
    process, receiver = _spawn(
        _crash_fence_after_pending_sync,
        (config.control_root, config.locator_root, schema.schema_id, schema.ddl_statements),
    )
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail("schema fence crash injector did not reach pending publication boundary")
    receiver.close()
    assert process.exitcode == 89
    assert pending_path.is_file()
    assert state_path.read_bytes() == prior_state

    # Exact resume rechecks the current database contract before publication.
    database_before_resume_tamper = database.read_bytes()
    database_identity_before_resume = (database.stat().st_dev, database.stat().st_ino)
    for label, mutate in (
        (
            "declared application schema drift",
            lambda conn: conn.execute(
                "ALTER TABLE synthetic_app_events ADD COLUMN pr11_pending_drift TEXT"
            ),
        ),
        ("SQLite internal sequence-table drift", _mutate_sqlite_sequence_schema),
    ):
        _write_tamper(database, mutate)
        try:
            with pytest.raises(InstanceAdmissionError):
                resume_authority_transition(
                    config, operation_id="schema-fence-pending", approval_ref="approved"
                )
            assert state_path.read_bytes() == prior_state, label
            assert pending_path.is_file(), label
            assert (database.stat().st_dev, database.stat().st_ino) == (
                database_identity_before_resume
            ), label
        finally:
            _restore_database(database, database_before_resume_tamper)
        assert (database.stat().st_dev, database.stat().st_ino) == (
            database_identity_before_resume
        ), label
    fenced = resume_authority_transition(
        config, operation_id="schema-fence-pending", approval_ref="approved"
    )
    assert fenced["state"] == "FENCED"
    assert not pending_path.exists()
    published_state = state_path.read_bytes()
    repeated_fence_resume = resume_authority_transition(
        config, operation_id="schema-fence-pending", approval_ref="approved"
    )
    assert repeated_fence_resume["state"] == "FENCED"
    assert state_path.read_bytes() == published_state
    history = json.loads(published_state)["history"]
    assert sum(entry["operation_id"] == "schema-fence-pending" for entry in history) == 1
    assert _read_valid_extension(config) == (61, "event-1:a  b")

    # Each mutation is applied to the same disposable SQLite file and then
    # restored byte-for-byte while preserving its pinned inode. This keeps the
    # case count broad without rerunning 60 Core migrations for every defect.
    authority_bytes = state_path.read_bytes()
    for label, mutate in _tamper_cases():
        pristine = database.read_bytes()
        before_id = (database.stat().st_dev, database.stat().st_ino)
        _write_tamper(database, mutate)
        try:
            try:
                _expect_read_rejected(config)
            except StagingDatabaseError:
                pass
            else:
                pytest.fail(f"{label} did not fail closed")
        finally:
            _restore_database(database, pristine)
        after_id = (database.stat().st_dev, database.stat().st_ino)
        assert after_id == before_id, label
        assert state_path.read_bytes() == authority_bytes, label
        assert not pending_path.exists(), label
        assert _read_valid_extension(config) == (61, "event-1:a  b"), label

    config.close()
