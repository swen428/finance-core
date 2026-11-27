"""Authenticated historical evidence for a manually promoted ledger."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from weakref import WeakValueDictionary

from finance_core.instance_admission.authority import (
    _MANUAL_RESTORE_FORMAT,
    _close_sqlite_handle,
    _record,
    _state,
)
from finance_core.instance_admission.sessions import (
    _close_cursor_handle,
    _evidence_read_callback,
    _require_runtime_scope,
    _rollback_sqlite_handle,
    _witness_for,
)

from .contracts import LifecycleError, RuntimeBinding, SnapshotCut, cut_ref, digest

if TYPE_CHECKING:
    from .contracts import EvidencePort
    from .receipt_snapshot_v1 import ReceiptSnapshotLookupV1


@dataclass(frozen=True)
class HistoricalGenerationV1:
    """Exact archive cut, runtime identity, application history and Core event anchor."""

    binding: RuntimeBinding
    cut: SnapshotCut
    application_history_sha256: str | None
    event_sha256: str | None

    def __post_init__(self) -> None:
        if (
            type(self.binding) is not RuntimeBinding
            or type(self.cut) is not SnapshotCut
            or (self.binding.instance_id, self.binding.generation)
            != (self.cut.instance_id, self.cut.generation)
            or (
                self.application_history_sha256 is not None
                and not _is_hash(self.application_history_sha256)
            )
            or (
                not _is_hash(self.event_sha256)
                if self.cut.commit_seq
                else self.event_sha256 is not None
            )
        ):
            raise LifecycleError("Exact historical runtime, archive and event anchor required")


@dataclass(frozen=True)
class HistoricalEvidenceV1:
    """Installed application verifier for one authenticated historical archive."""

    generation: HistoricalGenerationV1
    port: EvidencePort
    receipt_snapshot_lookup: ReceiptSnapshotLookupV1 | None = None


def _is_hash(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def historical_generations_sha256_v1(generations: tuple[HistoricalGenerationV1, ...]) -> str:
    """Commit to the exact generations in ancestry order."""
    if (
        type(generations) is not tuple
        or any(type(item) is not HistoricalGenerationV1 for item in generations)
        or len({item.binding.instance_id for item in generations}) != len(generations)
        or len({item.binding.generation for item in generations}) != len(generations)
        or len({item.cut.source_binding_sha256 for item in generations}) != len(generations)
        or any(
            first.cut.commit_seq > second.cut.commit_seq
            for first, second in zip(generations, generations[1:])
        )
    ):
        raise LifecycleError("Exact ordered historical generations required")
    return digest(
        ["manual-restore-historical-generations-v1", [asdict(item) for item in generations]]
    )


def _generations(evidence):
    if type(evidence) is not tuple or any(
        type(item) is not HistoricalEvidenceV1 or item.port is None for item in evidence
    ):
        raise LifecycleError("Exact installed historical evidence required")
    return tuple(item.generation for item in evidence)


def _require_immediate(generations, current, cut, history_sha256):
    historical_generations_sha256_v1(generations)
    if (
        not generations
        or generations[-1].cut != cut
        or generations[-1].application_history_sha256 != history_sha256
        or any(
            (item.application_history_sha256 is None) != (history_sha256 is None)
            or item.binding.instance_id == current.instance_id
            or item.binding.generation == current.generation
            or (item.binding.principal_id, item.binding.client_id)
            != (current.principal_id, current.client_id)
            for item in generations
        )
    ):
        raise LifecycleError("Historical generations differ from the approved archive ancestry")


def _verify_event_anchors(conn, generations):
    from .reader import _audit_event_chain

    if not generations:
        return
    anchors = {item.cut.commit_seq: item.event_sha256 for item in generations}
    if any(anchors[item.cut.commit_seq] != item.event_sha256 for item in generations):
        raise LifecycleError("Historical cuts disagree on their Core event anchor")
    anchors.pop(0, None)
    _audit_event_chain(conn, anchors=anchors)


_SCOPES: ContextVar[tuple[_HistoricalScope, ...]] = ContextVar(
    "finance_historical_evidence", default=()
)


class _HistoricalScope:
    def __init__(self, conn, current, evidence, *, receipt_lookup=None, receipt_cut=None):
        from .daily_v1.accounts import _EvidenceReadPort
        from .reader import SnapshotEvidenceVerifier

        self.conn = conn
        self.current = current
        self.issued = []
        self.active = True
        self.witness = _witness_for(conn)
        self.changes = conn.total_changes
        for item in evidence:
            port = item.port
            if self.witness is not None and type(port) is not _EvidenceReadPort:
                port = _EvidenceReadPort(port)
            verifier = SnapshotEvidenceVerifier(
                port,
                item.generation.binding,
                current.legacy_verifier,
                daily_capacity=current.daily_capacity,
            )
            self.issued.append((item.generation, verifier, item.receipt_snapshot_lookup))
        self.receipt_lookup = receipt_lookup
        self.receipt_cut = receipt_cut

    def check(self):
        if (
            not self.active
            or not self.conn.in_transaction
            or _witness_for(self.conn) is not self.witness
            or self.conn.total_changes != self.changes
            or (self.witness is None and self.conn.execute("PRAGMA query_only").fetchone()[0] != 1)
        ):
            raise LifecycleError("Historical evidence requires its original read transaction")

    def at(self, sequence, *, binding=None, generation=None):
        self.check()
        verifier = self.current
        for item, archived, _ in self.issued:
            if sequence <= item.cut.commit_seq:
                verifier = archived
                break
        if (binding is not None and verifier.binding != binding) or (
            generation is not None and verifier.binding.generation != generation
        ):
            raise LifecycleError("Retained evidence differs from its protected generation")
        return verifier

    def receipt(self, port, binding):
        self.check()
        candidates = [
            (generation.cut, lookup)
            for generation, verifier, lookup in self.issued
            if verifier.port is port and verifier.binding == binding
        ]
        if self.current.port is port and self.current.binding == binding:
            candidates.append((self.receipt_cut, self.receipt_lookup))
        if len(candidates) != 1:
            raise LifecycleError("Historical receipt evidence binding is unavailable")
        cut, lookup = candidates[0]
        if lookup is None:
            return None

        def read(name, *args):
            self.check()
            with _evidence_read_callback(self.conn) if self.witness is not None else nullcontext():
                if (
                    name in {"receipt_manual_fields_reply_at_v1", "receipt_ocr_fields_reply_at_v1"}
                    and args[-1] is None
                ):
                    raise LifecycleError("Exact historical receipt reply bound required")
                if lookup.snapshot_cut != cut:
                    raise LifecycleError("Historical receipt archive cut differs")
                method = getattr(lookup, name, None)
                if not callable(method):
                    raise LifecycleError("Historical receipt archive lacks authenticated evidence")
                result = method(*args)
            self.check()
            return result

        return read


_CLOSE_UNKNOWN: list[tuple[sqlite3.Connection, BinaryIO]] = []


class ArchiveCloseUnknownV1(LifecycleError):
    """Physical SQLite closure is uncertain and archive custody remains held."""


class _ArchiveCursor(sqlite3.Cursor):
    def close(self):
        conn = self.connection
        conn._require_reliable_close()
        try:
            _close_cursor_handle(self)
        except BaseException as exc:
            conn._retain_uncertain_close(exc)
            raise ArchiveCloseUnknownV1("Archived SQLite cursor actual close unknown") from exc
        conn._archive_cursors.pop(id(self), None)


class _ArchiveReadConnection(sqlite3.Connection):
    """Internal reader whose transaction and authorizer belong to Core."""

    _archive_owner: _ArchiveTransaction | None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Forgotten temporary cursors finalize normally; track every still-live
        # statement, including cursors deliberately retained by the caller.
        self._archive_cursors = WeakValueDictionary()
        self._archive_retained_cursors = ()
        self._archive_close_error = None
        self._archive_owner = None

    def _retain_uncertain_close(self, exc):
        self._archive_close_error = exc
        self._archive_retained_cursors = tuple(self._archive_cursors.values())

    def _require_reliable_close(self):
        if self._archive_close_error is not None:
            raise ArchiveCloseUnknownV1(
                "Archived SQLite actual close unknown"
            ) from self._archive_close_error

    def _require_idle_guard(self):
        self._require_reliable_close()
        if self._archive_owner is not None and self._archive_owner.active:
            raise LifecycleError("Core owns the archive read transaction")

    def __setattr__(self, name, value):
        if name in {"row_factory", "text_factory", "autocommit", "isolation_level"}:
            self._require_idle_guard()
        super().__setattr__(name, value)

    def set_authorizer(self, callback):
        self._require_idle_guard()
        return super().set_authorizer(callback)

    def cursor(self, factory=None):
        self._require_reliable_close()
        if factory is not None and factory is not _ArchiveCursor:
            raise LifecycleError("Archive cursor factory must retain Core ownership")
        cursor = super().cursor(_ArchiveCursor)
        self._archive_cursors[id(cursor)] = cursor
        return cursor

    def execute(self, sql, parameters=(), /):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters, /):
        self._require_idle_guard()
        return self.cursor().executemany(sql, seq_of_parameters)

    def close(self):
        self._require_idle_guard()
        _close_archive_connection(self)

    def blobopen(self, *args, **kwargs):
        raise LifecycleError("Archive read supports only owned SQL result cursors")

    def iterdump(self, *args, **kwargs):
        raise LifecycleError("Archive read supports only owned SQL result cursors")

    def executescript(self, script):
        self._require_idle_guard()
        return super().executescript(script)

    def deserialize(self, data, /, *, name="main"):
        self._require_idle_guard()
        return super().deserialize(data, name=name)


def _close_archive_connection(conn, *, close_connection=None):
    """Finalize each pending statement while its SQLite handle is still live."""
    conn._require_reliable_close()
    try:
        if conn.in_transaction:
            _rollback_sqlite_handle(conn)
        for cursor in tuple(conn._archive_cursors.values()):
            _close_cursor_handle(cursor)
            conn._archive_cursors.pop(id(cursor), None)
        (_close_sqlite_handle if close_connection is None else close_connection)(conn)
    except BaseException as exc:
        conn._retain_uncertain_close(exc)
        raise ArchiveCloseUnknownV1("Archived SQLite actual close unknown") from exc


@dataclass
class _ArchiveTransaction:
    connection: _ArchiveReadConnection
    database_sha256: str
    pid: int
    active: bool = True
    violated: bool = False


_ARCHIVE_TRANSACTIONS: ContextVar[tuple[_ArchiveTransaction, ...]] = ContextVar(
    "finance_archive_read_transactions", default=()
)


def _archive_transaction_for(conn):
    if type(conn) is _ArchiveReadConnection:
        owner = conn._archive_owner
        if (
            owner is not None
            and owner.connection is conn
            and owner.active
            and owner.pid == os.getpid()
            and any(item is owner for item in _ARCHIVE_TRANSACTIONS.get())
        ):
            return owner
    return None


def is_archive_read_connection_v1(conn: sqlite3.Connection) -> bool:
    """Report whether the caller is in this Core-issued archive read context."""
    return _archive_transaction_for(conn) is not None


def _require_archive_transaction(conn, database_sha256, *, original=None):
    witness = _archive_transaction_for(conn)
    if (
        witness is None
        or (original is not None and witness is not original)
        or conn._archive_close_error is not None
        or witness.database_sha256 != database_sha256
        or not conn.in_transaction
        or conn.row_factory is not sqlite3.Row
        or conn.execute("PRAGMA query_only").fetchone()[0] != 1
    ):
        raise LifecycleError("Core-issued archive read transaction required")
    if witness.violated:
        raise LifecycleError("Historical application audit violated its read-only scope")
    return witness


@contextmanager
def _issued_archive_transaction(conn, database_sha256):
    """Pin one Core-opened immutable/held transaction through the proof callbacks."""
    if (
        type(conn) is not _ArchiveReadConnection
        or conn._archive_owner is not None
        or not conn.in_transaction
        or conn.execute("PRAGMA query_only").fetchone()[0] != 1
    ):
        raise LifecycleError("Core archive read transaction required")
    witness = _ArchiveTransaction(conn, database_sha256, os.getpid())

    def authorize(action, first, second, _database, _source):
        if action in {
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_SAVEPOINT,
            sqlite3.SQLITE_TRANSACTION,
        } or (
            action == sqlite3.SQLITE_PRAGMA
            and first in {"query_only", "writable_schema", "foreign_keys"}
            and second is not None
        ):
            witness.violated = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn._archive_owner = witness
    # Evidence requires the issuing context. The connection owns its guards
    # even when a caller uses the handle in a different logical context.
    token = _ARCHIVE_TRANSACTIONS.set((*_ARCHIVE_TRANSACTIONS.get(), witness))
    try:
        sqlite3.Connection.set_authorizer(conn, authorize)
        yield
        _require_archive_transaction(conn, database_sha256, original=witness)
    finally:
        try:
            sqlite3.Connection.set_authorizer(conn, None)
        finally:
            witness.active = False
            conn._archive_owner = None
            _ARCHIVE_TRANSACTIONS.reset(token)


@contextmanager
def open_archive_read_connection_v1(path: Path, *, database_sha256: str):
    """Read exact archive bytes, retaining custody on ArchiveCloseUnknownV1."""
    if not _is_hash(database_sha256):
        raise LifecycleError("Exact archived database digest required")
    path = Path(path).resolve(strict=True)
    body = path.open("rb")
    retained = False
    try:

        def identity(details):
            return (
                details.st_dev,
                details.st_ino,
                details.st_size,
                details.st_mtime_ns,
                details.st_ctime_ns,
            )

        original = os.fstat(body.fileno())

        def require_unchanged():
            if identity(original) != identity(path.stat()) or (
                identity(original) != identity(os.fstat(body.fileno()))
            ):
                raise LifecycleError("Archived database changed during protected read")

        if not stat.S_ISREG(original.st_mode):
            raise LifecycleError("Exact regular archived database required")
        if hashlib.file_digest(body, "sha256").hexdigest() != database_sha256:
            raise LifecycleError("Archive file differs from its database digest")
        require_unchanged()
        conn = sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1",
            uri=True,
            timeout=0,
            factory=_ArchiveReadConnection,
        )
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            require_unchanged()
            with _issued_archive_transaction(conn, database_sha256):
                yield conn
        finally:
            try:
                _close_archive_connection(conn)
            except BaseException as exc:
                retained = True
                _CLOSE_UNKNOWN.append((conn, body))
                raise ArchiveCloseUnknownV1("Actual archived SQLite close is uncertain") from exc
        require_unchanged()
    finally:
        if not retained:
            body.close()


def _scope_for(conn):
    return next((scope for scope in _SCOPES.get() if scope.conn is conn), None)


@contextmanager
def _issued_scope(conn, service, evidence, *, receipt_lookup=None, receipt_cut=None):
    scope = _HistoricalScope(
        conn, service, evidence, receipt_lookup=receipt_lookup, receipt_cut=receipt_cut
    )
    scope.check()
    token = _SCOPES.set((*_SCOPES.get(), scope))
    try:
        yield
        scope.check()
    finally:
        scope.active = False
        _SCOPES.reset(token)


def _protected_history(conn, service, *, active_only=False):
    witness = _witness_for(conn)
    evidence = getattr(service, "historical_evidence", ())
    if witness is None:
        if evidence:
            raise LifecycleError("Historical evidence requires actual production authority")
        return None
    owner = getattr(service, "owner", None)
    if owner is None:
        if evidence:
            raise LifecycleError("Historical evidence requires an installed matching service")
        return None
    if witness.owner is not owner:
        raise LifecycleError("Historical evidence requires its matching production owner")
    config = owner.config
    state = _state(config, _proof=witness if witness.immutable_migration_digest else None)
    record = _record(config, state)
    if active_only and (record["state"] != "ACTIVE" or state["current"] != config.binding_sha256):
        raise LifecycleError("Runtime archive audit requires the current ACTIVE instance")
    entry = next(
        (
            entry
            for entry in state["history"]
            if entry["binding"] == config.binding_sha256
            and entry["operation_id"] == record["admission"]
        ),
        None,
    )
    context = entry.get("recovery_context", {}) if entry is not None else {}
    commitment = context.get("historical_generations_sha256")
    if commitment is None and not evidence:
        return None
    _require_runtime_scope(conn, service.binding, service.runtime_admission)
    generations = _generations(evidence)
    if (
        not (
            (record["state"] == "ACTIVE" and state["current"] == config.binding_sha256)
            or (record["state"] == "FENCED" and witness.read_only)
        )
        or entry is None
        or entry["action"] != "restore_admit"
        or context.get("format") != _MANUAL_RESTORE_FORMAT
        or context["runtime_entry_sha256"] != service.runtime_admission.registration_entry_sha256
        or not generations
        or any(item.receipt_snapshot_lookup is not None for item in evidence)
        or historical_generations_sha256_v1(generations) != commitment
        or cut_ref(generations[-1].cut) != context["cut_ref"]
    ):
        raise LifecycleError("Historical evidence differs from protected promotion commitment")
    _require_immediate(
        generations, service.binding, generations[-1].cut, context["application_history_sha256"]
    )
    _verify_event_anchors(conn, generations)
    return evidence


@contextmanager
def _active_scope(conn, service, verifier=None):
    if _scope_for(conn) is not None:
        yield
        return
    evidence = _protected_history(conn, service)
    if evidence is None:
        yield
        return
    with _issued_scope(conn, verifier if verifier is not None else service, evidence):
        yield


@contextmanager
def _archive_scope(conn, service, cut, evidence, receipt_lookup):
    _require_archive_transaction(conn, cut.database_sha256)
    generations = _generations(evidence)
    if (service.binding.instance_id, service.binding.generation) != (
        cut.instance_id,
        cut.generation,
    ) or any(
        item.cut.commit_seq > cut.commit_seq
        or item.binding.instance_id == service.binding.instance_id
        or item.binding.generation == service.binding.generation
        or (item.binding.principal_id, item.binding.client_id)
        != (service.binding.principal_id, service.binding.client_id)
        for item in generations
    ):
        raise LifecycleError("Historical archive evidence requires its exact read transaction")
    schemas = {row[1] for row in conn.execute("PRAGMA database_list")}
    if (
        "main" not in schemas
        or schemas - {"main", "temp"}
        or conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
        ).fetchone()
    ):
        raise LifecycleError("Historical archive proof requires an isolated read-only database")
    historical_generations_sha256_v1(generations)
    _verify_event_anchors(conn, generations)
    if _scope_for(conn) is not None:
        raise LifecycleError("Historical archive evidence is already scoped")
    with _issued_scope(conn, service, evidence, receipt_lookup=receipt_lookup, receipt_cut=cut):
        yield


def _event_sequence(conn, kind, identity, *, _rows=None):
    rows = (
        _rows.project_many(conn, "entity_events", ("seq",), kind, identity)
        if _rows is not None
        else conn.execute(
            "SELECT seq FROM l2_events WHERE kind=? AND entity_id=?", (kind, identity)
        ).fetchall()
    )
    if len(rows) != 1:
        raise LifecycleError("Historical evidence lacks its exact native event")
    return rows[0][0]


def _event_service(conn, service, kind, identity, binding, *, _rows=None):
    scope = _scope_for(conn)
    if scope is None:
        return service
    if type(binding) is not dict:
        raise LifecycleError("Exact retained runtime binding required for historical evidence")
    try:
        binding = RuntimeBinding(**binding)
    except TypeError as exc:
        raise LifecycleError(
            "Exact retained runtime binding required for historical evidence"
        ) from exc
    return scope.at(_event_sequence(conn, kind, identity, _rows=_rows), binding=binding)


def _source_service(conn, service, row, *, _rows=None):
    scope = _scope_for(conn)
    if scope is None:
        return service
    return scope.at(
        _event_sequence(conn, "capture", row["source_id"], _rows=_rows),
        generation=row["generation"],
    )


def _historical_receipt_lookup(conn, port, binding):
    scope = _scope_for(conn)
    return scope.receipt(port, binding) if scope is not None else None


def audit_archive_evidence_connection_v1(
    conn,
    port,
    binding: RuntimeBinding,
    cut: SnapshotCut,
    *,
    legacy_verifier=None,
    historical_evidence: tuple[HistoricalEvidenceV1, ...] = (),
    parent_history_sha256: str,
    receipt_snapshot_lookup: ReceiptSnapshotLookupV1 | None = None,
):
    """Audit an independently authenticated frozen archive and its parent history.

    The composing verifier authenticates the source archive, the protected
    parent commitment and all installed evidence ports. The result conveys
    only same-transaction audit evidence.
    """
    from .reader import SnapshotEvidenceVerifier, audit_evidence_connection
    from .recovery import _verify_cut_connection

    if type(cut) is not SnapshotCut:
        raise LifecycleError("Exact archive cut required")
    witness = _require_archive_transaction(conn, cut.database_sha256)
    if historical_generations_sha256_v1(_generations(historical_evidence)) != parent_history_sha256:
        raise LifecycleError("Archived parent evidence differs from its protected commitment")
    _verify_cut_connection(conn, cut)
    verifier = SnapshotEvidenceVerifier(port, binding, legacy_verifier)
    with _archive_scope(conn, verifier, cut, historical_evidence, receipt_snapshot_lookup):
        result = audit_evidence_connection(conn, port, binding, legacy_verifier)
        _verify_cut_connection(conn, cut)
    _require_archive_transaction(conn, cut.database_sha256, original=witness)
    return result


def audit_runtime_archive_evidence_connection_v1(
    conn,
    service,
    port,
    binding: RuntimeBinding,
    cut: SnapshotCut,
    *,
    receipt_snapshot_lookup: ReceiptSnapshotLookupV1 | None = None,
):
    """Audit an exact archive cut on the live service's issued read-only session."""
    from .daily_v1.accounts import _EvidenceReadPort
    from .reader import SnapshotEvidenceVerifier, audit_evidence_connection
    from .recovery import _verify_cut_connection
    from .service import LedgerService

    witness = _witness_for(conn)
    if (
        type(service) is not LedgerService
        or witness is None
        or not witness.read_only
        or witness.owner is not service.owner
        or not conn.in_transaction
        or type(binding) is not RuntimeBinding
        or binding != service.binding
        or type(cut) is not SnapshotCut
        or (binding.instance_id, binding.generation) != (cut.instance_id, cut.generation)
        or service.owner.config.binding_sha256 != cut.source_binding_sha256
        or _scope_for(conn) is not None
    ):
        raise LifecycleError("Runtime archive audit requires the exact issued service read")
    with service._runtime_scope():
        _require_runtime_scope(conn, binding, service.runtime_admission)
        evidence = _protected_history(conn, service, active_only=True) or ()
        _verify_cut_connection(conn, cut)
        if type(port) is not _EvidenceReadPort:
            port = _EvidenceReadPort(port)
        verifier = SnapshotEvidenceVerifier(
            port, binding, service.legacy_verifier, daily_capacity=service.daily_capacity
        )
        with _issued_scope(
            conn, verifier, evidence, receipt_lookup=receipt_snapshot_lookup, receipt_cut=cut
        ):
            result = audit_evidence_connection(
                conn, port, binding, service.legacy_verifier, daily_capacity=service.daily_capacity
            )
            _verify_cut_connection(conn, cut)
            return result
