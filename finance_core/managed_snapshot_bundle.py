"""Fixed, bounded Core-committed snapshot package and independent readback.

Only the delegated cut worker/reader call this module.  Database paths are
fixed inside FD6's private stage; file paths in database rows are evidence to
validate, never authority to open arbitrary locations.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from finance_core import API_CONTRACT_VERSION
from finance_core.intake import attachment_publication as publication
from finance_core.managed_cut_protocol import (
    BUNDLE_LIMITS,
    BUNDLE_LIMITS_VERSION,
    BUNDLE_REGISTRY_VERSION,
    BUNDLE_SCOPE,
    CutRequest,
)
from finance_core.managed_disk_snapshot import StagedDiskSnapshot
from finance_core.profile_paths import (
    ManagedStagingProfile,
    _migration_contract_digest,
    _reject_acl_grants,
)
from finance_core.reconciliation.migrations import (
    FINANCE_APPLICATION_ID,
    TEMP_DB_MIGRATION_PATHS,
    migration_ledger_rows,
    schema_fingerprint,
    verify_migration_history,
)

FORMAT = "core-committed-snapshot-manifest-v1"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MEMBER = re.compile(r"attachments/[0-9a-f]{2}/[0-9a-f]{64}\.(?:jpg|png|pdf)\Z")
_CHUNK = 65_536


class BundleError(RuntimeError):
    """No complete bundle receipt may be issued for this stage."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("Duplicate manifest field")
        result[key] = value
    return result


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _check(deadline: float, control_check: Callable[[], None]) -> None:
    if time.monotonic() >= deadline:
        raise BundleError("Bundle deadline expired")
    control_check()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_hash(value: Any) -> str:
    if type(value) is not str or not _HEX.fullmatch(value):
        raise BundleError("Referenced content hash is unsupported")
    return value


def _require_path(value: Any) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise BundleError("Referenced attachment path is unsupported")
    return value


