"""Migration ledger, immutable checksum, adoption, and deterministic replay tests."""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import finance_core.reconciliation.migrations as migration_runtime
from finance_core.reconciliation.migrations import (
    LIVE_DB_PATH,
    MIGRATION_029_FILENAME,
    MIGRATION_029_ID,
    MIGRATION_029_PREFLIGHT_ARTIFACT,
    MIGRATION_RUNNER_VERSION,
    TEMP_DB_MIGRATION_PATHS,
    MigrationChecksumError,
    MigrationExecutionError,
    MigrationHistoryError,
    MigrationPathManifest,
    MigrationPreflightArtifact,
    MigrationSchemaDriftError,
    apply_migration_paths,
    build_migration_manifest,
    canonical_schema_payload,
    migration_file_checksum,
    migration_ledger_rows,
    schema_fingerprint,
    verify_migration_history,
)

FROZEN_TIME = "2026-07-13T00:00:00+00:00"


def _clock() -> str:
    return FROZEN_TIME


_M029_PATH: Path = next(p for p in TEMP_DB_MIGRATION_PATHS if p.name == MIGRATION_029_FILENAME)


def _connection(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migration(tmp_path: Path, name: str, sql: str) -> Path:
    path = tmp_path / name
    path.write_text(sql, encoding="utf-8")
    return path


def _copied_manifest(tmp_path: Path) -> tuple[MigrationPathManifest, Path, Path]:
    migrations_dir = tmp_path / "copied-migrations"
    migrations_dir.mkdir()
    copied_paths: list[Path] = []
    for source in TEMP_DB_MIGRATION_PATHS:
        copied = migrations_dir / source.name
        copied.write_bytes(source.read_bytes())
        copied_paths.append(copied)
    preflight = tmp_path / "copied-migration_029_preflight.py"
    preflight.write_bytes(MIGRATION_029_PREFLIGHT_ARTIFACT.read_bytes())
    return (
        MigrationPathManifest(
            paths=tuple(copied_paths),
            preflight_artifacts=(
                MigrationPreflightArtifact(
                    migration_id=MIGRATION_029_ID,
                    migration_filename=MIGRATION_029_FILENAME,
                    path=preflight,
                ),
            ),
        ),
        next(p for p in copied_paths if p.name == MIGRATION_029_FILENAME),
        preflight,
    )


def _manifest_with_noncanonical_identifier(
    tmp_path: Path,
    filename: str,
) -> tuple[tuple[Path, ...], Path]:
    sequence = int(filename.partition("_")[0])
    alias = tmp_path / filename
    alias.write_bytes(TEMP_DB_MIGRATION_PATHS[sequence - 1].read_bytes())
    paths = list(TEMP_DB_MIGRATION_PATHS.paths)
    paths[sequence - 1] = alias
    return tuple(paths), alias


def _apply_legacy_prefix(conn: sqlite3.Connection, count: int) -> None:
    for path in TEMP_DB_MIGRATION_PATHS[:count]:
        conn.executescript(path.read_text(encoding="utf-8"))
    conn.commit()


def test_empty_database_records_exact_manifest_in_deterministic_order() -> None:
    conn = _connection()
    try:
        apply_migration_paths(
            conn,
            TEMP_DB_MIGRATION_PATHS,
            clock=_clock,
            application_version="finance-v3",
        )
        rows = migration_ledger_rows(conn)
        assert len(rows) == len(TEMP_DB_MIGRATION_PATHS)
        assert [row["migration_id"] for row in rows] == [
            f"{n:03d}" for n in range(1, len(TEMP_DB_MIGRATION_PATHS) + 1)
        ]
        assert [row["migration_sequence"] for row in rows] == list(
            range(1, len(TEMP_DB_MIGRATION_PATHS) + 1)
        )
        assert [row["migration_filename"] for row in rows] == [
            path.name for path in TEMP_DB_MIGRATION_PATHS
        ]
        assert all(row["applied_at"] == FROZEN_TIME for row in rows)
        assert all(row["runner_version"] == MIGRATION_RUNNER_VERSION for row in rows)
        assert all(row["application_version"] == "finance-v3" for row in rows)
        assert all(row["adoption_mode"] == "applied" for row in rows)
        assert rows[-1]["schema_fingerprint"] == schema_fingerprint(conn)
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    finally:
        conn.close()


def test_checksum_uses_exact_file_bytes(tmp_path: Path) -> None:
    migration = _migration(
        tmp_path,
        "001_exact_bytes.sql",
        "CREATE TABLE exact_bytes (id INTEGER PRIMARY KEY);\n",
    )
    expected = hashlib.sha256(migration.read_bytes()).hexdigest()
    assert migration_file_checksum(migration) == expected
    migration.write_bytes(migration.read_bytes() + b"-- trailing byte evidence\n")
    assert migration_file_checksum(migration) != expected


def test_migrations_001_through_028_retain_exact_sql_byte_checksums() -> None:
    for migration in TEMP_DB_MIGRATION_PATHS[:28]:
        assert (
            migration_file_checksum(migration) == hashlib.sha256(migration.read_bytes()).hexdigest()
        )


@pytest.mark.parametrize(
    "filename",
    [
        "0001_create_core_schema.sql",
        "0002_receipt_split_schema.sql",
        "0029_authoritative_proof_evidence_integrity.sql",
        "00029_authoritative_proof_evidence_integrity.sql",
    ],
)
def test_noncanonical_migration_identifiers_are_rejected_in_isolation_and_full_manifest(
    tmp_path: Path,
    filename: str,
) -> None:
    manifest, alias = _manifest_with_noncanonical_identifier(tmp_path, filename)

    with pytest.raises(MigrationHistoryError, match="canonical numeric representation"):
        build_migration_manifest((alias,))
    with pytest.raises(MigrationHistoryError, match="canonical numeric representation"):
        build_migration_manifest(manifest)
    with pytest.raises(MigrationHistoryError, match="canonical numeric representation"):
        migration_file_checksum(alias)


@pytest.mark.parametrize(
    "filename",
    [
        "0029_authoritative_proof_evidence_integrity.sql",
        "00029_authoritative_proof_evidence_integrity.sql",
    ],
)
def test_padded_migration_029_manifest_fails_before_schema_or_ledger_creation(
    tmp_path: Path,
    filename: str,
) -> None:
    manifest, _alias = _manifest_with_noncanonical_identifier(tmp_path, filename)
    conn = _connection()
    try:
        with pytest.raises(MigrationHistoryError, match="canonical numeric representation"):
            apply_migration_paths(conn, manifest, clock=_clock)
        assert canonical_schema_payload(conn)["tables"] == []
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_migration_029_checksum_binds_sql_and_preflight_bytes(tmp_path: Path) -> None:
    sql = tmp_path / "029_authoritative_proof_evidence_integrity.sql"
    preflight = tmp_path / "migration_029_preflight.py"
    sql.write_bytes(_M029_PATH.read_bytes())
    preflight.write_bytes(MIGRATION_029_PREFLIGHT_ARTIFACT.read_bytes())
    initial = migration_file_checksum(sql, preflight_path=preflight)

    sql.write_bytes(sql.read_bytes() + b"\n-- checksum SQL drift\n")
    assert migration_file_checksum(sql, preflight_path=preflight) != initial
    sql.write_bytes(_M029_PATH.read_bytes())
    assert migration_file_checksum(sql, preflight_path=preflight) == initial

    preflight.write_bytes(preflight.read_bytes() + b"\n# checksum preflight drift\n")
    assert migration_file_checksum(sql, preflight_path=preflight) != initial


def test_migration_029_preflight_imports_only_documented_standard_library() -> None:
    tree = ast.parse(MIGRATION_029_PREFLIGHT_ARTIFACT.read_bytes())
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.partition(".")[0])

    assert "src" not in imported_roots
    assert imported_roots <= {
        "__future__",
        "collections",
        "datetime",
        "decimal",
        "hashlib",
        "hmac",
        "json",
        "re",
        "sqlite3",
        "typing",
        "unicodedata",
    }


