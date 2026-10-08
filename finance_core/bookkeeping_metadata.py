"""Versioned whole-expense metadata, separate from numerical receipt inputs."""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

from finance_core.calculation.authoritative_snapshot import canonical_json_text

BOOKKEEPING_METADATA_VERSION = "application_bookkeeping_metadata_v1"


@dataclass(frozen=True)
class ReceiptBookkeepingMetadata:
    description: str | None
    category: str | None
    version: str = BOOKKEEPING_METADATA_VERSION

    def __post_init__(self) -> None:
        if self.version != BOOKKEEPING_METADATA_VERSION:
            raise ValueError("Unsupported independent bookkeeping metadata contract")
        for value in (self.description, self.category):
            if value is not None and (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 1024
                or unicodedata.normalize("NFC", value) != value
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Bookkeeping metadata requires full bounded canonical text")

    def as_payload(self) -> dict[str, Any]:
        return {"version": self.version, "description": self.description, "category": self.category}


def metadata_from_payload(payload: object) -> ReceiptBookkeepingMetadata | None:
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {"version", "description", "category"}:
        raise ValueError("Malformed bookkeeping metadata extension")
    return ReceiptBookkeepingMetadata(
        description=payload["description"], category=payload["category"], version=payload["version"]
    )


def metadata_hash(metadata: ReceiptBookkeepingMetadata) -> str:
    return hashlib.sha256(canonical_json_text(metadata.as_payload()).encode()).hexdigest()


def read_receipt_bookkeeping_metadata(
    conn: sqlite3.Connection, receipt_public_id: str
) -> ReceiptBookkeepingMetadata | None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(receipts)")}
    if "bookkeeping_metadata_version" not in columns:
        return None
    row = conn.execute(
        "SELECT id,description,category,bookkeeping_metadata_version FROM receipts WHERE "
        "public_id=?",
        (receipt_public_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Receipt metadata subject is absent")
    seal = conn.execute(
        "SELECT * FROM application_amendment_receipt_metadata WHERE receipt_id=?", (row["id"],)
    ).fetchone()
    if row["bookkeeping_metadata_version"] is None:
        if row["description"] is not None or row["category"] is not None or seal is not None:
            raise ValueError("Unversioned receipt metadata cannot become authorized")
        return None
    metadata = ReceiptBookkeepingMetadata(
        row["description"], row["category"], row["bookkeeping_metadata_version"]
    )
    if (
        seal is None
        or seal["metadata_version"] != metadata.version
        or seal["description"] != metadata.description
        or seal["category"] != metadata.category
        or seal["material_hash"] != metadata_hash(metadata)
    ):
        raise ValueError("Receipt bookkeeping metadata seal does not verify")
    return metadata


def build_application_receipt_projection(
    *, bookkeeping_metadata: ReceiptBookkeepingMetadata | None = None, **kwargs: Any
) -> dict[str, Any]:
    from finance_core.receipt_finalization.d2_conditional import build_d2_receipt_projection

    projection = build_d2_receipt_projection(**kwargs)
    if bookkeeping_metadata is not None:
        return {
            **projection,
            "bookkeeping_metadata": bookkeeping_metadata.as_payload(),
            "description": bookkeeping_metadata.description,
            "category": bookkeeping_metadata.category,
        }
    return projection
