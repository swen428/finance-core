"""B5.1a deterministic participant bootstrap (staging-only).

Bootstraps participant reference data from a validated manifest into a
trusted staging database.  Uses a single explicit transaction, is
idempotent for exact replay, and fails closed with a typed conflict for
same-ID-different-content.  Never reads or executes ``database/seed/**``,
never creates receipt lifecycle data, transactions, or settlement rows.

See ``docs/design/b5_1a_receipt_staging_runner_foundation_v1.md``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from finance_core.receipt_staging_runner.models import (
    ParticipantBootstrapResult,
    ParticipantDefinition,
    RunnerInputManifest,
    RunnerParticipantError,
)
from finance_core.staging_guard import require_staging_database

# ---------------------------------------------------------------------------
# Bootstrap hash derivation
# ---------------------------------------------------------------------------


def _canonical_participant_payload(p: ParticipantDefinition) -> dict[str, object]:
    """Stable canonical dict for one participant (used for hashing)."""
    return {
        "public_id": p.public_id,
        "display_name": p.display_name,
        "is_self": p.is_self,
        "is_active": p.is_active,
        "aliases": list(p.aliases),
        "notes": p.notes,
    }


def derive_bootstrap_hash(
    participants: tuple[ParticipantDefinition, ...],
    manifest_hash: str,
) -> str:
    """Deterministic hash of the complete participant set + manifest identity."""
    payload = {
        "manifest_hash": manifest_hash,
        "participants": [_canonical_participant_payload(p) for p in participants],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Participant bootstrap
# ---------------------------------------------------------------------------


def bootstrap_participants(
    conn: sqlite3.Connection,
    manifest: RunnerInputManifest,
) -> ParticipantBootstrapResult:
    """Deterministically bootstrap participant reference data from the manifest.

    Rules:
    - Calls ``require_staging_database`` before any access.
    - Uses one explicit ``BEGIN IMMEDIATE`` transaction.
    - First execution inserts all participants.
    - Exact replay (same public_id + same material) returns success.
    - Same public_id with any material difference raises typed conflict.
    - Does not use INSERT OR REPLACE.
    - Does not create transactions, receipts, proposals, calculations,
      authorizations, settlements, or reconciliation data.
    - On failure, the transaction is fully rolled back.

    Parameters
    ----------
    conn:
        Open staging database connection.
    manifest:
        Validated runner input manifest with participant definitions.

    Returns
    -------
    ParticipantBootstrapResult
        Stable participant identities, manifest hash, and bootstrap hash.

    Raises
    ------
    RunnerParticipantError
        On conflict, rollback failure, or staging guard rejection.
    """
    require_staging_database(conn)

    participants = manifest.participants
    manifest_hash = manifest.manifest_sha256
    bootstrap_hash = derive_bootstrap_hash(participants, manifest_hash)

    conn.execute("BEGIN IMMEDIATE")
    try:
        replayed = _bootstrap_in_transaction(conn, participants)
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise

    return ParticipantBootstrapResult(
        participant_public_ids=tuple(p.public_id for p in participants),
        manifest_hash=manifest_hash,
        bootstrap_hash=bootstrap_hash,
        replayed=replayed,
    )


def _bootstrap_in_transaction(
    conn: sqlite3.Connection,
    participants: tuple[ParticipantDefinition, ...],
) -> bool:
    """Insert or verify participants inside an open transaction.

    Returns True if this was a replay (all participants already existed
    with identical material), False if fresh inserts were performed.
    """
    any_inserted = False
    any_existing = False

    for p in participants:
        existing = conn.execute(
            "SELECT display_name, is_self, is_active, aliases, notes "
            "FROM participants WHERE public_id = ?",
            (p.public_id,),
        ).fetchone()

        aliases_json = json.dumps(list(p.aliases), separators=(",", ":")) if p.aliases else None

        if existing is None:
            conn.execute(
                "INSERT INTO participants "
                "(public_id, display_name, is_self, is_active, aliases, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    p.public_id,
                    p.display_name,
                    int(p.is_self),
                    int(p.is_active),
                    aliases_json,
                    p.notes or None,
                ),
            )
            any_inserted = True
        else:
            # Verify exact material match.
            durable_display = str(existing[0])
            durable_is_self = bool(existing[1])
            durable_is_active = bool(existing[2])
            durable_aliases = existing[3]  # TEXT or None
            durable_notes = str(existing[4]) if existing[4] is not None else ""

            conflicts: list[str] = []
            if durable_display != p.display_name:
                conflicts.append("display_name")
            if durable_is_self != p.is_self:
                conflicts.append("is_self")
            if durable_is_active != p.is_active:
                conflicts.append("is_active")
            # Compare aliases: None means empty, otherwise exact JSON string.
            expected_aliases = aliases_json
            if durable_aliases != expected_aliases:
                conflicts.append("aliases")
            expected_notes = p.notes or ""
            if durable_notes != expected_notes:
                conflicts.append("notes")

            if conflicts:
                raise RunnerParticipantError(
                    f"Participant {p.public_id!r} already exists with different "
                    f"material on: {', '.join(sorted(conflicts))}. "
                    "Same public_id with different content is a typed conflict."
                )
            any_existing = True

    if any_inserted and any_existing:
        # Mixed state: some new, some existing — this is a partial replay
        # which is acceptable (e.g., adding a participant to an existing set).
        # The transaction is still atomic.
        pass

    return any_existing and not any_inserted


# ---------------------------------------------------------------------------
# SELECT-only participant projection (S4 bridge support)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParticipantRead:
    """Bounded participant reference projection for structural guards only.

    Carries identity and flags only: no amounts, no allocations, no derived
    financial semantics.
    """

    public_id: str
    is_self: bool
    is_active: bool


def read_participants(conn: sqlite3.Connection) -> tuple[ParticipantRead, ...]:
    """Return the durable participant reference projection.

    Added for the S4 OpenClaw staging bridge under Owner's 2026-08-05 reader
    authorization.  Strictly SELECT-only: no writes, no DDL, no implicit
    repair, no caching, and no monetary or allocation derivation.  Rows are
    returned in deterministic ``public_id`` order; an empty participant
    table yields an empty tuple.
    """
    require_staging_database(conn)
    rows = conn.execute(
        "SELECT public_id, is_self, is_active FROM participants ORDER BY public_id ASC"
    ).fetchall()
    return tuple(
        ParticipantRead(
            public_id=str(row[0]),
            is_self=bool(row[1]),
            is_active=bool(row[2]),
        )
        for row in rows
    )


__all__ = [
    "ParticipantRead",
    "bootstrap_participants",
    "derive_bootstrap_hash",
    "read_participants",
]