def test_migration_029_preflight_executes_in_isolation_with_empty_authoritative_state() -> None:
    namespace: dict[str, object] = {"__name__": "isolated_migration_029_preflight"}
    source = MIGRATION_029_PREFLIGHT_ARTIFACT.read_bytes().decode("utf-8")
    exec(compile(source, str(MIGRATION_029_PREFLIGHT_ARTIFACT), "exec"), namespace)
    conn = _connection()
    try:
        conn.executescript(
            """
            CREATE TABLE statement_import_batches (
                id INTEGER PRIMARY KEY,
                source_file_hash TEXT,
                import_contract_version TEXT,
                import_command_hash TEXT,
                row_set_fingerprint TEXT
            );
            CREATE TABLE statement_transactions (
                batch_id INTEGER,
                row_fingerprint_version TEXT
            );
            CREATE TABLE reconciliation_match_results (
                id INTEGER PRIMARY KEY,
                public_id TEXT,
                decision_contract_version TEXT,
                matcher_version TEXT,
                compatibility_version TEXT,
                merchant_normalization_version TEXT,
                candidate_set_fingerprint TEXT,
                decision_hash TEXT,
                decision_material_json TEXT
            );
            """
        )
        verifier = namespace["verify_authoritative_state"]
        assert callable(verifier)
        verifier(conn)
    finally:
        conn.close()


