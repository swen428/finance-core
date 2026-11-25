"""Receipt lookup capability for one independently verified historical snapshot cut."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from finance_core.instance_admission import require_finance_read_connection
from finance_core.instance_admission.sessions import _evidence_read_callback, _witness_for

from .contracts import LifecycleError, RuntimeBinding, SnapshotCut, VerifiedReceiptSourceV1

if TYPE_CHECKING:
    from .receipt_posting_v1 import VerifiedReceiptCardDeliveryV1, VerifiedReceiptConfirmActionV1
    from .receipt_retained_manual_v1 import ReceiptJournalReplyRefV1, VerifiedReceiptManualHistoryV1
    from .receipt_retained_ocr_v1 import RetainedReceiptOcrEvidenceV1


class ReceiptSnapshotLookupV1(Protocol):
    """Independently authenticated historical journal and receipt originals at the given cut."""

    @property
    def snapshot_cut(self) -> SnapshotCut: ...

    def receipt_source_v1(self, evidence_id: str) -> VerifiedReceiptSourceV1: ...

    def receipt_retained_manual_history_v1(
        self, original_evidence_id: str, fields_reply: ReceiptJournalReplyRefV1
    ) -> VerifiedReceiptManualHistoryV1: ...

    def receipt_manual_fields_reply_at_v1(
        self, original_evidence_id: str, through: ReceiptJournalReplyRefV1
    ) -> ReceiptJournalReplyRefV1 | None: ...

    def receipt_card_delivery_v1(self, evidence_id: str) -> VerifiedReceiptCardDeliveryV1: ...

    def receipt_confirm_action_v1(self, evidence_id: str) -> VerifiedReceiptConfirmActionV1: ...


class ReceiptOcrSnapshotLookupV1(ReceiptSnapshotLookupV1, Protocol):
    """Snapshot lookups with original signed OCR history and retained bundle custody."""

    def receipt_retained_ocr_history_v1(
        self, original_evidence_id: str, fields_reply: ReceiptJournalReplyRefV1
    ) -> RetainedReceiptOcrEvidenceV1: ...

    def receipt_ocr_fields_reply_at_v1(
        self, original_evidence_id: str, through: ReceiptJournalReplyRefV1
    ) -> ReceiptJournalReplyRefV1 | None: ...


_LOOKUPS = (
    "receipt_source_v1",
    "receipt_retained_manual_history_v1",
    "receipt_manual_fields_reply_at_v1",
    "receipt_card_delivery_v1",
    "receipt_confirm_action_v1",
)


_REVISION_LOOKUPS = ("receipt_manual_fields_reply_at_v1", "receipt_ocr_fields_reply_at_v1")
_OCR_LOOKUPS = ("receipt_retained_ocr_history_v1", "receipt_ocr_fields_reply_at_v1")


class _ReceiptSnapshotScope:
    def __init__(self, conn, port, binding, cut, lookup, witness):
        self.connection = conn
        self.port = port
        self.binding = binding
        self.cut = cut
        self.lookup = lookup
        self.witness = witness
        self.changes = conn.total_changes
        self.active = False

    def _lookup_context(self):
        if self.witness is None:
            return nullcontext()
        return _evidence_read_callback(self.connection)

    def check_transaction(self):
        conn = self.connection
        if (
            not conn.in_transaction
            or conn.total_changes != self.changes
            or conn.row_factory is not sqlite3.Row
            or _witness_for(conn) is not self.witness
            or (self.witness is None and conn.execute("PRAGMA query_only").fetchone()[0] != 1)
        ):
            raise LifecycleError("Receipt snapshot evidence transaction changed")
        with self._lookup_context():
            bound_cut = getattr(self.lookup, "snapshot_cut", None)
            if type(bound_cut) is not SnapshotCut or bound_cut != self.cut:
                raise LifecycleError("Exact historical receipt snapshot evidence required")

    def check_lookup_methods(self):
        with self._lookup_context():
            if any(not callable(getattr(self.lookup, name, None)) for name in _LOOKUPS):
                raise LifecycleError("Exact historical receipt snapshot evidence required")

    def require_issued(self):
        if not self.active or not any(scope is self for scope in _SCOPES.get()):
            raise LifecycleError("Receipt snapshot evidence scope was not issued or expired")
        self.check_transaction()

    def read(self, name, *args):
        self.require_issued()
        if name not in (*_LOOKUPS, *_OCR_LOOKUPS) or (
            name in _REVISION_LOOKUPS and args[-1] is None
        ):
            raise LifecycleError("Exact historical receipt lookup required")
        with self._lookup_context():
            read = getattr(self.lookup, name, None)
            if not callable(read):
                raise LifecycleError("Snapshot lookup lacks authenticated receipt evidence")
            actual = read(*args)
        self.require_issued()
        return actual


_SCOPES: ContextVar[tuple[_ReceiptSnapshotScope, ...]] = ContextVar(
    "lifecycle_receipt_snapshot_scopes", default=()
)


def _receipt_snapshot_for(conn, port, binding):
    for scope in _SCOPES.get():
        if scope.connection is conn:
            scope.require_issued()
            if (
                scope.port is not port
                or type(binding) is not RuntimeBinding
                or scope.binding != binding
            ):
                raise LifecycleError("Receipt snapshot evidence port or binding differs")
            return scope
    return None


@contextmanager
def receipt_snapshot_evidence_scope(
    conn: sqlite3.Connection,
    port,
    binding: RuntimeBinding,
    cut: SnapshotCut,
    lookup: ReceiptSnapshotLookupV1,
):
    """Qualify historical receipt reads for one exact caller-owned read-only transaction.

    The composing verifier authenticates the original cut and the lookup's
    journal, transport and archived receipt evidence. Core independently verifies
    this transaction against that cut. Receipt lookup methods receive identities
    only. The result has no source admission, write or restore authority.
    """
    from .daily_v1.accounts import _EvidenceReadPort
    from .recovery import _verify_cut_connection

    witness = _witness_for(conn)
    if (
        (witness is None and type(conn) is not sqlite3.Connection)
        or type(binding) is not RuntimeBinding
        or type(cut) is not SnapshotCut
        or (binding.instance_id, binding.generation) != (cut.instance_id, cut.generation)
    ):
        raise LifecycleError("Exact historical receipt snapshot evidence required")
    if witness is not None:
        if (
            not witness.read_only
            or witness.owner.config.binding_sha256 != cut.source_binding_sha256
        ):
            raise LifecycleError("Receipt snapshot requires the exact admitted source read")
        require_finance_read_connection(conn, instance_id=binding.instance_id)
        if type(port) is not _EvidenceReadPort:
            port = _EvidenceReadPort(port)
    previous = _SCOPES.get()
    if any(scope.connection is conn for scope in previous):
        raise LifecycleError("Receipt snapshot evidence already scoped on this connection")
    scope = _ReceiptSnapshotScope(conn, port, replace(binding), replace(cut), lookup, witness)
    scope.check_transaction()
    scope.check_lookup_methods()
    schemas = {row[1] for row in conn.execute("PRAGMA database_list")}
    if (
        "main" not in schemas
        or schemas - {"main", "temp"}
        or conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
        ).fetchone()
    ):
        raise LifecycleError("Receipt snapshot requires one isolated read-only schema")
    _verify_cut_connection(conn, scope.cut)
    scope.check_transaction()
    token = _SCOPES.set((*previous, scope))
    scope.active = True
    try:
        yield port
        scope.require_issued()
        _verify_cut_connection(conn, scope.cut)
    finally:
        scope.active = False
        _SCOPES.reset(token)


@contextmanager
def _receipt_lookup(port, conn, binding):
    """Keep live receipt callbacks on the admitted connection and copy lookups detached."""
    from .daily_v1.accounts import _EvidenceReadPort

    scope = _receipt_snapshot_for(conn, port, binding)
    if scope is not None:
        yield scope.read
        return
    if type(port) is not _EvidenceReadPort:
        raise LifecycleError("Receipt history requires a protected ledger or exact snapshot cut")

    def read(name, *args):
        verify = getattr(port._port, name, None)
        if not callable(verify):
            raise LifecycleError("Installed evidence port lacks authenticated receipt evidence")
        return verify(conn, *args)

    with _evidence_read_callback(conn):
        yield read
