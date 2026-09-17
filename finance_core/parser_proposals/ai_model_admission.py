"""Append-only intake-level denials for Nomi model admission v2."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from typing import Any

from finance_core.sqlite_connection import ForeignKeysDisabledError, require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database

ADMISSION_DECISION_TYPE = "model_denied"
ADMISSION_REASON_CODE = "configuration_not_accepted"
MAX_CONFIG_EVIDENCE_BYTES = 32_768
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class AiModelAdmissionError(RuntimeError):
    """A model-admission decision cannot be persisted or verified."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AiModelAdmissionError(
            "AI_MODEL_CONFIG_REFUSED", "The Agent config evidence is invalid."
        ) from exc
    if len(encoded) > MAX_CONFIG_EVIDENCE_BYTES:
        raise AiModelAdmissionError(
            "AI_MODEL_CONFIG_REFUSED", "The Agent config evidence is oversized."
        )
    return encoded


def config_evidence_sha256(config_evidence: Mapping[str, Any]) -> str:
    """Hash bounded evidence while retaining none of its content."""
    return hashlib.sha256(_canonical_json_bytes(config_evidence)).hexdigest()


def _material_hash(*, intake_public_id: str, evidence_hash: str) -> str:
    material = _canonical_json_bytes(
        {
            "schema_version": "finance-ai-model-admission-decision-v2",
            "intake_public_id": intake_public_id,
            "config_evidence_sha256": evidence_hash,
            "decision_type": ADMISSION_DECISION_TYPE,
            "safe_reason_code": ADMISSION_REASON_CODE,
        }
    )
    digest = hashlib.sha256()
    digest.update(b"finance-ai-model-admission-decision-v2\0")
    digest.update(material)
    return digest.hexdigest()


def verify_ai_model_admission_decision(
    decision: Mapping[str, Any], *, intake_public_id: str
) -> dict[str, Any]:
    """Verify the deterministic identity and closed denial vocabulary."""
    try:
        evidence_hash = decision["config_evidence_sha256"]
        if not isinstance(evidence_hash, str) or _HASH_RE.fullmatch(evidence_hash) is None:
            raise ValueError("invalid evidence hash")
        material_hash = _material_hash(
            intake_public_id=intake_public_id, evidence_hash=evidence_hash
        )
        if (
            decision["decision_material_hash"] != material_hash
            or decision["decision_public_id"] != f"aimd_{material_hash}"
            or decision["decision_type"] != ADMISSION_DECISION_TYPE
            or decision["safe_reason_code"] != ADMISSION_REASON_CODE
            or isinstance(decision["decided_at_ms"], bool)
            or not isinstance(decision["decided_at_ms"], int)
            or decision["decided_at_ms"] < 0
        ):
            raise ValueError("decision material mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise AiModelAdmissionError(
            "AI_MODEL_COMPATIBILITY_CONFLICT",
            "Persisted AI model admission decision does not verify.",
        ) from exc
    return dict(decision)