def test_migration_029_pinned_canonical_json_vector() -> None:
    namespace: dict[str, object] = {"__name__": "isolated_migration_029_preflight"}
    source = MIGRATION_029_PREFLIGHT_ARTIFACT.read_bytes().decode("utf-8")
    exec(compile(source, str(MIGRATION_029_PREFLIGHT_ARTIFACT), "exec"), namespace)
    canonical_json_text = namespace["canonical_json_text"]
    assert callable(canonical_json_text)
    value = {
        "e\u0301": "Cafe\u0301",
        "amount": Decimal("100.5000"),
        "date": date(2026, 7, 15),
        "when": datetime(2026, 7, 15, 9, 30, tzinfo=timezone(timedelta(hours=8))),
        "items": (True, None, 2),
    }
    assert canonical_json_text(value) == (
        '{"contract_version":"finance-canonical-json-v1","value":'
        '{"amount":{"$decimal":"100.5"},"date":{"$date":"2026-07-15"},'
        '"items":[true,null,2],"when":{"$datetime":"2026-07-15T01:30:00.000000Z"},'
        '"é":"Café"}}'
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.000", "0"),
        ("-0.00", "0"),
        ("100.0", "100"),
        ("0.0100", "0.01"),
        ("-12.340", "-12.34"),
        ("1E+3", "1000"),
    ],
)
def test_migration_029_pinned_decimal_vectors(value: str, expected: str) -> None:
    namespace: dict[str, object] = {"__name__": "isolated_migration_029_preflight"}
    source = MIGRATION_029_PREFLIGHT_ARTIFACT.read_bytes().decode("utf-8")
    exec(compile(source, str(MIGRATION_029_PREFLIGHT_ARTIFACT), "exec"), namespace)
    canonical_decimal_str = namespace["canonical_decimal_str"]
    assert callable(canonical_decimal_str)
    assert canonical_decimal_str(Decimal(value)) == expected


def test_migration_history_detects_preflight_artifact_drift(
    tmp_path: Path,
) -> None:
    manifest, _sql, preflight = _copied_manifest(tmp_path)
    conn = _connection()
    try:
        apply_migration_paths(conn, manifest, clock=_clock)
        preflight.write_bytes(preflight.read_bytes() + b"\n# historical drift\n")
        with pytest.raises(MigrationChecksumError, match="029_authoritative"):
            verify_migration_history(conn, manifest)
    finally:
        conn.close()


def test_canonical_migration_029_manifest_slice_preserves_preflight_binding() -> None:
    specs = build_migration_manifest(TEMP_DB_MIGRATION_PATHS[28:29])
    assert len(specs) == 1
    assert specs[0].migration_id == MIGRATION_029_ID
    assert specs[0].filename == MIGRATION_029_FILENAME
    assert specs[0].preflight_path == MIGRATION_029_PREFLIGHT_ARTIFACT
    assert specs[0].preflight_bytes == MIGRATION_029_PREFLIGHT_ARTIFACT.read_bytes()


def test_copied_migration_029_binds_explicit_matching_preflight(tmp_path: Path) -> None:
    manifest, sql, preflight = _copied_manifest(tmp_path)
    _029_manifest = MigrationPathManifest(
        paths=tuple(p for p in manifest.paths if p.name == MIGRATION_029_FILENAME),
        preflight_artifacts=manifest.preflight_artifacts,
    )
    spec = build_migration_manifest(_029_manifest)[0]
    assert spec.path == sql
    assert spec.preflight_path == preflight
    assert spec.checksum_sha256 == migration_file_checksum(sql, preflight_path=preflight)


