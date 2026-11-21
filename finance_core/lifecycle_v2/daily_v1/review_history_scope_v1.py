"""Same-transaction namespace authentication for movement review history."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from sqlite3 import Connection
from threading import get_ident

from finance_core.instance_admission import ProductionOwner
from finance_core.instance_admission.sessions import _require_session, _Witness, _witness_for

from ..contracts import LifecycleError, RuntimeBinding


@dataclass(slots=True)
class _ReviewHistoryScope:
    conn: Connection
    port: object
    owner: ProductionOwner
    binding: RuntimeBinding
    witness: _Witness
    thread_id: int
    authority_version: int
    total_changes: int
    row_factory: object
    text_factory: object
    active: bool = True
    authenticating: bool = False
    authenticated: bool = False
    failed: bool = False

    def callback_token(self, conn: Connection, port: object) -> object:
        witness = self.witness
        if (
            not self.active
            or self.thread_id != get_ident()
            or conn is not self.conn
            or port is not self.port
            or _witness_for(conn) is not witness
            or witness.owner is not self.owner
            or witness.read_only
            or not witness.active
            or witness.closing
            or witness.closed
            or witness.authority_version != self.authority_version
            or witness.evidence_callback_violated
            or witness.evidence_callback_token is None
            or not conn.in_transaction
            or conn.total_changes != self.total_changes
            or conn.row_factory is not self.row_factory
            or conn.text_factory is not self.text_factory
        ):
            raise LifecycleError("Review history requires its original protected evidence callback")
        return witness.evidence_callback_token


_CURRENT: ContextVar[_ReviewHistoryScope | None] = ContextVar(
    "movement_review_history", default=None
)


@contextmanager
def _movement_review_history_scope_v1(
    conn: Connection, port: object, owner: ProductionOwner, binding: RuntimeBinding
) -> Iterator[None]:
    """Bind the historical audit to the transaction just started by prepare_review."""
    if binding.environment != "single-host-synthetic":
        yield
        return
    if _CURRENT.get() is not None:
        raise LifecycleError("Nested review history scope refused")
    witness = _witness_for(conn)
    if (
        witness is None
        or witness.owner is not owner
        or witness.read_only
        or not witness.active
        or witness.closing
        or witness.closed
        or witness.evidence_callback_token is not None
        or not conn.in_transaction
    ):
        raise LifecycleError("Review history requires the issued review transaction")
    scope = _ReviewHistoryScope(
        conn,
        port,
        owner,
        binding,
        witness,
        get_ident(),
        witness.authority_version,
        conn.total_changes,
        conn.row_factory,
        conn.text_factory,
    )
    reset = _CURRENT.set(scope)
    try:
        yield
        if scope.failed:
            raise LifecycleError("Review history namespace scope failed")
    finally:
        scope.active = False
        _CURRENT.reset(reset)


def authenticate_movement_review_history_namespaces_v1(
    conn: Connection, port: object, authenticate: Callable[[], None]
) -> bool:
    """Run the adapter's complete scans once inside the exact history callback.

    False means the caller must run its ordinary scans. An active scope can
    only retain successful completion, never material, permissions or the
    callable. Per-item evidence and admission checks remain the caller's job.
    """
    scope = _CURRENT.get()
    if scope is None:
        return False
    try:
        if scope.failed or scope.authenticating:
            raise LifecycleError("Review history namespace scope failed")
        token = scope.callback_token(conn, port)
        if scope.authenticated:
            return True
        scope.authenticating = True
        _require_session(conn, write=True, instance_id=scope.binding.instance_id)
        authenticate()
        _require_session(conn, write=True, instance_id=scope.binding.instance_id)
        if scope.failed or scope.callback_token(conn, port) is not token:
            raise LifecycleError("Review history namespace scope failed")
        scope.authenticating = False
        scope.authenticated = True
        return True
    except BaseException:
        scope.failed = True
        raise