def _require_size(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise BundleError("Referenced attachment size is unsupported")
    return value


def _member_from_path(path: str, root: Path, expected_hash: str | None) -> str:
    prefix = str(root) + "/"
    if not path.startswith(prefix) or "//" in path or "/./" in path or "/../" in path:
        raise BundleError("Attachment is outside the fixed managed root")
    relative = "attachments/" + path[len(prefix) :]
    if not _MEMBER.fullmatch(relative):
        raise BundleError("Attachment is not a canonical managed member")
    digest = relative.split("/")[-1].split(".")[0]
    if digest[:2] != relative.split("/")[1] or (
        expected_hash is not None and digest != expected_hash
    ):
        raise BundleError("Attachment path and recorded hash differ")
    return relative


@dataclass(frozen=True)
class Inventory:
    members: dict[str, tuple[str, int | None, str | None]]
    facts: tuple[dict[str, Any], ...]
    digest: str


def _rows(
    conn: sqlite3.Connection, table: str, columns: str, limit: int, check: Callable[[], None]
) -> list[sqlite3.Row]:
    cursor = conn.execute(f"SELECT {columns} FROM {table} ORDER BY 1")
    result: list[sqlite3.Row] = []
    retained_bytes = 0
    try:
        while True:
            check()
            batch = cursor.fetchmany(32)
            if not batch:
                return result
            for row in batch:
                for value in row:
                    if isinstance(value, str):
                        retained_bytes += len(value.encode("utf-8"))
                    elif isinstance(value, bytes):
                        retained_bytes += len(value)
                    else:
                        retained_bytes += 16
                if retained_bytes > BUNDLE_LIMITS["max_core_db_bytes"]:
                    raise BundleError("Reference registry memory budget exceeded")
            result.extend(batch)
            if len(result) > limit:
                raise BundleError("Reference registry row budget exceeded")
    finally:
        cursor.close()


def collect_references(
    conn: sqlite3.Connection,
    trusted_attachment_root: Path,
    limits: dict[str, int],
    deadline: float,
    control_check: Callable[[], None],
) -> Inventory:
    """Enumerate every v1 registered file reference from one closed output DB."""
    if limits != BUNDLE_LIMITS or not trusted_attachment_root.is_absolute():
        raise BundleError("Bundle inventory authority is invalid")

    def check() -> None:
        _check(deadline, control_check)

    verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS, require_complete=True)
    attachments = _rows(
        conn,
        "attachments",
        "id,public_id,file_path,file_hash,mime_type",
        limits["max_references"],
        check,
    )
    by_id: dict[int, dict[str, Any]] = {}
    by_path: dict[str, list[int]] = {}
    public_ids: dict[str, int] = {}
    hashes: dict[int, set[str]] = {}
    sizes: dict[int, set[int]] = {}
    source_aliases: dict[int, set[str]] = {}
    claims: list[tuple[str, str, str, int | None, str | None, str | None, int | None, bool]] = []

    def claim(
        table: str,
        key: Any,
        column: str,
        *,
        aid: Any = None,
        path: Any = None,
        digest: Any = None,
        size: Any = None,
        alias: bool = False,
    ) -> None:
        if len(claims) >= limits["max_references"]:
            raise BundleError("Reference fact budget exceeded")
        if aid is not None and (type(aid) is not int or aid not in by_id):
            raise BundleError("Referenced attachment ID is missing")
        if path is not None:
            path = _require_path(path)
        if digest is not None:
            digest = _require_hash(digest)
        size = _require_size(size)
        if aid is None and path is None:
            if digest is not None or size is not None:
                raise BundleError("Unbound attachment evidence is unsupported")
            return
        if aid is not None:
            if digest is not None:
                hashes[aid].add(digest)
            if size is not None:
                sizes[aid].add(size)
        claims.append((table, str(key), column, aid, path, digest, size, alias))

    for row in attachments:
        aid = row["id"]
        if type(aid) is not int or aid in by_id or type(row["public_id"]) is not str:
            raise BundleError("Attachment declaration is invalid")
        attachment_path = _require_path(row["file_path"])
        by_id[aid] = dict(row)
        public_ids[row["public_id"]] = aid
        by_path.setdefault(attachment_path, []).append(aid)
        hashes[aid] = set()
        sizes[aid] = set()
        source_aliases[aid] = set()
        claim(
            "attachments",
            aid,
            "file_path",
            aid=aid,
            path=attachment_path,
            digest=row["file_hash"],
        )

    intake = {
        row["id"]: row
        for row in _rows(
            conn,
            "raw_intake_records",
            "id,public_id,attachment_id,attachment_path,attachment_hash",
            limits["max_references"],
            check,
        )
    }
    for row in intake.values():
        claim(
            "raw_intake_records",
            row["id"],
            "attachment_path",
            aid=row["attachment_id"],
            path=row["attachment_path"],
            digest=row["attachment_hash"],
            alias=True,
        )

    telegram_rows = _rows(
        conn,
        "telegram_attachment_source",
        "id,attachment_id,raw_intake_record_id,original_attachment_path,content_hash,observed_file_size",
        limits["max_references"],
        check,
    )
    telegram = {row["id"]: row for row in telegram_rows}
    for row in telegram_rows:
        original = intake.get(row["raw_intake_record_id"])
        if original is None or original["attachment_id"] != row["attachment_id"]:
            raise BundleError("Telegram attachment/intake binding differs")
        source_aliases[row["attachment_id"]].add(row["original_attachment_path"])
        claim(
            "telegram_attachment_source",
            row["id"],
            "original_attachment_path",
            aid=row["attachment_id"],
            path=row["original_attachment_path"],
            digest=row["content_hash"],
            size=row["observed_file_size"],
            alias=True,
        )

    for row in _rows(
        conn,
        "local_attachment_source",
        "id,attachment_id,raw_intake_record_id,workspace_copy_path,content_hash,observed_file_size",
        limits["max_references"],
        check,
    ):
        original = intake.get(row["raw_intake_record_id"])
        if original is None or original["attachment_id"] != row["attachment_id"]:
            raise BundleError("Local attachment/intake binding differs")
        claim(
            "local_attachment_source",
            row["id"],
            "workspace_copy_path",
            aid=row["attachment_id"],
            path=row["workspace_copy_path"],
            digest=row["content_hash"],
            size=row["observed_file_size"],
        )

    for table, columns in (
        ("parser_outputs", "id,attachment_id"),
        (
            "raw_intake_evidence",
            "id,raw_intake_record_id,attachment_id,attachment_path,attachment_hash,source_file_hash",
        ),
        ("receipts", "id,attachment_id,attachment_path,payment_record_attachment_id"),
        ("payment_records", "id,attachment_id,attachment_path"),
        (
            "receipt_ocr_extractions",
            "id,attachment_id,source_attachment_hash,source_attachment_size",
        ),
    ):
        for row in _rows(conn, table, columns, limits["max_references"], check):
            key = row["id"]
            if table == "parser_outputs":
                claim(table, key, "attachment_id", aid=row["attachment_id"])
            elif table == "raw_intake_evidence":
                raw = intake.get(row["raw_intake_record_id"])
                if raw is None or (
                    row["attachment_id"] is not None
                    and raw["attachment_id"] not in {None, row["attachment_id"]}
                ):
                    raise BundleError("Raw evidence/intake attachment binding differs")
                digest = row["attachment_hash"]
                if (
                    row["source_file_hash"] is not None
                    and digest is not None
                    and row["source_file_hash"] != digest
                ):
                    raise BundleError("Evidence attachment hashes conflict")
                claim(
                    table,
                    key,
                    "attachment_path",
                    aid=row["attachment_id"],
                    path=row["attachment_path"],
                    digest=digest or row["source_file_hash"],
                    alias=True,
                )
            elif table == "receipts":
                claim(
                    table,
                    key,
                    "attachment_path",
                    aid=row["attachment_id"],
                    path=row["attachment_path"],
                    alias=True,
                )
                claim(
                    table,
                    key,
                    "payment_record_attachment_id",
                    aid=row["payment_record_attachment_id"],
                )
            elif table == "payment_records":
                claim(
                    table,
                    key,
                    "attachment_path",
                    aid=row["attachment_id"],
                    path=row["attachment_path"],
                    alias=True,
                )
            else:
                claim(
                    table,
                    key,
                    "attachment_id",
                    aid=row["attachment_id"],
                    digest=row["source_attachment_hash"],
                    size=row["source_attachment_size"],
                )

    for row in _rows(
        conn,
        "finance_capture_jobs",
        "id,raw_intake_record_id,capture_kind,attachment_evidence_id,attachment_content_hash",
        limits["max_references"],
        check,
    ):
        if row["capture_kind"] == "receipt_image":
            source = telegram.get(row["attachment_evidence_id"])
            if source is None or source["raw_intake_record_id"] != row["raw_intake_record_id"]:
                raise BundleError("Capture job has no bound original image")
            claim(
                "finance_capture_jobs",
                row["id"],
                "attachment_evidence_id",
                aid=source["attachment_id"],
                digest=row["attachment_content_hash"],
            )
        elif (
            row["attachment_evidence_id"] is not None or row["attachment_content_hash"] is not None
        ):
            raise BundleError("Text capture has unexpected attachment evidence")

    for table, columns in (
        ("statement_batches", "id,source_file_path,source_file_hash"),
        (
            "statement_import_batches",
            "id,source_file_path,source_file_hash,source_hash_verification_status",
        ),
        ("statement_import_source_evidence", "id,evidence_path,source_content_hash"),
        ("pdf_statement_import_runs", "id,source_pdf_path,import_batch_public_id"),
        ("reconciliation_structured_evidence", "id,source_path"),
    ):
        for row in _rows(conn, table, columns, limits["max_references"], check):
            path_col = (
                "evidence_path"
                if table == "statement_import_source_evidence"
                else (
                    "source_pdf_path"
                    if table == "pdf_statement_import_runs"
                    else (
                        "source_path"
                        if table == "reconciliation_structured_evidence"
                        else "source_file_path"
                    )
                )
            )
            hash_col = (
                "source_content_hash"
                if table == "statement_import_source_evidence"
                else "source_file_hash"
            )
            digest = row[hash_col] if hash_col in row.keys() else None
            if (
                table == "statement_import_batches"
                and row["source_hash_verification_status"] != "verified_from_bytes"
            ):
                digest = None
            claim(table, row["id"], path_col, path=row[path_col], digest=digest)

    _collect_json_references(conn, intake, public_ids, set(by_id), claim, limits, check)

    members: dict[str, tuple[str, int | None, str | None]] = {}
    resolved: list[dict[str, Any]] = []
    path_hashes: dict[str, set[str]] = {}
    path_sizes: dict[str, set[int]] = {}
    for _table, _pk, _column, _aid, path, digest, size, _alias in claims:
        if path is not None and digest is not None:
            path_hashes.setdefault(path, set()).add(digest)
        if path is not None and size is not None:
            path_sizes.setdefault(path, set()).add(size)
    for table, pk, column, aid, path, digest, size, alias in claims:
        check()
        if aid is not None:
            recorded = by_id[aid]
            canonical_path = str(recorded["file_path"])
            if (
                path is not None
                and path != canonical_path
                and (not alias or path not in source_aliases[aid])
            ):
                raise BundleError("Attachment path binding differs")
            candidate_ids = [aid]
        else:
            assert path is not None
            candidate_ids = by_path.get(path, [])
        recorded_hashes = {value for candidate in candidate_ids for value in hashes[candidate]}
        if digest is not None:
            recorded_hashes.add(digest)
        if path is not None and (aid is None or path == by_id[aid]["file_path"]):
            recorded_hashes.update(path_hashes.get(path, set()))
        if len(recorded_hashes) != 1:
            raise BundleError("Referenced original has no unique authoritative hash")
        expected_hash = next(iter(recorded_hashes))
        if candidate_ids:
            for candidate in candidate_ids:
                if hashes[candidate] and hashes[candidate] != {expected_hash}:
                    raise BundleError("Attachment declarations conflict")
            canonical_path = str(by_id[candidate_ids[0]]["file_path"])
        else:
            canonical_path = path or ""
        member = _member_from_path(canonical_path, trusted_attachment_root, expected_hash)
        if aid is None and path != canonical_path:
            raise BundleError("Unbound source alias is unsupported")
        if aid is not None and table == "local_attachment_source" and path != canonical_path:
            raise BundleError("Local source copy is not the canonical member")
        expected_sizes = {value for candidate in candidate_ids for value in sizes[candidate]}
        if size is not None:
            expected_sizes.add(size)
        if path is not None and (aid is None or path == by_id[aid]["file_path"]):
            expected_sizes.update(path_sizes.get(path, set()))
        if len(expected_sizes) > 1:
            raise BundleError("Attachment evidence sizes conflict")
        expected_size = next(iter(expected_sizes)) if expected_sizes else None
        extension = member.rsplit(".", 1)[-1]
        for candidate in candidate_ids:
            mime = by_id[candidate]["mime_type"]
            if (
                mime is not None
                and mime
                != {"jpg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}[extension]
            ):
                raise BundleError("Attachment MIME evidence conflicts")
        prior = members.get(member)
        value = (
            expected_hash,
            expected_size,
            {"jpg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}[extension],
        )
        if prior is not None and prior != value:
            raise BundleError("Member evidence conflicts")
        members[member] = value
        resolved.append(
            {
                "table": table,
                "primary_key": pk,
                "column": column,
                "member": member,
                "expected_sha256": expected_hash,
                "expected_size": expected_size,
            }
        )
    if len(members) > limits["max_attachment_members"]:
        raise BundleError("Attachment member budget exceeded")
    payloads = sorted(canonical_json(fact) for fact in resolved)
    digest_hash = hashlib.sha256()
    for payload in payloads:
        digest_hash.update(payload + b"\n")
    return Inventory(members, tuple(resolved), digest_hash.hexdigest())


def _load_json(value: Any, *, required: type = list) -> Any:
    if type(value) is not str or len(value.encode("utf-8")) > BUNDLE_LIMITS["max_manifest_bytes"]:
        raise BundleError("Registered source JSON is invalid")
    try:
        decoded = json.loads(value, object_pairs_hook=_pairs)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise BundleError("Registered source JSON is invalid") from exc
    if type(decoded) is not required:
        raise BundleError("Registered source JSON has unsupported shape")
    return decoded


def _collect_json_references(
    conn: sqlite3.Connection,
    intake: dict[int, sqlite3.Row],
    public_ids: dict[str, int],
    attachment_ids: set[int],
    claim: Callable[..., None],
    limits: dict[str, int],
    check: Callable[[], None],
) -> None:
    by_public = {row["public_id"]: row for row in intake.values()}
    for row in _rows(
        conn, "statement_transactions", "id,raw_row_payload_json", limits["max_references"], check
    ):
        if row["raw_row_payload_json"] is None:
            continue
        payload = _load_json(row["raw_row_payload_json"], required=dict)
        if payload.get("evidence_contract_version") == "pdf-row-evidence-v2":
            claim(
                "statement_transactions",
                row["id"],
                "raw_row_payload_json.attachment_path",
                path=payload.get("attachment_path"),
                digest=payload.get("source_content_hash"),
            )
        elif payload.get("attachment_path") or payload.get("source_file_path"):
            raise BundleError("Unknown statement row file reference")
    for row in _rows(
        conn, "correction_targets", "target_id,source_json", limits["max_references"], check
    ):
        source = _load_json(row["source_json"], required=dict)
        if source.get("schema") != "correction-original-d2-source-v1":
            raise BundleError("Unknown correction source version")
        original = by_public.get(source.get("raw_intake_id"))
        if (
            original is None
            or source.get("attachment_path") != original["attachment_path"]
            or source.get("attachment_hash") != original["attachment_hash"]
        ):
            raise BundleError("Correction original source differs from intake")
        claim(
            "correction_targets",
            row["target_id"],
            "source_json.attachment_path",
            aid=original["attachment_id"],
            path=source.get("attachment_path"),
            digest=source.get("attachment_hash"),
            alias=True,
        )
    for row in _rows(
        conn,
        "receipt_finalization_audit",
        "finalization_id,source_attachment_refs_json,evidence_refs_json",
        limits["max_references"],
        check,
    ):
        if _load_json(row["source_attachment_refs_json"]) != []:
            raise BundleError("Unknown finalization attachment-reference format")
        _collect_reference_tokens(
            row["evidence_refs_json"],
            "receipt_finalization_audit",
            row["finalization_id"],
            public_ids,
            attachment_ids,
            claim,
        )
    for table, pk, column in (
        ("financial_audit_events", "event_public_id", "source_evidence_refs_json"),
        ("receipt_finalization_authorizations", "authorization_id", "source_evidence_refs_json"),
        ("authoritative_calculation_snapshots", "snapshot_public_id", "source_references_json"),
    ):
        extra = ",event_type,event_payload_json" if table == "financial_audit_events" else ""
        for row in _rows(conn, table, f"{pk},{column}{extra}", limits["max_references"], check):
            _collect_reference_tokens(
                row[column], table, row[pk], public_ids, attachment_ids, claim
            )
            if (
                table == "financial_audit_events"
                and row["event_type"] == "statement_import_source_evidence_observed"
            ):
                payload = _load_json(row["event_payload_json"], required=dict)
                state = payload.get("new_state", payload)
                if type(state) is not dict:
                    raise BundleError("Statement audit source observation is invalid")
                path = state.get("evidence_path")
                digest = state.get("source_content_hash")
                if path is None or digest is None:
                    raise BundleError("Statement audit source observation is incomplete")
                claim(table, row[pk], "event_payload_json.evidence_path", path=path, digest=digest)


def _collect_reference_tokens(
    value: Any,
    table: str,
    pk: Any,
    public_ids: dict[str, int],
    attachment_ids: set[int],
    claim: Callable[..., None],
) -> None:
    if value is None:
        return
    tokens = _load_json(value)
    if any(type(token) is not str for token in tokens):
        raise BundleError("Registered source token is invalid")
    file_paths = [
        token.removeprefix("source-file-path:")
        for token in tokens
        if token.startswith("source-file-path:")
    ]
    file_hashes = [
        token.removeprefix("source-file-sha256:")
        for token in tokens
        if token.startswith("source-file-sha256:")
    ]
    if len(file_paths) > 1 or len(file_hashes) > 1:
        raise BundleError("Ambiguous source file audit token")
    if file_paths:
        claim(
            table,
            pk,
            f"{table}.source-file-path",
            path=file_paths[0],
            digest=file_hashes[0] if file_hashes else None,
        )
    elif file_hashes:
        # A hash-only token may describe a retained proof rather than a file.
        _require_hash(file_hashes[0])
    for token in tokens:
        if token.startswith("attachment:"):
            aid = public_ids.get(token[len("attachment:") :])
            if aid is None:
                raise BundleError("Audit attachment reference is missing")
            claim(table, pk, "attachment", aid=aid)
        elif token.startswith("attachment-id:"):
            raw_id = token[len("attachment-id:") :]
            if not re.fullmatch(r"[1-9][0-9]*", raw_id) or int(raw_id) not in attachment_ids:
                raise BundleError("Audit attachment ID is missing")
            claim(table, pk, "attachment-id", aid=int(raw_id))
        elif token.startswith("attachment-content-hash:"):
            _require_hash(token[len("attachment-content-hash:") :])
        elif token.startswith("attachment-"):
            raise BundleError("Unknown attachment source token")
        elif token.startswith("source-file-") and not token.startswith(
            ("source-file-path:", "source-file-sha256:")
        ):
            raise BundleError("Unknown source file token")


def _read_db_inventory(
    path: Path,
    root: Path,
    limits: dict[str, int],
    deadline: float,
    control_check: Callable[[], None],
) -> tuple[Inventory, dict[str, Any]]:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")

        def progress() -> int:
            try:
                _check(deadline, control_check)
            except BaseException:
                return 1
            return 0

        connection.set_progress_handler(progress, 1024)
        inventory = collect_references(connection, root, limits, deadline, control_check)
        _check(deadline, control_check)
        ledger = migration_ledger_rows(connection)
        if not ledger:
            raise BundleError("Migration ledger is empty")
        ledger_bytes = canonical_json(ledger)
        if len(ledger_bytes) > limits["max_manifest_bytes"]:
            raise BundleError("Migration metadata budget exceeded")
        metadata = {
            "application_id": connection.execute("PRAGMA application_id").fetchone()[0],
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "schema_fingerprint": schema_fingerprint(connection),
            "migration_ledger_sha256": _sha(ledger_bytes),
            "migration_ledger_count": len(ledger),
            "migration_latest_id": str(ledger[-1]["migration_id"]),
        }
        if metadata["application_id"] != FINANCE_APPLICATION_ID:
            raise BundleError("Snapshot application identity differs")
        return inventory, metadata
    finally:
        connection.close()


def _dir_role(path: Path, *, mode: int = 0o700) -> os.stat_result:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        named = path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != mode
        ):
            raise BundleError("Bundle directory role is unsafe")
        _reject_acl_grants(fd, path)
        return opened
    finally:
        os.close(fd)


def _file_role(
    path: Path, *, mode: int, max_bytes: int, deadline: float, control_check: Callable[[], None]
) -> tuple[os.stat_result, str, bytes]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        named = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_nlink != 1
            or before.st_size > max_bytes
        ):
            raise BundleError("Bundle file role is unsafe")
        _reject_acl_grants(fd, path)
        digest = hashlib.sha256()
        prefix = bytearray()
        total = 0
        while True:
            _check(deadline, control_check)
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise BundleError("Bundle file budget exceeded")
            if len(prefix) < 16:
                prefix.extend(chunk[: 16 - len(prefix)])
            digest.update(chunk)
        after = os.fstat(fd)
        final = path.lstat()
        if (
            total != before.st_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                before.st_mode,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_uid,
                after.st_mode,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
            != (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_ctime_ns)
        ):
            raise BundleError("Bundle file changed during verification")
        return after, digest.hexdigest(), bytes(prefix)
    finally:
        os.close(fd)