def test_copied_migration_029_without_preflight_fails_before_ledger(
    tmp_path: Path,
) -> None:
    manifest, sql, _preflight = _copied_manifest(tmp_path)
    unbound_copied_paths = tuple(manifest.paths)
    conn = _connection()
    try:
        with pytest.raises(MigrationHistoryError, match="explicitly bound preflight"):
            apply_migration_paths(conn, unbound_copied_paths, clock=_clock)
        assert not canonical_schema_payload(conn)["tables"]
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is None
        )
        with pytest.raises(MigrationHistoryError, match="explicitly bound preflight"):
            build_migration_manifest((sql,))
        with pytest.raises(MigrationHistoryError, match="explicitly bound preflight"):
            migration_file_checksum(sql)
        assert (
            build_migration_manifest(
                MigrationPathManifest(
                    paths=tuple(p for p in manifest.paths if p.name == MIGRATION_029_FILENAME),
                    preflight_artifacts=manifest.preflight_artifacts,
                )
            )[0].preflight_bytes
            is not None
        )
    finally:
        conn.close()


def test_migration_029_renamed_sql_is_rejected_even_with_preflight(tmp_path: Path) -> None:
    renamed = tmp_path / "029_renamed_authoritative_integrity.sql"
    renamed.write_bytes(_M029_PATH.read_bytes())
    manifest = MigrationPathManifest(
        paths=(renamed,),
        preflight_artifacts=(
            MigrationPreflightArtifact(
                migration_id=MIGRATION_029_ID,
                migration_filename=MIGRATION_029_FILENAME,
                path=MIGRATION_029_PREFLIGHT_ARTIFACT,
            ),
        ),
    )
    with pytest.raises(MigrationHistoryError, match="exact filename"):
        build_migration_manifest(manifest)


def test_migration_029_missing_bound_preflight_file_fails_closed(tmp_path: Path) -> None:
    sql = tmp_path / MIGRATION_029_FILENAME
    sql.write_bytes(_M029_PATH.read_bytes())
    missing_preflight = tmp_path / "missing-migration_029_preflight.py"
    manifest = MigrationPathManifest(
        paths=(sql,),
        preflight_artifacts=(
            MigrationPreflightArtifact(
                migration_id=MIGRATION_029_ID,
                migration_filename=MIGRATION_029_FILENAME,
                path=missing_preflight,
            ),
        ),
    )
    with pytest.raises(MigrationExecutionError, match="preflight artifact"):
        build_migration_manifest(manifest)


@pytest.mark.parametrize(
    ("migration_id", "migration_filename"),
    [
        ("030", MIGRATION_029_FILENAME),
        (MIGRATION_029_ID, "029_another_filename.sql"),
    ],
)
def test_migration_029_preflight_binding_identity_mismatch_is_rejected(
    tmp_path: Path,
    migration_id: str,
    migration_filename: str,
) -> None:
    sql = tmp_path / MIGRATION_029_FILENAME
    sql.write_bytes(_M029_PATH.read_bytes())
    manifest = MigrationPathManifest(
        paths=(sql,),
        preflight_artifacts=(
            MigrationPreflightArtifact(
                migration_id=migration_id,
                migration_filename=migration_filename,
                path=MIGRATION_029_PREFLIGHT_ARTIFACT,
            ),
        ),
    )
    with pytest.raises(MigrationHistoryError, match="id 029 and exact filename"):
        build_migration_manifest(manifest)


def test_sql_only_migration_executes_immutable_manifest_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration(
        tmp_path,
        "001_snapshot.sql",
        "CREATE TABLE snapshotted_sql (id INTEGER PRIMARY KEY);\n",
    )
    specs = build_migration_manifest((migration,))
    expected_checksum = hashlib.sha256(specs[0].sql_bytes).hexdigest()
    migration.write_text(
        "CREATE TABLE drifted_sql (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(migration_runtime, "build_migration_manifest", lambda _paths: specs)
    conn = _connection()
    try:
        apply_migration_paths(conn, (migration,), clock=_clock)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'snapshotted_sql'"
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'drifted_sql'"
        ).fetchone()
        assert migration_ledger_rows(conn)[0]["checksum_sha256"] == expected_checksum
    finally:
        conn.close()


