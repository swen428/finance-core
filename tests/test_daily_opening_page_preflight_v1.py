import sqlite3
from contextlib import closing
from importlib.resources import files

import pytest

from finance_core.lifecycle_v2.contracts import LifecycleError, canonical
from finance_core.lifecycle_v2.daily_v1 import _opening_proposal_pages_v1 as pages
from finance_core.lifecycle_v2.reader import _has_daily_values, audit_connection

_HEADER = canonical({"format": pages.PAGED_OBSERVATION})
_VALUES = canonical({"format": pages.PAGED_PENDING})


@pytest.fixture
def immutable_copy(tmp_path):
    path = tmp_path / "opening-cut.sqlite"

    def create(rows, events=()):
        with closing(sqlite3.connect(path)) as db:
            migration = files("finance_core.resources.migrations") / "061_lifecycle_v2.sql"
            db.executescript(migration.read_text(encoding="utf-8"))
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.executemany(
                "INSERT INTO l2_proposals VALUES(?,?,1,?,0,NULL,?,?,?,0)",
                (
                    (f"proposal-{i}", f"source-{i}", f"observation-{i}", values, header, str(i))
                    for i, (header, values) in enumerate(rows)
                ),
            )
            db.executemany(
                "INSERT INTO l2_events VALUES(?,?,'propose',?,?,NULL,?)",
                (
                    (i, f"operation-{i}", f"proposal-{owner}", payload, str(i))
                    for i, (owner, payload) in enumerate(events, 1)
                ),
            )
            db.commit()
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        return closing(connection)

    return create


@pytest.mark.parametrize("events", [(), ((0, "[]"), (0, "[]"))], ids=["absent", "duplicate"])
def test_paged_copy_requires_exactly_one_propose_event(immutable_copy, events):
    with immutable_copy([(_HEADER, _VALUES)], events) as conn:
        with pytest.raises(LifecycleError, match="exact owning propose event"):
            audit_connection(conn)


def test_paged_copy_counts_proposal_bytes_when_owning_event_is_absent(immutable_copy, monkeypatch):
    monkeypatch.setattr(pages, "_CUT_BYTES", 200)
    with immutable_copy([(_HEADER, _VALUES)] * 3) as conn:
        with pytest.raises(LifecycleError, match="storage budget"):
            audit_connection(conn)


@pytest.mark.parametrize("row", [(_HEADER, "{}"), ("{}", _VALUES)])
def test_paged_copy_requires_both_format_markers(immutable_copy, row):
    with immutable_copy([row], [(0, "[]")]) as conn:
        with pytest.raises(LifecycleError, match="page format markers differ"):
            audit_connection(conn)


def test_paged_values_marker_retains_the_event_budget(immutable_copy, monkeypatch):
    monkeypatch.setattr(pages, "_CELL_BYTES", 128)
    oversized_event = canonical(["é" * 64])
    with immutable_copy([("{}", _VALUES)], [(0, oversized_event)]) as conn:
        with pytest.raises(LifecycleError, match="storage budget"):
            audit_connection(conn)


@pytest.mark.parametrize(
    ("row", "payload"),
    [((_HEADER[:-1], _VALUES), "[]"), ((_HEADER, _VALUES[:-1]), "[]"), ((_HEADER, _VALUES), "[")],
    ids=["header", "pending", "event"],
)
def test_paged_copy_reports_truncated_json_as_lifecycle_error(immutable_copy, row, payload):
    with immutable_copy([row], [(0, payload)]) as conn:
        with pytest.raises(LifecycleError, match="Malformed opening proposal page material"):
            audit_connection(conn)


def test_daily_format_classification_does_not_fetch_proposal_material(immutable_copy):
    values = canonical({"format": pages.PAGED_PENDING, "pending": "synthetic material"})

    def metadata_only(_cursor, row):
        assert values not in row
        return row

    with immutable_copy([(_HEADER, values)], [(0, "[]")]) as conn:
        conn.row_factory = metadata_only
        assert _has_daily_values(conn) is True


def test_preflight_keeps_canonical_paged_and_legacy_scalar_sizes_separate(
    immutable_copy, monkeypatch
):
    monkeypatch.setattr(pages, "_CELL_BYTES", 128)
    legacy_values = canonical({"text": "historical " * 128})
    with immutable_copy([(_HEADER, _VALUES), ("{}", legacy_values)], [(0, "[]")]) as conn:
        pages.preflight(conn)