def _source_copy(
    root: publication.StorageRootHandle,
    member: str,
    digest: str,
    expected_size: int | None,
    mime: str | None,
    destination: Path,
    limits: dict[str, int],
    deadline: float,
    control_check: Callable[[], None],
) -> int:
    parts = member.split("/")
    shard_fd = publication.open_private_shard(root, parts[1])
    try:
        source_fd = os.open(parts[2], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=shard_fd)
        try:
            before = os.fstat(source_fd)
            named = os.stat(parts[2], dir_fd=shard_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_dev != root.device
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o400
                or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                or before.st_size > limits["max_attachment_bytes"]
                or (expected_size is not None and expected_size != before.st_size)
            ):
                raise BundleError("Original attachment role or size is invalid")
            _reject_acl_grants(source_fd, root.path / parts[1] / parts[2])
            target_fd = os.open(
                destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
            try:
                actual = hashlib.sha256()
                prefix = bytearray()
                copied = 0
                while True:
                    _check(deadline, control_check)
                    chunk = os.read(source_fd, _CHUNK)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > limits["max_attachment_bytes"]:
                        raise BundleError("Original attachment exceeds member limit")
                    if len(prefix) < 16:
                        prefix.extend(chunk[: 16 - len(prefix)])
                    actual.update(chunk)
                    remaining = memoryview(chunk)
                    while remaining:
                        _check(deadline, control_check)
                        wrote = os.write(target_fd, remaining)
                        if wrote <= 0:
                            raise BundleError("Bundle attachment write failed")
                        remaining = remaining[wrote:]
                if copied != before.st_size or actual.hexdigest() != digest:
                    raise BundleError("Original attachment bytes differ from evidence")
                detected = publication.detect_content_type(bytes(prefix), observed_size=copied)
                if detected.extension != "." + parts[2].rsplit(".", 1)[1] or (
                    mime is not None and detected.mime_type != mime
                ):
                    raise BundleError("Original attachment type differs from evidence")
                os.fsync(target_fd)
                os.fchmod(target_fd, 0o400)
                os.fsync(target_fd)
            finally:
                os.close(target_fd)
            after = os.fstat(source_fd)
            final = os.stat(parts[2], dir_fd=shard_fd, follow_symlinks=False)
            if (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_uid,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns) != (
                final.st_dev,
                final.st_ino,
                final.st_mtime_ns,
                final.st_ctime_ns,
            ):
                raise BundleError("Original attachment changed during copying")
            return copied
        finally:
            os.close(source_fd)
    finally:
        os.close(shard_fd)


def _identity_record(path: str, kind: str, info: os.stat_result) -> dict[str, Any]:
    return {
        "path": path,
        "kind": kind,
        "dev": str(info.st_dev),
        "ino": str(info.st_ino),
        "uid": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": info.st_nlink,
        "size": info.st_size,
        "mtime_ns": str(info.st_mtime_ns),
        "ctime_ns": str(info.st_ctime_ns),
    }


def tree_identity(
    stage: Path,
    members: dict[str, dict[str, Any]],
    limits: dict[str, int],
    deadline: float,
    control_check: Callable[[], None],
) -> str:
    """Reopen exact stage roles and calculate the Node-compatible final tree identity."""
    expected_files = set(members) | {"manifest.json"}
    expected_shards = {name.split("/")[1] for name in members if name.startswith("attachments/")}
    expected_dirs = {".", "db", "attachments"} | {"attachments/" + name for name in expected_shards}
    expected_children: dict[str, set[str]] = {name: set() for name in expected_dirs}
    for name in expected_dirs | expected_files:
        if name == ".":
            continue
        parent = "." if "/" not in name else name.rsplit("/", 1)[0]
        expected_children[parent].add(name.rsplit("/", 1)[-1])
    records: list[dict[str, Any]] = []
    for name in sorted(expected_dirs):
        _check(deadline, control_check)
        path = stage if name == "." else stage / name
        info = _dir_role(path)
        if set(os.listdir(path)) != expected_children[name]:
            raise BundleError("Bundle stage contains an unexpected role")
        records.append(_identity_record(name, "dir", info))
    for name in sorted(expected_files):
        _check(deadline, control_check)
        role = members.get(name, {}).get("role")
        mode = 0o400 if role == "attachment" else 0o600
        cap = (
            limits["max_attachment_bytes"]
            if role == "attachment"
            else (
                limits["max_manifest_bytes"]
                if name == "manifest.json"
                else limits["max_core_db_bytes"]
            )
        )
        info, digest, _ = _file_role(
            stage / name, mode=mode, max_bytes=cap, deadline=deadline, control_check=control_check
        )
        if name in members and (
            digest != members[name]["sha256"] or info.st_size != members[name]["bytes"]
        ):
            raise BundleError("Bundle member content changed")
        records.append(_identity_record(name, "file", info))
    return _sha(canonical_json(sorted(records, key=lambda record: record["path"])))


def installed_version_matches(request: CutRequest) -> None:
    if (
        request.core_api_contract_version != API_CONTRACT_VERSION
        or request.core_version != importlib.metadata.version("finance-core")
        or request.schema_sha256 != _migration_contract_digest()
    ):
        raise BundleError("Installed Core identity differs from fixed request")


def _member_map(value: Any) -> dict[str, dict[str, Any]]:
    if (
        type(value) is not list
        or not value
        or len(value) > BUNDLE_LIMITS["max_attachment_members"] + 1
    ):
        raise BundleError("Manifest member list is invalid")
    result: dict[str, dict[str, Any]] = {}
    for member in value:
        if type(member) is not dict or set(member) != {"path", "role", "bytes", "sha256"}:
            raise BundleError("Manifest member is invalid")
        path = member["path"]
        if type(path) is not str or (path != "db/core.sqlite" and not _MEMBER.fullmatch(path)):
            raise BundleError("Manifest member path is invalid")
        if member["role"] != ("database" if path == "db/core.sqlite" else "attachment"):
            raise BundleError("Manifest member role is invalid")
        if path in result or type(member["bytes"]) is not int or member["bytes"] <= 0:
            raise BundleError("Manifest member size or identity is invalid")
        _require_hash(member["sha256"])
        result[path] = member
    if list(result) != sorted(result) or "db/core.sqlite" not in result:
        raise BundleError("Manifest members are not canonically sorted")
    return result


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def stage_bundle(
    *,
    profile: ManagedStagingProfile,
    stage: Path,
    request: CutRequest,
    staged_disk: StagedDiskSnapshot,
    snapshot_recorded_at: str,
    deadline: float,
    control_check: Callable[[], None],
) -> dict[str, Any]:
    """Stage complete bytes after the source context has really closed."""
    installed_version_matches(request)
    limits = request.limits
    if limits != BUNDLE_LIMITS or staged_disk.output != str(stage / "db" / "core.sqlite"):
        raise BundleError("Bundle stage contract differs")
    _check(deadline, control_check)
    if (
        os.statvfs(stage).f_bavail * os.statvfs(stage).f_frsize
        < limits["max_stage_bytes"] + limits["min_free_bytes"]
    ):
        raise BundleError("Bundle free-space reserve is unavailable")
    inventory, database = _read_db_inventory(
        stage / "db" / "core.sqlite",
        profile.workspace / "attachments",
        limits,
        deadline,
        control_check,
    )
    if staged_disk.byte_length > limits["max_core_db_bytes"]:
        raise BundleError("Snapshot database exceeds bundle budget")
    root_stage = os.stat(stage, follow_symlinks=False)
    db_stage = os.stat(stage / "db", follow_symlinks=False)
    os.mkdir(stage / "attachments", 0o700)
    members: dict[str, dict[str, Any]] = {
        "db/core.sqlite": {
            "path": "db/core.sqlite",
            "role": "database",
            "bytes": staged_disk.byte_length,
            "sha256": staged_disk.sha256,
        }
    }
    aggregate = staged_disk.byte_length
    root: publication.StorageRootHandle | None = None
    locked = False
    try:
        if inventory.members:
            root = publication.open_storage_root(profile.workspace / "attachments")
            publication.acquire_storage_root_lock(
                root,
                deadline=deadline,
                clock=time.monotonic,
                deadline_error_factory=lambda _phase: BundleError(
                    "Attachment lock deadline expired"
                ),
            )
            locked = True
        shards: set[str] = set()
        for member, (digest, expected_size, mime) in sorted(inventory.members.items()):
            _check(deadline, control_check)
            assert root is not None
            shard = member.split("/")[1]
            if shard not in shards:
                os.mkdir(stage / "attachments" / shard, 0o700)
                shards.add(shard)
            size = _source_copy(
                root,
                member,
                digest,
                expected_size,
                mime,
                stage / member,
                limits,
                deadline,
                control_check,
            )
            aggregate += size
            if aggregate > limits["max_stage_bytes"] - limits["max_manifest_bytes"]:
                raise BundleError("Bundle stage budget exceeded")
            members[member] = {
                "path": member,
                "role": "attachment",
                "bytes": size,
                "sha256": digest,
            }
        for shard in sorted(shards):
            _sync_dir(stage / "attachments" / shard)
        _sync_dir(stage / "attachments")
    finally:
        if root is not None:
            if locked:
                publication.release_storage_root_lock(root)
            root.close()
    _check(deadline, control_check)
    _sync_dir(stage / "db")
    completed_at = _utc_now()
    database_entry = {
        "path": "db/core.sqlite",
        "sha256": staged_disk.sha256,
        "bytes": staged_disk.byte_length,
        "page_count": staged_disk.page_count,
        **database,
    }
    manifest = {
        "format": FORMAT,
        "scope": BUNDLE_SCOPE,
        "reference_registry_version": BUNDLE_REGISTRY_VERSION,
        "limits_version": BUNDLE_LIMITS_VERSION,
        "limits_sha256": request.limits_sha256,
        "cut_id": request.cut_id,
        "core_version": request.core_version,
        "core_api_contract_version": request.core_api_contract_version,
        "core_artifact_sha256": request.artifact_sha256,
        "migration_contract_sha256": request.schema_sha256,
        "snapshot_point": {
            "kind": "exclusive_core_commit_state",
            "cut_id": request.cut_id,
            "recorded_at": snapshot_recorded_at,
            "db_sha256": staged_disk.sha256,
        },
        "package_completed_at": completed_at,
        "database": database_entry,
        "members": [members[name] for name in sorted(members)],
        "reference_count": len(inventory.facts),
        "reference_sha256": inventory.digest,
        "member_count": len(members),
        "member_bytes": aggregate,
    }
    encoded = canonical_json(manifest)
    if (
        len(encoded) > limits["max_manifest_bytes"]
        or aggregate + len(encoded) > limits["max_stage_bytes"]
    ):
        raise BundleError("Bundle manifest or total bytes exceed limit")
    fd = os.open(
        stage / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    try:
        remaining = memoryview(encoded)
        while remaining:
            _check(deadline, control_check)
            written = os.write(fd, remaining)
            if written <= 0:
                raise BundleError("Manifest write failed")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_dir(stage)
    _check(deadline, control_check)
    return {
        "bundle_stage_dev": str(root_stage.st_dev),
        "bundle_stage_ino": str(root_stage.st_ino),
        "db_stage_dev": str(db_stage.st_dev),
        "db_stage_ino": str(db_stage.st_ino),
        "db_output_dev": str(staged_disk.output_dev),
        "db_output_ino": str(staged_disk.output_ino),
        "db_bytes": staged_disk.byte_length,
        "db_sha256": staged_disk.sha256,
        "db_page_count": staged_disk.page_count,
        "manifest_sha256": _sha(encoded),
        "manifest_bytes": len(encoded),
        "member_count": len(members),
        "member_bytes": aggregate,
        "reference_count": len(inventory.facts),
        "reference_sha256": inventory.digest,
        "snapshot_recorded_at": snapshot_recorded_at,
        "package_completed_at": completed_at,
        "schema_fingerprint": database["schema_fingerprint"],
        "migration_ledger_sha256": database["migration_ledger_sha256"],
        "migration_ledger_count": database["migration_ledger_count"],
    }


def verify_bundle(
    *, stage: Path, request: CutRequest, deadline: float, control_check: Callable[[], None]
) -> dict[str, Any]:
    """Fresh reader independently derives database references and package truth."""
    installed_version_matches(request)
    if request.staged is None or request.limits != BUNDLE_LIMITS:
        raise BundleError("Staged bundle evidence is missing")
    staged = request.staged
    _check(deadline, control_check)
    stage_info = _dir_role(stage)
    db_dir_info = _dir_role(stage / "db")
    if (str(stage_info.st_dev), str(stage_info.st_ino)) != (
        staged["bundle_stage_dev"],
        staged["bundle_stage_ino"],
    ):
        raise BundleError("Bundle root identity differs")
    if (str(db_dir_info.st_dev), str(db_dir_info.st_ino)) != (
        staged["db_stage_dev"],
        staged["db_stage_ino"],
    ):
        raise BundleError("Bundle DB directory identity differs")
    manifest_info, manifest_hash, _ = _file_role(
        stage / "manifest.json",
        mode=0o600,
        max_bytes=request.limits["max_manifest_bytes"],
        deadline=deadline,
        control_check=control_check,
    )
    if (
        manifest_hash != staged["manifest_sha256"]
        or manifest_info.st_size != staged["manifest_bytes"]
    ):
        raise BundleError("Manifest bytes differ from staged receipt")
    encoded = (stage / "manifest.json").read_bytes()
    if len(encoded) != manifest_info.st_size or _sha(encoded) != manifest_hash:
        raise BundleError("Manifest changed between reads")
    try:
        manifest = json.loads(encoded.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise BundleError("Manifest JSON is invalid") from exc
    if (
        canonical_json(manifest) != encoded
        or type(manifest) is not dict
        or set(manifest)
        != {
            "format",
            "scope",
            "reference_registry_version",
            "limits_version",
            "limits_sha256",
            "cut_id",
            "core_version",
            "core_api_contract_version",
            "core_artifact_sha256",
            "migration_contract_sha256",
            "snapshot_point",
            "package_completed_at",
            "database",
            "members",
            "reference_count",
            "reference_sha256",
            "member_count",
            "member_bytes",
        }
    ):
        raise BundleError("Manifest contract is invalid")
    if any(
        manifest[key] != expected
        for key, expected in (
            ("format", FORMAT),
            ("scope", BUNDLE_SCOPE),
            ("reference_registry_version", BUNDLE_REGISTRY_VERSION),
            ("limits_version", BUNDLE_LIMITS_VERSION),
            ("limits_sha256", request.limits_sha256),
            ("cut_id", request.cut_id),
            ("core_version", request.core_version),
            ("core_api_contract_version", request.core_api_contract_version),
            ("core_artifact_sha256", request.artifact_sha256),
            ("migration_contract_sha256", request.schema_sha256),
        )
    ):
        raise BundleError("Manifest trusted identity differs")
    point = manifest["snapshot_point"]
    if (
        type(point) is not dict
        or set(point) != {"kind", "cut_id", "recorded_at", "db_sha256"}
        or point
        != {
            "kind": "exclusive_core_commit_state",
            "cut_id": request.cut_id,
            "recorded_at": staged["snapshot_recorded_at"],
            "db_sha256": staged["db_sha256"],
        }
        or manifest["package_completed_at"] != staged["package_completed_at"]
    ):
        raise BundleError("Manifest snapshot point differs")
    members = _member_map(manifest["members"])
    database_member = members["db/core.sqlite"]
    if (
        database_member["bytes"] != staged["db_bytes"]
        or database_member["sha256"] != staged["db_sha256"]
    ):
        raise BundleError("Manifest database differs")
    inventory, database = _read_db_inventory(
        stage / "db" / "core.sqlite",
        _trusted_attachment_root_from_manifest_stage(stage),
        request.limits,
        deadline,
        control_check,
    )
    actual_members = set(inventory.members) | {"db/core.sqlite"}
    if set(members) != actual_members:
        raise BundleError("Manifest omitted or added a referenced attachment")
    if (
        len(inventory.facts) != manifest["reference_count"]
        or inventory.digest != manifest["reference_sha256"]
    ):
        raise BundleError("Manifest reference inventory differs")
    if (
        len(members) != manifest["member_count"]
        or sum(member["bytes"] for member in members.values()) != manifest["member_bytes"]
    ):
        raise BundleError("Manifest member accounting differs")
    db_entry = manifest["database"]
    if type(db_entry) is not dict or db_entry != {
        "path": "db/core.sqlite",
        "sha256": staged["db_sha256"],
        "bytes": staged["db_bytes"],
        "page_count": staged["db_page_count"],
        **database,
    }:
        raise BundleError("Manifest SQLite metadata differs")
    for member, (digest, expected_size, mime) in inventory.members.items():
        claimed = members[member]
        if claimed["sha256"] != digest or (
            expected_size is not None and claimed["bytes"] != expected_size
        ):
            raise BundleError("Manifest attachment evidence differs")
        info, content_hash, prefix = _file_role(
            stage / member,
            mode=0o400,
            max_bytes=request.limits["max_attachment_bytes"],
            deadline=deadline,
            control_check=control_check,
        )
        if info.st_size != claimed["bytes"] or content_hash != digest:
            raise BundleError("Copied attachment differs from evidence")
        detected = publication.detect_content_type(prefix, observed_size=info.st_size)
        if detected.extension != "." + member.rsplit(".", 1)[1] or (
            mime is not None and detected.mime_type != mime
        ):
            raise BundleError("Copied attachment content type differs")
    tree = tree_identity(stage, members, request.limits, deadline, control_check)
    for key, actual in (
        ("manifest_sha256", manifest_hash),
        ("manifest_bytes", manifest_info.st_size),
        ("member_count", len(members)),
        ("member_bytes", manifest["member_bytes"]),
        ("reference_count", len(inventory.facts)),
        ("reference_sha256", inventory.digest),
        ("schema_fingerprint", database["schema_fingerprint"]),
        ("migration_ledger_sha256", database["migration_ledger_sha256"]),
        ("migration_ledger_count", database["migration_ledger_count"]),
    ):
        if staged[key] != actual:
            raise BundleError("Staged evidence differs from independent readback")
    return {
        **staged,
        "schema_object_count": _schema_object_count(stage / "db" / "core.sqlite"),
        "journal_mode": "delete",
        "tree_identity_sha256": tree,
    }


def _schema_object_count(path: Path) -> int:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        return int(conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0])
    finally:
        conn.close()


def _trusted_attachment_root_from_manifest_stage(stage: Path) -> Path:
    # FD6's validated role is profile/work/core-cut-<id>.  No manifest or row
    # supplies this root; the fixed enrolled profile layout does.
    return stage.parent.parent / "workspace" / "attachments"