def test_migration_029_executes_snapshotted_sql_and_preflight_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, sql, preflight = _copied_manifest(tmp_path)
    specs = build_migration_manifest(manifest)
    expected_checksum = next(
        sp.checksum_sha256 for sp in specs if sp.migration_id == MIGRATION_029_ID
    )
    sql.write_bytes(sql.read_bytes() + b"\nCREATE TABLE drifted_029_sql (id INTEGER);\n")
    preflight.write_text("raise RuntimeError('drifted preflight executed')\n", encoding="utf-8")
    assert migration_file_checksum(sql, preflight_path=preflight) != expected_checksum
    monkeypatch.setattr(migration_runtime, "build_migration_manifest", lambda _paths: specs)
    conn = _connection()
    try:
        apply_migration_paths(conn, manifest, clock=_clock)
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'drifted_029_sql'"
        ).fetchone()
        row = next(r for r in migration_ledger_rows(conn) if r["migration_id"] == MIGRATION_029_ID)
        assert row["migration_id"] == MIGRATION_029_ID
        assert row["checksum_sha256"] == expected_checksum
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_copied_manifest_fresh_apply_and_replay_are_deterministic(tmp_path: Path) -> None:
    manifest, _sql, _preflight = _copied_manifest(tmp_path)
    conn = _connection()
    try:
        apply_migration_paths(conn, manifest, clock=_clock)
        first_rows = migration_ledger_rows(conn)
        apply_migration_paths(conn, manifest, clock=lambda: "later")
        assert migration_ledger_rows(conn) == first_rows
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_repeated_runner_is_idempotent_and_preserves_ledger_rows() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        first = migration_ledger_rows(conn)
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=lambda: "later")
        assert migration_ledger_rows(conn) == first
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_modified_applied_migration_fails_closed(tmp_path: Path) -> None:
    migration = _migration(
        tmp_path,
        "001_immutable.sql",
        "CREATE TABLE immutable_history (id INTEGER PRIMARY KEY);",
    )
    conn = _connection()
    try:
        apply_migration_paths(conn, (migration,), clock=_clock)
        migration.write_text(
            "CREATE TABLE immutable_history (id INTEGER PRIMARY KEY, changed TEXT);",
            encoding="utf-8",
        )
        with pytest.raises(MigrationChecksumError, match="checksum changed"):
            apply_migration_paths(conn, (migration,), clock=_clock)
        assert conn.execute("PRAGMA table_info(immutable_history)").fetchall()[0][1] == "id"
    finally:
        conn.close()


def test_tampered_stored_checksum_fails_verification() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        conn.execute(
            "UPDATE schema_migrations SET checksum_sha256 = ? WHERE migration_id = '001'",
            ("f" * 64,),
        )
        conn.commit()
        with pytest.raises(MigrationChecksumError, match="001_create_core_schema"):
            verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    finally:
        conn.close()


def test_recorded_padded_identifier_alias_is_rejected_by_history_verification() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        conn.execute(
            "UPDATE schema_migrations SET migration_id = ?, migration_filename = ? "
            "WHERE migration_id = ?",
            (
                "0029",
                "0029_authoritative_proof_evidence_integrity.sql",
                MIGRATION_029_ID,
            ),
        )
        conn.commit()
        with pytest.raises(MigrationHistoryError, match="order does not match"):
            verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    finally:
        conn.close()


def test_duplicate_migration_identifier_is_rejected(tmp_path: Path) -> None:
    first = _migration(tmp_path, "001_alpha.sql", "CREATE TABLE alpha (id INTEGER);")
    second = _migration(tmp_path, "001_beta.sql", "CREATE TABLE beta (id INTEGER);")
    with pytest.raises(MigrationHistoryError, match="Duplicate migration identifier"):
        build_migration_manifest((first, second))


def test_reordered_manifest_is_rejected(tmp_path: Path) -> None:
    first = _migration(tmp_path, "001_alpha.sql", "CREATE TABLE alpha (id INTEGER);")
    second = _migration(tmp_path, "002_beta.sql", "CREATE TABLE beta (id INTEGER);")
    with pytest.raises(MigrationHistoryError, match="strictly increasing"):
        build_migration_manifest((second, first))


