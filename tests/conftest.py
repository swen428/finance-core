import os
import sqlite3
from pathlib import Path
from typing import Iterator, Sequence, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FINANCE_RUNTIME_ROOT", str(REPO_ROOT))

from migrated_staging_snapshot_v1 import (  # noqa: E402
    MigratedStagingTemplate,
    clone_migrated_staging_template,
    create_migrated_staging_template,
)

from finance_core.reconciliation.migrations import (  # noqa: E402
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
)
from finance_core.staging_guard import create_staging_database  # noqa: E402

LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"
MIGRATION_PATHS = TEMP_DB_MIGRATION_PATHS
TC001_SEED_PATH = REPO_ROOT / "database" / "seed" / "002_test_case_001_receipt_split.sql"


@pytest.fixture()
def temp_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "finance_pytest.sqlite"

    assert db_path != LIVE_DB_PATH
    assert db_path.is_relative_to(tmp_path)

    return db_path


@pytest.fixture()
def temp_db_connection(temp_db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect_temp_db(temp_db_path)

    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def migrated_temp_db_path(temp_db_path: Path, request: pytest.FixtureRequest) -> Path:
    marker = request.node.get_closest_marker("migrated_staging_snapshot")
    if marker is not None:
        template = cast(
            MigratedStagingTemplate,
            request.getfixturevalue("migrated_staging_snapshot_template"),
        )
        conn = clone_migrated_staging_template(template, temp_db_path)
    else:
        conn = connect_temp_db(temp_db_path)
    try:
        if marker is None:
            apply_migrations(conn)
            conn.commit()
    finally:
        conn.close()

    return temp_db_path


@pytest.fixture(scope="session")
def migrated_staging_snapshot_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MigratedStagingTemplate]:
    template = create_migrated_staging_template(
        tmp_path_factory.mktemp("migrated-staging-template") / "finance.sqlite"
    )
    try:
        yield template
    finally:
        template.path.chmod(0o600)


@pytest.fixture()
def migrated_temp_db_connection(
    migrated_temp_db_path: Path,
) -> Iterator[sqlite3.Connection]:
    conn = connect_temp_db(migrated_temp_db_path)

    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def legacy_temp_db_path(temp_db_path: Path) -> Path:
    """Keep pre-D3 one-step Telegram adapter contracts on their original schema."""
    conn = connect_temp_db(temp_db_path)
    try:
        assert MIGRATION_PATHS[-1].name == "055_d3_interaction_routes.sql"
        apply_migrations(conn, MIGRATION_PATHS[:-1])
        conn.commit()
    finally:
        conn.close()
    return temp_db_path


@pytest.fixture()
def legacy_temp_db_connection(legacy_temp_db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect_temp_db(legacy_temp_db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def tc001_db(migrated_temp_db_connection: sqlite3.Connection) -> sqlite3.Connection:
    _seed_owner_participant(migrated_temp_db_connection)
    apply_sql(migrated_temp_db_connection, TC001_SEED_PATH)
    migrated_temp_db_connection.commit()
    return migrated_temp_db_connection


def connect_temp_db(db_path: Path) -> sqlite3.Connection:
    """Create or reconnect to a staging-authorised temporary database."""
    db_path_resolved = db_path.resolve()
    if db_path_resolved.exists():
        # Reconnect — the DB was already created and authorised by a prior
        # fixture step (e.g. migrated_temp_db_path). Connect normally and
        # let the existing _staging_authorization record prove identity.
        conn = sqlite3.connect(str(db_path_resolved))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    return create_staging_database(db_path_resolved, migration_paths=None)


def apply_migrations(
    conn: sqlite3.Connection,
    migration_paths: Sequence[Path] = MIGRATION_PATHS,
) -> None:
    apply_migration_paths(conn, migration_paths)


def apply_sql(conn: sqlite3.Connection, path: Path) -> None:
    conn.executescript(path.read_text(encoding="utf-8"))


def _seed_owner_participant(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO participants (public_id, display_name, aliases, is_self, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("person_owner", "Owner", '["Owner","me","我"]', 1, "Primary participant for TC001"),
    )
