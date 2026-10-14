"""Process-owned operations: locks and issued connection witnesses span close."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Self

from finance_core.profile_gate import GateLease, writer_gate
from finance_core.profile_paths import _MANAGED_SQLITE_LIFETIME_LOCK
from finance_core.sqlite_connection import configure_sqlite_connection
from finance_core.staging_guard import require_staging_database

from .authority import (
    _CONFIG_OWNERS,
    CONTROL_LOCK_FILENAME,
    OWNER_LOCK_FILENAME,
    InstanceAdmissionError,
    ProductionProfile,
    TrustedInstanceConfig,
    _close_sqlite_handle,
    _Lock,
    _profile,
    _record,
    _state,
)

_OWNER_TOKEN = object()
_OWNERS: dict[int, "ProductionOwner"] = {}
_WITNESSES: dict[sqlite3.Connection, "_Witness"] = {}
# Unknown-close handles deliberately remain reachable until process exit.
_UNCERTAIN: list["_Witness"] = []


class ProductionCloseUncertain(InstanceAdmissionError):
    """Connection close was not proved; the owner and operation remain held."""

    def __init__(self, witness: "_Witness") -> None:
        super().__init__("SQLite close uncertain; connection and all leases retained")
        self.connection = witness.connection
        self.owner = witness.owner


class ProductionOwner:
    """Unique lifetime EX lease for this control root, across profile successors."""

    def __init__(self, config: TrustedInstanceConfig, lease: _Lock, *, _token=None):
        if _token is not _OWNER_TOKEN:
            raise InstanceAdmissionError("Production owner must be acquired")
        self.config, self._lease, self._pid = config, lease, os.getpid()
        self._active: _Witness | None = None
        self._uncertain = False
        _OWNERS[id(self)] = self
        _CONFIG_OWNERS[id(config)] = _CONFIG_OWNERS.get(id(config), 0) + 1

    def assert_valid(self) -> None:
        if self._pid != os.getpid() or self._uncertain or _OWNERS.get(id(self)) is not self:
            raise InstanceAdmissionError("Forked or uncertain production owner")
        self._lease.assert_valid()

    def close(self) -> None:
        self.assert_valid()
        if self._active is not None:
            raise InstanceAdmissionError("Owner cannot release before actual operation close")
        self._lease.close()
        count = _CONFIG_OWNERS[id(self.config)] - 1
        if count:
            _CONFIG_OWNERS[id(self.config)] = count
        else:
            _CONFIG_OWNERS.pop(id(self.config), None)
        _OWNERS.pop(id(self), None)

    def __enter__(self) -> Self:
        self.assert_valid()
        return self

    def __exit__(self, *args: object) -> None:
        if self._uncertain and args and args[0] is not None:
            return
        self.close()


def acquire_production_owner(
    config: TrustedInstanceConfig, *, timeout_seconds: float = 5.0
) -> ProductionOwner:
    lease = _Lock(config, OWNER_LOCK_FILENAME)
    try:
        lease.acquire(fcntl.LOCK_EX, timeout_seconds)
        control = _Lock(config, CONTROL_LOCK_FILENAME)
        try:
            control.acquire(fcntl.LOCK_SH, timeout_seconds)
            record = _record(config, _state(config))
            if record["state"] not in {"ACTIVE", "FENCED"}:
                raise InstanceAdmissionError("Owner requires admitted or fenced instance")
            with _profile(config, expected=record["db_identity"]):
                pass
        finally:
            control.close()
        return ProductionOwner(config, lease, _token=_OWNER_TOKEN)
    except BaseException:
        lease.close()
        raise


@dataclass
class _Witness:
    owner: ProductionOwner
    connection: sqlite3.Connection
    control: _Lock
    profile: ProductionProfile
    gate: GateLease
    authority_version: int
    read_only: bool
    pid: int
    active: bool = True
    closing: bool = False
    closed: bool = False

    def preflight(
        self, *, write: bool = False, instance_id: str | None = None, full: bool = True
    ) -> None:
        if (
            self.pid != os.getpid()
            or not self.active
            or self.closed
            or self.closing
            or self.owner._active is not self
            or _WITNESSES.get(self.connection) is not self
        ):
            raise InstanceAdmissionError("Closed, forked, stale or unissued production session")
        if not full:
            if self.owner._pid != os.getpid() or self.owner._uncertain or self.owner._lease.fd < 0:
                raise InstanceAdmissionError("Production owner is no longer held")
            return
        self.owner.assert_valid()
        self.control.assert_valid()
        self.profile.revalidate()
        self.gate.assert_valid()
        state = _state(self.owner.config)
        record = _record(self.owner.config, state)
        if state["version"] != self.authority_version or record["maintenance"] is not None:
            raise InstanceAdmissionError("Production authority version/maintenance changed")
        if write and (
            self.read_only
            or record["state"] != "ACTIVE"
            or state["current"] != self.owner.config.binding_sha256
        ):
            raise InstanceAdmissionError(
                "Production write requires current ACTIVE writable session"
            )
        if not write and record["state"] not in {"ACTIVE", "FENCED"}:
            raise InstanceAdmissionError("Production read requires admitted original authority")
        if instance_id is not None and instance_id != self.owner.config.instance_id:
            raise InstanceAdmissionError("Business binding belongs to another production instance")


def _preflight(conn: sqlite3.Connection, *, write: bool = False) -> _Witness:
    witness = _WITNESSES.get(conn)
    if witness is None:
        raise InstanceAdmissionError("Production connection was not issued by operation owner")
    witness.preflight(write=write, full=False)
    return witness


class _ProductionCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=(), /):
        _preflight(self.connection)
        return super().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters, /):
        _preflight(self.connection)
        return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script, /):
        _preflight(self.connection)
        return super().executescript(sql_script)

    def fetchone(self):
        _preflight(self.connection)
        return super().fetchone()

    def fetchmany(self, size=None):
        _preflight(self.connection)
        return super().fetchmany() if size is None else super().fetchmany(size)

    def fetchall(self):
        _preflight(self.connection)
        return super().fetchall()

    def __iter__(self):
        _preflight(self.connection)
        return self

    def __next__(self):
        _preflight(self.connection)
        return super().__next__()


class _ProductionConnection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters, /):
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script, /):
        return self.cursor().executescript(sql_script)

    def cursor(self, factory=None):
        _preflight(self)
        if factory is not None and factory is not _ProductionCursor:
            raise InstanceAdmissionError("Production cursor factory cannot replace session witness")
        return super().cursor(_ProductionCursor)

    def commit(self) -> None:
        witness = _preflight(self)
        _require_session(self, write=not witness.read_only, instance_id=None)
        super().commit()

    def rollback(self) -> None:
        _preflight(self)
        super().rollback()

    def close(self) -> None:
        witness = _preflight(self)
        super().close()
        witness.closed = True

    def __enter__(self) -> Self:
        _preflight(self)
        return super().__enter__()

    def __exit__(self, *args: Any) -> Any:
        witness = _preflight(self)
        _require_session(self, write=not witness.read_only, instance_id=None)
        return super().__exit__(*args)


def _require_session(conn: sqlite3.Connection, *, write: bool, instance_id: str | None) -> None:
    witness = _WITNESSES.get(conn)
    if witness is None:
        # Known production connection subclasses must never take staging fallback.
        if isinstance(conn, _ProductionConnection):
            raise InstanceAdmissionError("Expired/unissued production session")
        require_staging_database(conn)
        return
    witness.preflight(write=write, instance_id=instance_id)
    try:
        databases = sqlite3.Connection.execute(conn, "PRAGMA database_list").fetchall()
        if sqlite3.Connection.execute(
            conn, "SELECT 1 FROM sqlite_temp_master WHERE name NOT GLOB 'sqlite_*'"
        ).fetchone():
            raise InstanceAdmissionError("Production session cannot carry TEMP schema shadows")
        main = [row for row in databases if row[1] == "main"]
        if (
            len(main) != 1
            or any(row[1] not in {"main", "temp"} for row in databases)
            or main[0][2] != str(witness.profile.live_database)
        ):
            raise InstanceAdmissionError("Production session has unexpected database attachment")
    except sqlite3.Error as exc:
        raise InstanceAdmissionError("Production SQLite handle is no longer live") from exc


def require_finance_write_connection(
    conn: sqlite3.Connection, *, instance_id: str | None = None
) -> None:
    """Read-only admission check; original staging fallback preserves transactions."""
    _require_session(conn, write=True, instance_id=instance_id)


def require_finance_read_connection(
    conn: sqlite3.Connection, *, instance_id: str | None = None
) -> None:
    """Accept issued admitted/FENCED read sessions or original staging."""
    _require_session(conn, write=False, instance_id=instance_id)


@contextmanager
def _operation(
    owner: ProductionOwner, *, read_only: bool, timeout_seconds: float
) -> Iterator[sqlite3.Connection]:
    from .schema_contract import SchemaContractError, validate_application_database

    owner.assert_valid()
    if owner._active is not None:
        raise InstanceAdmissionError("Nested production operations are forbidden")
    control = _Lock(owner.config, CONTROL_LOCK_FILENAME)
    profile: ProductionProfile | None = None
    gate: GateLease | None = None
    conn: sqlite3.Connection | None = None
    witness: _Witness | None = None
    lifetime = False
    sqlite_began = False
    close_uncertain = False
    try:
        control.acquire(fcntl.LOCK_SH, timeout_seconds)
        state = _state(owner.config)
        record = _record(owner.config, state)
        if (
            record["maintenance"] is not None
            or record["state"] not in {"ACTIVE", "FENCED"}
            or (
                not read_only
                and (record["state"] != "ACTIVE" or state["current"] != owner.config.binding_sha256)
            )
        ):
            raise InstanceAdmissionError("Current authority does not admit this operation")
        _MANAGED_SQLITE_LIFETIME_LOCK.__enter__()
        lifetime = True
        profile = _profile(owner.config, expected=record["db_identity"])
        gate = writer_gate(profile, timeout_seconds=timeout_seconds)
        _MANAGED_SQLITE_LIFETIME_LOCK.begin_sqlite()
        sqlite_began = True
        uri = profile.live_database.as_uri() + ("?mode=ro" if read_only else "?mode=rw")
        conn = sqlite3.connect(uri, uri=True, factory=_ProductionConnection)
        witness = _Witness(
            owner, conn, control, profile, gate, state["version"], read_only, os.getpid()
        )
        _WITNESSES[conn] = witness
        owner._active = witness
        configure_sqlite_connection(conn)
        try:
            validate_application_database(
                conn,
                owner.config._binding["migration_identity"],
                owner.config.application_schema,
                record["ledger_sha256"],
            )
        except SchemaContractError as exc:
            raise InstanceAdmissionError(str(exc)) from exc
        require_finance_read_connection(conn)
        if not read_only:
            require_finance_write_connection(conn)
        yield conn
    finally:
        if conn is not None:
            try:
                if witness is not None:
                    witness.closing = True
                # No outer transaction is committed on behalf of business services.
                if not (witness is not None and witness.closed) and conn.in_transaction:
                    sqlite3.Connection.rollback(conn)
                _close_sqlite_handle(conn)
            except BaseException as exc:
                close_uncertain = True
                owner._uncertain = True
                _MANAGED_SQLITE_LIFETIME_LOCK.mark_uncertain()
                if witness is not None:
                    _UNCERTAIN.append(witness)
                    raise ProductionCloseUncertain(witness) from exc
                raise InstanceAdmissionError(
                    "SQLite close uncertain before session publication"
                ) from exc
            finally:
                if witness is not None and not close_uncertain:
                    witness.active = False
                    witness.closed = True
                    _WITNESSES.pop(conn, None)
                    owner._active = None
        if not close_uncertain:
            if sqlite_began:
                _MANAGED_SQLITE_LIFETIME_LOCK.end_sqlite()
            if gate is not None:
                gate.close()
            if profile is not None:
                profile.close()
            control.close()
        if lifetime:
            _MANAGED_SQLITE_LIFETIME_LOCK.__exit__()


def production_write_operation(owner: ProductionOwner, *, timeout_seconds: float = 5.0):
    """Hold owner/control/profile witnesses until own connection actually closes."""
    return _operation(owner, read_only=False, timeout_seconds=timeout_seconds)


def production_read_operation(owner: ProductionOwner, *, timeout_seconds: float = 5.0):
    """Open true mode=ro, including legitimate FENCED forensic admission."""
    return _operation(owner, read_only=True, timeout_seconds=timeout_seconds)