def test_missing_previously_applied_migration_is_rejected(tmp_path: Path) -> None:
    first = _migration(tmp_path, "001_alpha.sql", "CREATE TABLE alpha (id INTEGER);")
    second = _migration(tmp_path, "002_beta.sql", "CREATE TABLE beta (id INTEGER);")
    conn = _connection()
    try:
        apply_migration_paths(conn, (first, second), clock=_clock)
        apply_migration_paths(conn, (first,), clock=_clock)
        with pytest.raises(MigrationHistoryError, match="incomplete"):
            verify_migration_history(conn, (first,))
    finally:
        conn.close()


def test_recorded_order_tampering_is_rejected(tmp_path: Path) -> None:
    first = _migration(tmp_path, "001_alpha.sql", "CREATE TABLE alpha (id INTEGER);")
    second = _migration(tmp_path, "002_beta.sql", "CREATE TABLE beta (id INTEGER);")
    conn = _connection()
    try:
        apply_migration_paths(conn, (first, second), clock=_clock)
        conn.execute(
            "UPDATE schema_migrations SET migration_sequence = 99 WHERE migration_id = '001'"
        )
        conn.execute(
            "UPDATE schema_migrations SET migration_sequence = 1 WHERE migration_id = '002'"
        )
        conn.execute(
            "UPDATE schema_migrations SET migration_sequence = 2 WHERE migration_id = '001'"
        )
        conn.commit()
        with pytest.raises(MigrationHistoryError, match="order"):
            verify_migration_history(conn, (first, second))
    finally:
        conn.close()


def test_syntax_failure_is_not_recorded_and_does_not_create_partial_schema(
    tmp_path: Path,
) -> None:
    first = _migration(tmp_path, "001_alpha.sql", "CREATE TABLE alpha (id INTEGER);")
    failing = _migration(tmp_path, "002_broken.sql", "CREATE TABL broken (id INTEGER);")
    conn = _connection()
    try:
        with pytest.raises(MigrationExecutionError, match="002_broken"):
            apply_migration_paths(conn, (first, failing), clock=_clock)
        rows = migration_ledger_rows(conn)
        assert [row["migration_id"] for row in rows] == ["001"]
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'broken'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_runtime_failure_rolls_back_all_statements_and_ledger_row(tmp_path: Path) -> None:
    first = _migration(tmp_path, "001_alpha.sql", "CREATE TABLE alpha (id INTEGER);")
    failing = _migration(
        tmp_path,
        "002_runtime_failure.sql",
        "CREATE TABLE partial_child (id INTEGER);\n"
        "INSERT INTO table_that_does_not_exist VALUES (1);\n",
    )
    conn = _connection()
    try:
        with pytest.raises(MigrationExecutionError, match="002_runtime_failure"):
            apply_migration_paths(conn, (first, failing), clock=_clock)
        assert [row["migration_id"] for row in migration_ledger_rows(conn)] == ["001"]
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'partial_child'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


@pytest.mark.parametrize("historical_level", range(1, 23))
def test_each_historical_prefix_is_verified_before_ledger_adoption(
    historical_level: int,
) -> None:
    conn = _connection()
    try:
        _apply_legacy_prefix(conn, historical_level)
        legacy_fingerprint = schema_fingerprint(conn)
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        rows = migration_ledger_rows(conn)
        assert len(rows) == len(TEMP_DB_MIGRATION_PATHS)
        assert all(row["adoption_mode"] == "verified_legacy" for row in rows[:historical_level])
        assert rows[historical_level - 1]["schema_fingerprint"] == legacy_fingerprint
        assert all(row["adoption_mode"] == "applied" for row in rows[historical_level:])
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    finally:
        conn.close()


def test_unknown_preledger_schema_fails_adoption_without_creating_ledger() -> None:
    conn = _connection()
    try:
        conn.execute("CREATE TABLE unknown_legacy_shape (id INTEGER PRIMARY KEY)")
        conn.commit()
        with pytest.raises(MigrationSchemaDriftError, match="supported migration prefix"):
            apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'unknown_legacy_shape'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_preledger_database_cannot_be_silently_adopted_with_fragment_manifest(
    tmp_path: Path,
) -> None:
    fragment = _migration(tmp_path, "007_fragment.sql", "CREATE TABLE fragment (id INTEGER);")
    conn = _connection()
    try:
        conn.execute("CREATE TABLE preexisting (id INTEGER PRIMARY KEY)")
        conn.commit()
        with pytest.raises(MigrationHistoryError, match="contiguous manifest"):
            apply_migration_paths(conn, (fragment,), clock=_clock)
    finally:
        conn.close()