def _record_ai_model_admission_denial_v2(
    conn: sqlite3.Connection,
    *,
    intake_public_id: str,
    config_evidence: Mapping[str, Any],
    now_ms: int | None = None,
    eligibility_guard: Callable[[sqlite3.Connection], object] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Append one terminal config denial, mutually exclusive with an attempt."""
    require_staging_database(conn)
    try:
        require_foreign_keys_enabled(conn)
    except ForeignKeysDisabledError as exc:
        raise AiModelAdmissionError(
            "AI_MODEL_COMPATIBILITY_FK_REFUSED", "Foreign keys must be enabled."
        ) from exc
    if not isinstance(intake_public_id, str) or not intake_public_id:
        raise AiModelAdmissionError("INTAKE_NOT_FOUND", "Raw intake was not found.")
    decided_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if isinstance(decided_at_ms, bool) or not isinstance(decided_at_ms, int) or decided_at_ms < 0:
        raise AiModelAdmissionError(
            "AI_MODEL_COMPATIBILITY_ARGUMENTS_REFUSED", "now_ms is invalid."
        )
    evidence_hash = config_evidence_sha256(config_evidence)
    material_hash = _material_hash(intake_public_id=intake_public_id, evidence_hash=evidence_hash)
    public_id = f"aimd_{material_hash}"
    try:
        conn.execute("BEGIN IMMEDIATE")
        intake = conn.execute(
            "SELECT id FROM raw_intake_records WHERE public_id = ?", (intake_public_id,)
        ).fetchone()
        if intake is None:
            raise AiModelAdmissionError("INTAKE_NOT_FOUND", "Raw intake was not found.")
        if eligibility_guard is not None:
            eligibility_guard(conn)
        existing_attempt = conn.execute(
            "SELECT 1 FROM ai_fallback_attempts WHERE raw_intake_record_id = ?",
            (intake["id"],),
        ).fetchone()
        if existing_attempt is not None:
            raise AiModelAdmissionError(
                "AI_MODEL_COMPATIBILITY_CONFLICT",
                "Raw intake is already bound to an AI fallback attempt.",
            )
        existing = conn.execute(
            "SELECT * FROM ai_model_admission_decisions WHERE raw_intake_record_id = ?",
            (intake["id"],),
        ).fetchone()
        replay = existing is not None
        if existing is None:
            conn.execute(
                """
                INSERT INTO ai_model_admission_decisions (
                    decision_public_id, decision_material_hash, raw_intake_record_id,
                    config_evidence_sha256, decision_type, safe_reason_code, decided_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id,
                    material_hash,
                    intake["id"],
                    evidence_hash,
                    ADMISSION_DECISION_TYPE,
                    ADMISSION_REASON_CODE,
                    decided_at_ms,
                ),
            )
            persisted = conn.execute(
                "SELECT * FROM ai_model_admission_decisions WHERE decision_public_id = ?",
                (public_id,),
            ).fetchone()
        else:
            persisted = existing
        if persisted is None:
            raise AiModelAdmissionError(
                "AI_MODEL_COMPATIBILITY_INTERNAL", "Admission decision persistence failed."
            )
        verified = verify_ai_model_admission_decision(
            dict(persisted), intake_public_id=intake_public_id
        )
        if verified["decision_public_id"] != public_id:
            raise AiModelAdmissionError(
                "AI_MODEL_COMPATIBILITY_CONFLICT",
                "Raw intake is already bound to different admission evidence.",
            )
        conn.commit()
    except AiModelAdmissionError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise AiModelAdmissionError(
            "AI_MODEL_COMPATIBILITY_INTERNAL", "Admission decision persistence failed."
        ) from exc
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {
        "decision_public_id": public_id,
        "decision_type": ADMISSION_DECISION_TYPE,
        "safe_reason_code": ADMISSION_REASON_CODE,
        "config_evidence_sha256": evidence_hash,
    }, replay


def get_ai_model_admission_decision_v2(
    conn: sqlite3.Connection, *, intake_public_id: str
) -> dict[str, Any] | None:
    """Return a verified historical denial without consulting current config."""
    require_staging_database(conn)
    row = conn.execute(
        """
        SELECT decision.*
        FROM raw_intake_records AS intake
        JOIN ai_model_admission_decisions AS decision
          ON decision.raw_intake_record_id = intake.id
        WHERE intake.public_id = ?
        """,
        (intake_public_id,),
    ).fetchone()
    if row is None:
        return None
    return verify_ai_model_admission_decision(dict(row), intake_public_id=intake_public_id)


__all__ = [
    "ADMISSION_DECISION_TYPE",
    "ADMISSION_REASON_CODE",
    "AiModelAdmissionError",
    "config_evidence_sha256",
    "get_ai_model_admission_decision_v2",
    "verify_ai_model_admission_decision",
]