def test_schema_fingerprint_is_deterministic_and_data_independent() -> None:
    first = _connection()
    second = _connection()
    try:
        apply_migration_paths(first, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        apply_migration_paths(second, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        expected = schema_fingerprint(first)
        assert schema_fingerprint(second) == expected
        second.execute(
            "INSERT INTO participants (public_id, display_name) VALUES ('person-data', 'Data')"
        )
        second.commit()
        assert schema_fingerprint(second) == expected
    finally:
        first.close()
        second.close()


def test_schema_fingerprint_covers_columns_indexes_foreign_keys_and_partial_predicates() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        payload = canonical_schema_payload(conn)
        tables = {table["name"]: table for table in payload["tables"]}  # type: ignore[index]
        audit = tables["receipt_finalization_audit"]
        assert any(column["name"] == "content_fingerprint" for column in audit["columns"])
        assert any(index["unique"] == 1 for index in audit["indexes"])
        authorizations = tables["receipt_finalization_authorizations"]
        assert authorizations["foreign_keys"]
        assert any(
            "WHERE status = 'finalized'" in str(obj["sql"])
            for obj in payload["objects"]  # type: ignore[union-attr]
            if obj["name"] == "uq_receipt_finalization_audit_one_per_group"
        )
    finally:
        conn.close()


def test_schema_drift_after_success_fails_closed() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        conn.execute("CREATE TABLE unauthorized_schema_drift (id INTEGER PRIMARY KEY)")
        conn.commit()
        with pytest.raises(MigrationSchemaDriftError, match="fingerprint"):
            verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    finally:
        conn.close()


def test_process_restart_after_success_replays_without_new_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "restart-success.sqlite"
    first = _connection(db_path)
    try:
        apply_migration_paths(first, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        expected_rows = migration_ledger_rows(first)
    finally:
        first.close()

    restarted = _connection(db_path)
    try:
        apply_migration_paths(restarted, TEMP_DB_MIGRATION_PATHS, clock=lambda: "later")
        assert migration_ledger_rows(restarted) == expected_rows
        assert restarted.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restarted.close()


def test_process_restart_after_failure_can_apply_corrected_unrecorded_migration(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "restart-failure.sqlite"
    first_path = _migration(tmp_path, "001_alpha.sql", "CREATE TABLE alpha (id INTEGER);")
    second_path = _migration(
        tmp_path,
        "002_beta.sql",
        "CREATE TABLE beta (id INTEGER); INSERT INTO missing_table VALUES (1);",
    )
    first = _connection(db_path)
    try:
        with pytest.raises(MigrationExecutionError):
            apply_migration_paths(first, (first_path, second_path), clock=_clock)
        assert [row["migration_id"] for row in migration_ledger_rows(first)] == ["001"]
    finally:
        first.close()

    second_path.write_text("CREATE TABLE beta (id INTEGER);", encoding="utf-8")
    restarted = _connection(db_path)
    try:
        apply_migration_paths(restarted, (first_path, second_path), clock=_clock)
        assert [row["migration_id"] for row in migration_ledger_rows(restarted)] == [
            "001",
            "002",
        ]
        assert (
            restarted.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'beta'"
            ).fetchone()
            is not None
        )
    finally:
        restarted.close()


def test_two_clean_replays_produce_identical_ledger_and_schema() -> None:
    first = _connection()
    second = _connection()
    try:
        apply_migration_paths(first, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        apply_migration_paths(second, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        assert migration_ledger_rows(first) == migration_ledger_rows(second)
        assert schema_fingerprint(first) == schema_fingerprint(second)
    finally:
        first.close()
        second.close()


def test_incomplete_authoritative_ledger_fails_complete_verification() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:-1], clock=_clock)
        with pytest.raises(MigrationHistoryError, match="incomplete"):
            verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS, require_complete=False)
    finally:
        conn.close()


def test_migration_ledger_tests_never_target_live_database(tmp_path: Path) -> None:
    assert (tmp_path / "migration-ledger.sqlite").resolve() != LIVE_DB_PATH.resolve()


# ---------------------------------------------------------------------------
# Migration 030 compatibility regressions
# ---------------------------------------------------------------------------


def test_authoritative_manifest_contains_migration_001_through_049_in_order() -> None:
    """The current authoritative manifest has ordered migration IDs 001–049
    with no gaps, duplicates or reordered entries."""
    assert len(TEMP_DB_MIGRATION_PATHS) == 49
    assert tuple(TEMP_DB_MIGRATION_PATHS.paths) == tuple(
        path
        for mid, path in sorted(
            (
                int(p.name.partition("_")[0]),
                p,
            )
            for p in TEMP_DB_MIGRATION_PATHS
        )
    )
    # Verify every expected migration ID is present
    identifiers = [f"{n:03d}" for n in range(1, len(TEMP_DB_MIGRATION_PATHS) + 1)]
    assert identifiers == [p.name.partition("_")[0] for p in TEMP_DB_MIGRATION_PATHS]


def test_migration_030_is_sql_only_with_no_preflight_binding() -> None:
    """Migration 030 must not inherit migration 029's preflight artifact."""
    _030_path = TEMP_DB_MIGRATION_PATHS[29]
    assert _030_path.name == "030_parser_proposal_completion.sql"
    specs = build_migration_manifest(TEMP_DB_MIGRATION_PATHS)
    spec_029 = next(sp for sp in specs if sp.migration_id == MIGRATION_029_ID)
    spec_030 = next(sp for sp in specs if sp.migration_id == "030")
    assert spec_029.preflight_path is not None
    assert spec_029.preflight_bytes is not None
    assert spec_030.preflight_path is None
    assert spec_030.preflight_bytes is None


def test_fresh_migration_001_through_049_replay_is_deterministic() -> None:
    """A fresh temporary database must apply 001–049, record all ledger rows
    in order, pass history verification, and remain unchanged on replay."""
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        rows = migration_ledger_rows(conn)
        assert len(rows) == 49
        assert rows[-1]["migration_id"] == "049"
        assert all(row["adoption_mode"] == "applied" for row in rows)
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        # Replay must produce the same immutable ledger
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS, clock=lambda: "later")
        assert migration_ledger_rows(conn) == rows
    finally:
        conn.close()


def test_incremental_upgrade_029_to_030_preserves_ledger_evidence() -> None:
    """Applying 001–029 first, then upgrading with 030 only, must record 030
    once and preserve all existing migration-029 ledger and checksum evidence.
    Migration 031 must not be applied in this test."""
    conn = _connection()
    try:
        manifest_through_029 = TEMP_DB_MIGRATION_PATHS[:29]
        apply_migration_paths(conn, manifest_through_029, clock=_clock)

        rows_029 = migration_ledger_rows(conn)
        assert len(rows_029) == 29
        assert rows_029[-1]["migration_id"] == MIGRATION_029_ID

        # Apply migration 030 only, not the full manifest
        manifest_through_030 = TEMP_DB_MIGRATION_PATHS[:30]
        apply_migration_paths(conn, manifest_through_030, clock=_clock)

        rows_030 = migration_ledger_rows(conn)
        assert len(rows_030) == 30
        # First 29 rows must be identical
        for actual, expected in zip(rows_030[:29], rows_029):
            assert actual == expected
        assert rows_030[-1]["migration_id"] == "030"
        assert rows_030[-1]["adoption_mode"] == "applied"

        verify_migration_history(conn, manifest_through_030)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_complete_replay_001_through_034_on_restart_returns_original_rows(
    tmp_path: Path,
) -> None:
    """Process restart after success replays through 034 without new rows."""
    db_path = tmp_path / "restart-030.sqlite"
    first = _connection(db_path)
    try:
        apply_migration_paths(first, TEMP_DB_MIGRATION_PATHS, clock=_clock)
        expected = migration_ledger_rows(first)
    finally:
        first.close()

    restarted = _connection(db_path)
    try:
        apply_migration_paths(restarted, TEMP_DB_MIGRATION_PATHS, clock=lambda: "later")
        assert migration_ledger_rows(restarted) == expected
        assert restarted.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restarted.close()
