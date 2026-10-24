"""Fixed read-only legacy export; retained cards never become new authority.

This adapter is deliberately a historical compatibility boundary. It uses the
old owners and the composition's retained trust roots once, in one snapshot.
The exported JSON is evidence material, not a signed approval or a live command.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import math
import sqlite3
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_core.application.ai_invocation import AiInvocationService
from finance_core.application.amendment import EDITABLE_FIELDS, AmendmentService
from finance_core.application.amendment_contract import amendment_review_sha256
from finance_core.application.independent_corrections import IndependentCorrectionService
from finance_core.application.posting import PostingService
from finance_core.application.posting_contract import posting_review_sha256
from finance_core.application.review import (
    classify_payload,
    prepare_proposal_review,
    project_proposal_review,
    review_snapshot,
)
from finance_core.bookkeeping_metadata import (
    ReceiptBookkeepingMetadata,
    build_application_receipt_projection,
)
from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text,
    canonical_json_value,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.financial_audit import verify_financial_audit_chain
from finance_core.instance_admission import require_finance_read_connection
from finance_core.parser_proposals import decision_owner
from finance_core.parser_proposals.independent_ai_revision import ordered_revision_proof
from finance_core.parser_proposals.independent_ai_v2 import _canonical_fields
from finance_core.parser_proposals.receipt_facts_conversion import (
    resolve_independent_receipt_payload_fields,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    build_migration_manifest,
    canonical_schema_payload,
    migration_ledger_rows,
)

LEGACY_BASELINE = "d8186832f10a4df0af401a7052d0f41980c535e2"
EXPORT_SCHEMA = "finance-lifecycle-legacy-verified-export-v1"
LEGACY_MIGRATION_PATHS = TEMP_DB_MIGRATION_PATHS[:60]
_ISSUER = object()
_issued: dict[int, weakref.ReferenceType[VerifiedLegacyExport]] = {}


class LegacyExportError(ValueError):
    """The complete supported legacy archive could not be authenticated."""


def _wire(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _snapshot_hash(value: Any) -> str:
    return hashlib.sha256(_wire(value).encode()).hexdigest()


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json_text(value).encode()).hexdigest()


@dataclass(frozen=True, init=False, eq=False)
class VerifiedLegacyExport:
    """In-process receipt issued only after fixed old-owner verification.

    All nested material is canonical text or immutable bytes. Construction or
    reloading a JSON document does not grant this process-local provenance.
    Recovery must authenticate its own bundle before reconstructing new facts.
    """

    schema: str
    legacy_baseline: str
    legacy_namespace: str
    instance_id: str
    snapshot_sha256: str
    manifest_json: str
    archive_json: str
    originals: tuple[tuple[str, str, bytes], ...]
    signature: str
    signing_key_id: str

    def __init__(self, *, _issuer: object = None, **values: Any) -> None:
        if _issuer is not _ISSUER:
            raise LegacyExportError("A caller cannot construct verified legacy provenance")
        for field in dataclasses.fields(self):
            object.__setattr__(self, field.name, values[field.name])
        identity = id(self)
        _issued[identity] = weakref.ref(self, lambda _ref: _issued.pop(identity, None))


def require_verified_legacy_export(value: object) -> VerifiedLegacyExport:
    """Check issuer identity and retained material, never a caller verified flag."""
    if type(value) is not VerifiedLegacyExport:
        raise LegacyExportError("Legacy import requires an issued export receipt")
    reference = _issued.get(id(value))
    if reference is None or reference() is not value:
        raise LegacyExportError("Legacy export receipt has no issuing verifier")
    manifest = json.loads(value.manifest_json)
    if (
        value.schema != EXPORT_SCHEMA
        or value.legacy_baseline != LEGACY_BASELINE
        or manifest["schema"] != value.schema
        or manifest["legacy_namespace"] != value.legacy_namespace
        or manifest["instance_id"] != value.instance_id
        or _snapshot_hash(json.loads(value.archive_json)) != value.snapshot_sha256
        or manifest["snapshot_sha256"] != value.snapshot_sha256
        or manifest["originals"]
        != [{"path": p, "sha256": h, "size": len(b)} for p, h, b in value.originals]
        or any(hashlib.sha256(b).hexdigest() != h for _, h, b in value.originals)
    ):
        raise LegacyExportError("Issued legacy export material changed")
    return value


def _sql_value(value: Any) -> Any:
    if type(value) is bytes:
        return {"sqlite_blob_hex": value.hex()}
    if type(value) is float:
        if not math.isfinite(value):
            raise LegacyExportError("Nonfinite SQLite value cannot be archived")
        return {"sqlite_real_hex": value.hex()}
    if value is None or type(value) in {str, int}:
        return value
    raise LegacyExportError("Unsupported SQLite archive value")


def _archive(connection: sqlite3.Connection) -> dict[str, Any]:
    """Lossless logical snapshot of all retained rows and schema, including drafts."""
    schema = [
        list(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    ]
    tables = {}
    for kind, name, _table, _sql in schema:
        if kind != "table":
            continue
        escaped = name.replace('"', '""')
        cursor = connection.execute(f'SELECT * FROM "{escaped}"')
        columns = [column[0] for column in cursor.description]
        rows = [[_sql_value(value) for value in row] for row in cursor.fetchall()]
        tables[name] = {"columns": columns, "rows": sorted(rows, key=canonical_json_text)}
    return {"schema": schema, "tables": tables}


def _historical_review(connection, proposal_id, version, digest, parse_status):
    proposal = ParserProposalRepository(connection).get_by_public_id(proposal_id)
    if proposal is None:
        raise LegacyExportError("Historical card proposal is absent")
    revisions = ordered_revision_proof(connection, proposal).revisions
    if type(version) is not int or not 0 <= version < len(revisions):
        raise LegacyExportError("Historical card revision is absent")
    revision = revisions[version]
    expected_status = (
        "parsed_pending_confirmation" if version == 0 else "edited_pending_confirmation"
    )
    if digest != revision.content_hash or parse_status != expected_status:
        raise LegacyExportError("Historical card is not its actual pending revision")
    prepared = prepare_proposal_review(connection, proposal_id)
    payload = revision.payload
    classification, unknown = classify_payload(payload)
    account = payload.get("account", payload.get("account_id"))
    if account is not None:
        raise LegacyExportError("Legacy exporter supports unspecified account only")
    lineage = prepared.ai_lineage
    if lineage is not None and lineage.get("proposal_origin") == "independent_ai":
        ancestor_ids = set()
        repository = ParserProposalRepository(connection)
        ancestor = repository.get_lineage_row_by_id(proposal["id"])
        while ancestor is not None:
            if ancestor["id"] in ancestor_ids:
                raise LegacyExportError("Historical proposal ancestry cycles")
            ancestor_ids.add(ancestor["id"])
            parent_id = ancestor["parent_parser_output_id"]
            ancestor = None if parent_id is None else repository.get_lineage_row_by_id(parent_id)
        relevant = [
            identity
            for identity in lineage["attempt_ids"]
            if connection.execute(
                "SELECT child_parser_output_id FROM independent_ai_publications WHERE attempt_id=?",
                (identity,),
            ).fetchone()[0]
            in ancestor_ids
        ]
        if not relevant:
            lineage = None
        else:
            lineage = {**lineage, "attempt_ids": relevant}
    if lineage is not None:
        lineage = {
            **lineage,
            "ambiguity_flags": payload.get("ambiguity_flags", []),
            "requires_resolution": bool(payload.get("ambiguity_flags", []))
            and proposal["attachment_id"] is None,
        }
    historical = dataclasses.replace(
        prepared,
        payload=payload,
        version=version,
        content_hash=digest,
        ai_lineage=lineage,
        account=None,
        account_status="absent",
        account_truncated=False,
        classification=classification,
        classification_unknown=unknown,
        transaction_date=payload.get("transaction_date", payload.get("date")),
        parse_status=expected_status,
    )
    return proposal, payload, project_proposal_review(connection, historical), lineage


def _assessment(connection, proposal, payload, view, lineage, source):
    if lineage is None or lineage.get("proposal_origin") != "independent_ai":
        return None
    from finance_core.application.ai_invocation_contract import AI_INPUT_SCHEMA

    attempts = [
        json.loads(
            connection.execute(
                "SELECT material_json FROM independent_ai_attempts WHERE attempt_id=?", (identity,)
            ).fetchone()[0]
        )
        for identity in lineage["attempt_ids"]
    ]
    if not any(item["schema"] == AI_INPUT_SCHEMA for item in attempts):
        raise LegacyExportError("Legacy AI format requires its separate archive adapter")
    fields = _canonical_fields(payload, complete=True)
    before = _canonical_fields(attempts[0]["projection"]["current_values"], complete=False)
    blockers = []
    if (
        payload.get("intent") != "personal_expense_log"
        or payload.get("transaction_type") != "personal_expense"
        or any(
            payload.get(k) is not None
            for k in ("account", "account_id", "payer", "paid_by", "participants", "ledger")
        )
    ):
        blockers.append("unsupported_scope")
    warnings = sorted(set(payload.get("ambiguity_flags", [])))
    if proposal["confidence_score"] is None or float(proposal["confidence_score"]) < 0.8:
        warnings.append("historical_low_ocr_or_parser_confidence")
    return {
        "basis": "full_card_ai_expense_v1",
        "proposal_public_id": proposal["public_id"],
        "proposal_version": view["proposal_version"],
        "effective_content_hash": view["effective_content_hash"],
        "canonical_fields": fields,
        "hard_blockers": blockers,
        "review_warnings": sorted(set(warnings)),
        "source_and_interpretation_digest": _snapshot_hash(
            {"source": source, "attempts": attempts, "fields": fields}
        ),
        "assessment_policy_version": "finance-independent-ai-expense-assessment-v2",
        "change_summary": {
            "before": before,
            "after": fields,
            "changes": [
                {"field": field, "before": before.get(field), "after": fields[field]}
                for field in fields
                if before.get(field) != fields[field]
            ],
            "interaction_kind": attempts[0]["projection"].get("interaction_kind", "initial"),
        },
    }


class LegacyExportVerifier:
    """Trusted installation/recovery root; never decoded from an import payload."""

    def __init__(self, *, namespace: str, instance_id: str, key: bytes) -> None:
        if type(key) is not bytes or len(key) < 32 or not namespace or not instance_id:
            raise LegacyExportError("A separate fixed legacy export root is required")
        self.namespace, self.instance_id, self._key = namespace, instance_id, key
        self.key_id = hashlib.sha256(key).hexdigest()

    def _sign(self, domain: str, material: Any) -> str:
        return hmac.new(
            self._key, (domain + "\x00" + _wire(material)).encode(), hashlib.sha256
        ).hexdigest()

    def _verify(self, domain: str, material: Any, signature: str) -> None:
        if not isinstance(signature, str) or not hmac.compare_digest(
            self._sign(domain, material), signature
        ):
            raise LegacyExportError("Legacy evidence differs from the trusted export root")

    def verify_payload(self, material: Mapping[str, Any]) -> None:
        manifest = material["manifest"]
        if (
            material["signing_key_id"] != self.key_id
            or manifest["schema"] != EXPORT_SCHEMA
            or manifest["legacy_baseline"] != LEGACY_BASELINE
            or manifest["legacy_namespace"] != self.namespace
            or manifest["instance_id"] != self.instance_id
            or manifest["coverage"] != "supported_independent_lifecycle_complete"
            or manifest["archive_rights"] != "read_only_non_executable"
        ):
            raise LegacyExportError("Legacy archive identity or coverage differs")
        self._verify("export", manifest, material["signature"])
        if _snapshot_hash(json.loads(material["archive_json"])) != manifest["snapshot_sha256"]:
            raise LegacyExportError("Complete legacy archive differs from its sealed snapshot")
        originals = material["originals"]
        inventory = []
        for original in originals:
            raw = bytes.fromhex(original["data_hex"])
            if hashlib.sha256(raw).hexdigest() != original["sha256"]:
                raise LegacyExportError("Archived legacy original bytes changed")
            inventory.append(
                {"path": original["path"], "sha256": original["sha256"], "size": len(raw)}
            )
        if inventory != manifest["originals"]:
            raise LegacyExportError("Archived legacy original inventory changed")


class LegacyReadAdapter:
    """Fixed trusted composition for the supported old independent lifecycle.

    The supplied services are installation authority, never request DTOs. This
    cooperative Python boundary does not claim protection from hostile same-UID
    code. No callable provided by the export caller can mark evidence verified.
    """

    def __init__(
        self,
        *,
        posting: PostingService,
        amendment: AmendmentService,
        corrections: IndependentCorrectionService,
        attachment_root: Path | None = None,
        authority_tables: frozenset[str] = frozenset(),
        export_verifier: LegacyExportVerifier,
    ) -> None:
        if (
            type(posting) is not PostingService
            or type(amendment) is not AmendmentService
            or type(corrections) is not IndependentCorrectionService
            or posting._conn is not amendment._conn
            or posting._conn is not corrections._conn
            or corrections._posting is not posting
            or amendment._binding.binding != posting._binding
            or amendment._source_port is not posting._source_port
            or amendment._ai_lineage_authority is not posting._ai_lineage_authority
        ):
            raise LegacyExportError("Legacy exporter requires one fixed old-owner composition")
        self._posting = posting
        self._amendment = amendment
        self._corrections = corrections
        self._connection = posting._conn
        self._attachment_root = attachment_root
        if (
            type(export_verifier) is not LegacyExportVerifier
            or export_verifier.namespace != posting._binding.source_namespace
            or export_verifier.instance_id != posting._binding.instance_id
        ):
            raise LegacyExportError("Export signing root differs from fixed legacy identity")
        self._authority_tables = authority_tables
        self._export_verifier = export_verifier

    def _cards(self) -> list[dict[str, str]]:
        c = self._connection
        result = []
        for row in c.execute("SELECT * FROM application_amendment_reviews ORDER BY review_id"):
            card = self._amendment._read_review(row["review_id"])
            view = card["proposal_review"]
            proposal, payload, rebuilt, _lineage = _historical_review(
                c,
                view["proposal_public_id"],
                view["proposal_version"],
                view["effective_content_hash"],
                view["parse_status"],
            )
            source = vars(self._posting._source(view["intake_public_id"]))
            editable = {
                field: payload.get(
                    field, payload.get("date") if field == "transaction_date" else None
                )
                for field in sorted(EDITABLE_FIELDS)
            }
            if (
                card["source"] != source
                or view != rebuilt
                or card["editable_values"] != editable
                or json.loads(card["effective_payload_json"]) != payload
                or row["parser_output_id"] != proposal["id"]
                or row["intake_public_id"] != view["intake_public_id"]
                or row["source_event_key"]
                != _sha([source["namespace"], source["instance_id"], source["source_event_id"]])
                or row["base_version"] != view["proposal_version"]
                or row["base_content_hash"] != view["effective_content_hash"]
                or row["created_at"] != card["created_at"]
                or row["expires_at"] != card["expires_at"]
                or row["review_id"] != "aer_" + amendment_review_sha256(card)
                or card["expires_at"] != card["created_at"] + 900
            ):
                raise LegacyExportError("Historical amendment card differs from its real revision")
            result.append(
                {
                    "kind": "amendment_card",
                    "id": row["review_id"],
                    "state": "archived_non_executable",
                }
            )
        for row in c.execute("SELECT * FROM application_posting_reviews ORDER BY review_id"):
            _, card = self._posting._read_review(row["review_id"])
            view = card["proposal_review"]
            proposal, payload, rebuilt, lineage = _historical_review(
                c,
                view["proposal_public_id"],
                view["proposal_version"],
                view["effective_content_hash"],
                view["parse_status"],
            )
            source = vars(self._posting._source(view["intake_public_id"]))
            if card["posting_path"] == "text":
                amount, currency = decision_owner._required_canonical_money(payload)
                financial = {
                    "intent": payload.get("intent") or "personal_expense_log",
                    "amount": amount,
                    "currency": currency,
                    "transaction_date": decision_owner._required_date(payload),
                    "merchant": decision_owner._optional_text(payload.get("merchant")),
                    "description": decision_owner._optional_text(payload.get("description")),
                    "category": payload.get("category"),
                    "source_channel": decision_owner._source_channel(c, proposal),
                    "account": "unspecified",
                }
                if (
                    proposal["attachment_id"] is not None
                    or card["payer_participant_public_id"] is not None
                ):
                    raise LegacyExportError(
                        "Historical text card has contradictory receipt material"
                    )
            elif card["posting_path"] == "personal_receipt":
                payer = card["payer_participant_public_id"]
                fields = resolve_independent_receipt_payload_fields(c, payload)
                calculation = calculate_receipt_split(
                    {
                        "currency": fields["currency"],
                        "participants": [payer],
                        "payer": payer,
                        "receipts": [
                            {
                                "merchant": fields["merchant"],
                                "paid_by": payer,
                                "net_paid": fields["canonical_amount"],
                                "items": [
                                    {
                                        "description": "Receipt total",
                                        "amount": fields["canonical_amount"],
                                        "owners": [payer],
                                    }
                                ],
                            }
                        ],
                    }
                )
                metadata = (
                    ReceiptBookkeepingMetadata(fields["description"], fields["category"])
                    if fields["description"] is not None or fields["category"] is not None
                    else None
                )
                financial = build_application_receipt_projection(
                    bookkeeping_metadata=metadata,
                    merchant=fields["merchant"],
                    receipt_date=fields["receipt_date"],
                    currency=fields["currency"],
                    payer_participant_public_id=payer,
                    calculation=calculation,
                )
            else:
                raise LegacyExportError("Unsupported historical posting route")
            assessment = _assessment(c, proposal, payload, rebuilt, lineage, source)
            if (
                card["binding"] != vars(self._posting._binding)
                or card["source"] != source
                or view != rebuilt
                or card["financial_projection"]
                != canonical_json_value(
                    canonical_json_text(financial), label="financial projection"
                )
                or card.get("expense_assessment") != assessment
                or row["parser_output_id"] != proposal["id"]
                or card["source_evidence_id"] != source["evidence_id"]
                or card["source_evidence_digest"] != source["evidence_digest"]
                or card["proposal_public_id"] != view["proposal_public_id"]
                or card["proposal_version"] != view["proposal_version"]
                or card["effective_content_hash"] != view["effective_content_hash"]
                or row["review_id"] != "apr_" + posting_review_sha256(card)
                or card["expires_at"] != card["prepared_at"] + 900
            ):
                raise LegacyExportError("Historical posting card differs from its real revision")
            result.append(
                {"kind": "posting_card", "id": row["review_id"], "state": "archived_non_executable"}
            )
        return result

    def _plans(self, current_by_target) -> list[dict[str, str]]:
        result = []
        service = self._corrections
        for row in self._connection.execute(
            "SELECT * FROM application_correction_plans ORDER BY plan_id"
        ):
            _, plan = service._plan(row["plan_id"])
            target = plan["target_id"]
            if target not in current_by_target:
                raise LegacyExportError("Correction plan has no verified original posting")
            current, applied = current_by_target[target]
            source = current.original_source
            states = [service._original(source), *applied.values()]
            before = next(
                (item for item in states if item.version == plan["expected_version"]), None
            )
            if before is None or any(
                (
                    plan["before"] != before.fields.as_dict(),
                    plan["predecessor_id"] != before.predecessor_id,
                    plan["predecessor_hash"] != before.predecessor_hash,
                    plan["attempt_id"] != source.attempt_id,
                    plan["route"] != source.route,
                    plan["source"] != vars(source.source),
                    plan["source_hash"] != source.source_hash,
                    plan["original_hash"] != source.original_hash,
                    plan["expires_at"] != plan["created_at"] + 600,
                )
            ):
                raise LegacyExportError(
                    "Correction plan differs from its authenticated predecessor"
                )
            created = datetime.fromtimestamp(plan["created_at"], UTC).isoformat()
            # The unchanged receipt owner recomputes pending financial material.
            if plan["plan_id"] not in applied:
                service._receipt(source, plan, before, created)
            result.append(
                {
                    "kind": "correction_plan",
                    "id": row["plan_id"],
                    "state": "archived_non_executable",
                }
            )
        return result

    def _originals(self) -> tuple[tuple[str, str, bytes], ...]:
        rows = self._connection.execute(
            "SELECT public_id,file_path,file_hash FROM attachments ORDER BY public_id"
        ).fetchall()
        if rows and self._attachment_root is None:
            raise LegacyExportError("Legacy attachment root is required")
        output = []
        for row in rows:
            path = Path(row["file_path"])
            root = self._attachment_root
            assert root is not None
            if (
                not root.is_absolute()
                or not path.is_absolute()
                or path.is_symlink()
                or not path.resolve().is_relative_to(root.resolve())
            ):
                raise LegacyExportError("Legacy original is outside fixed attachment root")
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if row["file_hash"].removeprefix("sha256:") != digest:
                raise LegacyExportError("Legacy original bytes differ from retained evidence")
            output.append((str(path), digest, data))
        return tuple(output)

    def verify_export(self) -> VerifiedLegacyExport:
        """Verify the entire supported snapshot; never dispatch or mint authority."""
        c = self._connection
        posting = self._posting
        require_finance_read_connection(c, instance_id=posting._binding.instance_id)
        if c.in_transaction:
            raise LegacyExportError("Legacy export owns its fixed read snapshot")
        try:
            with review_snapshot(c):
                specs = build_migration_manifest(LEGACY_MIGRATION_PATHS)
                ledger = migration_ledger_rows(c)
                if len(specs) != len(ledger) or any(
                    row["migration_id"] != spec.migration_id
                    or row["migration_filename"] != spec.filename
                    or row["migration_sequence"] != spec.sequence
                    or row["checksum_sha256"] != spec.checksum_sha256
                    for row, spec in zip(ledger, specs, strict=True)
                ):
                    raise LegacyExportError("Legacy migration ledger differs from fixed baseline")
                schema: dict[str, Any] = canonical_schema_payload(c)
                schema["tables"] = [
                    table
                    for table in schema["tables"]
                    if table["name"] not in self._authority_tables
                ]
                schema["objects"] = [
                    obj for obj in schema["objects"] if obj["table"] not in self._authority_tables
                ]
                if _snapshot_hash(schema) != ledger[-1]["schema_fingerprint"]:
                    raise LegacyExportError("Legacy Core schema differs from migration baseline")
                if c.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise LegacyExportError("Legacy snapshot has broken foreign keys")
                archive = _archive(c)
                for table in (
                    "correction_plans",
                    "correction_versions",
                    "parser_human_drafts",
                ):
                    material = archive["tables"].get(table)
                    if material and material["rows"]:
                        raise LegacyExportError("Legacy owner format requires a separate adapter")
                for row in c.execute("SELECT public_id FROM raw_intake_records ORDER BY public_id"):
                    posting._source(row[0])
                # Authentic provider/input roots are re-read via the old fixed AI owner.
                ai = posting._ai_lineage_authority
                if archive["tables"].get("independent_ai_attempts", {}).get("rows"):
                    if type(ai) is not AiInvocationService:
                        raise LegacyExportError("Historical provider authority is unavailable")
                    for row in c.execute(
                        "SELECT attempt_id FROM independent_ai_attempts ORDER BY attempt_id"
                    ):
                        ai.get_status(row[0])
                for row in c.execute(
                    "SELECT * FROM application_amendment_records ORDER BY amendment_id"
                ):
                    self._amendment._accepted(row)
                archive_inventory = self._cards()
                records = []
                current_by_target = {}
                attempts = c.execute(
                    "SELECT attempt_id FROM application_posting_attempts ORDER BY attempt_id"
                ).fetchall()
                for row in attempts:
                    status = posting.get_status(row[0])
                    if status.state != "finalized" or status.transaction_public_id is None:
                        raise LegacyExportError(
                            "Unfinished accepted posting requires explicit recovery"
                        )
                    current, applied = self._corrections._lookup(status.transaction_public_id)
                    current_by_target[status.transaction_public_id] = current, applied
                    records.append(
                        {
                            "legacy_record_id": status.transaction_public_id,
                            "attempt_id": status.attempt_id,
                            "original": current.original_source.fields.as_dict(),
                            "current": current.fields.as_dict(),
                            "version": current.version,
                            "history": list(current.history),
                            "original_evidence": canonical_json_value(
                                current.original_source.source_json, label="old original source"
                            ),
                            "source": vars(current.original_source.source),
                            "source_hash": current.original_source.source_hash,
                            "original_hash": current.original_source.original_hash,
                        }
                    )
                if {r[0] for r in c.execute("SELECT public_id FROM transactions")} != set(
                    current_by_target
                ):
                    raise LegacyExportError(
                        "Unowned financial facts require their separate adapter"
                    )
                archive_inventory.extend(self._plans(current_by_target))
                for row in c.execute(
                    "SELECT DISTINCT aggregate_type,aggregate_public_id FROM financial_audit_events"
                ):
                    chain = verify_financial_audit_chain(
                        c, aggregate_type=row[0], aggregate_public_id=row[1]
                    )
                    if not chain.valid:
                        raise LegacyExportError("Legacy audit chain is invalid")
                originals = self._originals()
                if _archive(c) != archive:
                    raise LegacyExportError("Legacy verification changed its snapshot")
                snapshot_hash = _snapshot_hash(archive)
                manifest = {
                    "schema": EXPORT_SCHEMA,
                    "legacy_baseline": LEGACY_BASELINE,
                    "legacy_namespace": posting._binding.source_namespace,
                    "instance_id": posting._binding.instance_id,
                    "snapshot_sha256": snapshot_hash,
                    "records": records,
                    "archive_inventory": archive_inventory,
                    "coverage": "supported_independent_lifecycle_complete",
                    "archive_rights": "read_only_non_executable",
                    "originals": [
                        {"path": p, "sha256": h, "size": len(b)} for p, h, b in originals
                    ],
                }
                return VerifiedLegacyExport(
                    _issuer=_ISSUER,
                    schema=EXPORT_SCHEMA,
                    legacy_baseline=LEGACY_BASELINE,
                    legacy_namespace=posting._binding.source_namespace,
                    instance_id=posting._binding.instance_id,
                    snapshot_sha256=snapshot_hash,
                    manifest_json=_wire(manifest),
                    archive_json=_wire(archive),
                    originals=originals,
                    signature=self._export_verifier._sign("export", manifest),
                    signing_key_id=self._export_verifier.key_id,
                )
        except Exception as exc:
            if isinstance(exc, LegacyExportError):
                raise
            raise LegacyExportError(
                "Legacy source, approval, provider, card, or plan did not verify"
            ) from exc


def _export_material(export: VerifiedLegacyExport) -> dict[str, Any]:
    return {
        "manifest": json.loads(export.manifest_json),
        "archive_json": export.archive_json,
        "originals": [
            {"path": p, "sha256": h, "data_hex": b.hex()} for p, h, b in export.originals
        ],
        "signature": export.signature,
        "signing_key_id": export.signing_key_id,
    }


def _verifier(value: object) -> LegacyExportVerifier:
    if type(value) is not LegacyExportVerifier:
        raise LegacyExportError("A fixed legacy recovery verifier is required")
    return value


def _capsule(conn, export_id, kind, identity, verifier):
    verifier = _verifier(verifier)
    row = conn.execute(
        "SELECT material_json FROM l2_archives WHERE export_id=? AND kind=? AND identity=?",
        (export_id, kind, identity),
    ).fetchone()
    if row is None:
        raise LegacyExportError("Imported evidence capsule is absent")
    sealed = json.loads(row[0])
    material = sealed["material"]
    verifier._verify(kind, material, sealed["signature"])
    if (
        material["export_id"] != export_id
        or material["identity"] != identity
        or material["legacy_namespace"] != verifier.namespace
        or material["legacy_instance_id"] != verifier.instance_id
    ):
        raise LegacyExportError("Imported capsule namespace or identity differs")
    return material


def _persist_capsule(conn, export_id, kind, identity, payload, verifier, snapshot_hash):
    material = {
        "export_id": export_id,
        "identity": identity,
        "legacy_namespace": verifier.namespace,
        "legacy_instance_id": verifier.instance_id,
        "snapshot_sha256": snapshot_hash,
        **payload,
    }
    conn.execute(
        "INSERT INTO l2_archives VALUES(?,?,?,?)",
        (
            export_id,
            identity,
            kind,
            _wire({"material": material, "signature": verifier._sign(kind, material)}),
        ),
    )


def _legacy_key(namespace, instance, identity):
    return _snapshot_hash([namespace, instance, identity])


def _versions(record):
    result = [record["original"]]
    for expected, correction in enumerate(record["history"], 1):
        if (
            correction["version"] != expected
            or correction["before"] != result[-1]
            or correction["target_id"] != record["legacy_record_id"]
        ):
            raise LegacyExportError("Exported correction chain differs from original fact")
        result.append(correction["after"])
    if record["version"] != len(result) - 1 or record["current"] != result[-1]:
        raise LegacyExportError("Exported effective values differ from correction chain")
    return result


def import_into_v2(conn, export, binding, verifier):
    """Only the new owner's already-admitted transaction may import issued truth.

    Duplicate suppression uses old namespace/instance/fact identity, independently
    of export digest. A later archive with more drafts cannot duplicate money.
    """
    from .contracts import CommitReceipt, VerifiedSource, canonical, digest
    from .service import _event, _fact_material, _money

    if not conn.in_transaction:
        raise LegacyExportError("Legacy import requires the lifecycle owner's transaction")
    export = require_verified_legacy_export(export)
    verifier = _verifier(verifier)
    material = _export_material(export)
    verifier.verify_payload(material)
    manifest = material["manifest"]
    export_id = "lx_" + _legacy_key(
        export.legacy_namespace, export.instance_id, export.snapshot_sha256
    )
    target = dataclasses.asdict(binding)
    stored = {**material, "target_binding": target}
    stored["import_signature"] = verifier._sign("import", stored)
    prior = conn.execute("SELECT * FROM l2_imports WHERE export_id=?", (export_id,)).fetchone()
    if prior:
        # Replays verify the originally persisted binding, including archived generation.
        old = json.loads(prior["material_json"])
        verifier.verify_payload(old)
        unsigned = {k: v for k, v in old.items() if k != "import_signature"}
        verifier._verify("import", unsigned, old["import_signature"])
        if (
            old["manifest"] != manifest
            or old["target_binding"]["principal_id"] != binding.principal_id
            or old["target_binding"]["client_id"] != binding.client_id
            or digest(old) != prior["export_sha256"]
        ):
            raise LegacyExportError("Legacy replay differs from original target/import")
    else:
        conn.execute(
            "INSERT INTO l2_imports VALUES(?,?,?)", (export_id, digest(stored), canonical(stored))
        )
        _event(
            conn,
            export_id,
            "legacy_archive",
            export_id,
            {
                "export_id": export_id,
                "export_sha256": digest(stored),
                "snapshot_sha256": export.snapshot_sha256,
            },
        )
        for item in manifest["archive_inventory"]:
            conn.execute(
                "INSERT INTO l2_archives VALUES(?,?,?,?)",
                (export_id, item["id"], item["kind"], canonical(item)),
            )
    archive = json.loads(export.archive_json)
    intake_table = archive["tables"]["raw_intake_records"]
    intakes = {
        row[intake_table["columns"].index("public_id")]: dict(zip(intake_table["columns"], row))
        for row in intake_table["rows"]
    }
    receipts = []
    for record in manifest["records"]:
        key = _legacy_key(export.legacy_namespace, export.instance_id, record["legacy_record_id"])
        record_id, economic_id = "lr_" + key, "legacy:" + key
        source = record["source"]
        source_key = "legacy:" + _legacy_key(
            export.legacy_namespace, export.instance_id, source["source_event_id"]
        )
        row = conn.execute("SELECT * FROM l2_sources WHERE source_key=?", (source_key,)).fetchone()
        intake = intakes[source["intake_public_id"]]
        raw = intake["raw_input"]
        raw_hash = hashlib.sha256(raw.encode()).hexdigest()
        if raw_hash != source["source_content_hash"]:
            raise LegacyExportError("Exported original text differs from actual source")
        if row is not None and (row["raw_input"] != raw or row["original_sha256"] != raw_hash):
            raise LegacyExportError("A later export contradicts the imported original source")
        if row is None:
            source_id = "ls_" + _legacy_key(
                export.legacy_namespace, export.instance_id, source["intake_public_id"]
            )
            evidence_id = f"legacy:{export_id}:{source_id}"
            evidence = VerifiedSource(
                evidence_id,
                binding.principal_id,
                binding.client_id,
                source_key,
                raw,
                raw_hash,
                source_timestamp=source["received_at"],
            )
            values = {
                "source_id": source_id,
                **{
                    name: getattr(evidence, name)
                    for name in (
                        "evidence_id",
                        "principal_id",
                        "client_id",
                        "source_key",
                        "raw_input",
                        "original_sha256",
                    )
                },
                "source_sha256": digest(dataclasses.asdict(evidence)),
                "generation": binding.generation,
                "created_at": source["received_at"],
            }
            conn.execute(
                "INSERT INTO l2_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    values[name]
                    for name in (
                        "source_id",
                        "source_key",
                        "evidence_id",
                        "principal_id",
                        "client_id",
                        "raw_input",
                        "original_sha256",
                        "source_sha256",
                        "generation",
                        "created_at",
                    )
                ),
            )
            _persist_capsule(
                conn,
                export_id,
                "source",
                source_id,
                {"source_row": values, "legacy_source": source, "target_binding": target},
                verifier,
                export.snapshot_sha256,
            )
            row = conn.execute(
                "SELECT * FROM l2_sources WHERE source_id=?", (source_id,)
            ).fetchone()
        verify_imported_source(conn, row, binding, verifier)
        source_id = row["source_id"]
        versions = _versions(record)
        if not prior:
            _persist_capsule(
                conn,
                export_id,
                "record",
                record_id,
                {
                    "record": record,
                    "source_id": source_id,
                    "economic_event_id": economic_id,
                    "target_binding": target,
                },
                verifier,
                export.snapshot_sha256,
            )
        previous = None
        for version, values in enumerate(versions, 1):
            _money(values)
            fact_hash = digest(
                _fact_material(record_id, version, economic_id, source_id, values, previous)
            )
            existing = conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? AND version=?", (record_id, version)
            ).fetchone()
            if existing:
                verify_imported_fact(conn, existing, verifier)
                if (
                    existing["fact_sha256"] != fact_hash
                    or existing["values_json"] != canonical(values)
                    or existing["source_id"] != source_id
                    or existing["economic_event_id"] != economic_id
                ):
                    raise LegacyExportError(
                        "Different export contradicts an imported economic fact"
                    )
                seq = existing["commit_seq"]
            else:
                operation = "legacy-fact:" + key + ":" + str(version)
                seq = _event(
                    conn,
                    operation,
                    "import",
                    record_id,
                    {
                        "export_id": export_id,
                        "legacy_record_id": record["legacy_record_id"],
                        "legacy_version": version - 1,
                        "version": version,
                        "fact_sha256": fact_hash,
                    },
                )
                conn.execute(
                    "INSERT INTO l2_facts VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        version,
                        economic_id,
                        source_id,
                        None,
                        canonical(values),
                        previous,
                        fact_hash,
                        seq,
                    ),
                )
            previous = fact_hash
        receipts.append(CommitReceipt(record_id, len(versions), previous, seq))
    return tuple(receipts)


def verify_imported_source(conn, row, binding, verifier):
    """Verify a small indexed source capsule through the independent recovery root."""
    verifier = _verifier(verifier)
    parts = row["evidence_id"].split(":")
    if len(parts) != 3 or parts[0] != "legacy" or parts[2] != row["source_id"]:
        raise LegacyExportError("Imported source receipt identity differs")
    material = _capsule(conn, parts[1], "source", row["source_id"], verifier)
    source_row = material["source_row"]
    if (
        dict(row) != source_row
        or source_row["principal_id"] != binding.principal_id
        or source_row["client_id"] != binding.client_id
        or hashlib.sha256(source_row["raw_input"].encode()).hexdigest()
        != source_row["original_sha256"]
        or material["legacy_source"]["source_content_hash"] != source_row["original_sha256"]
    ):
        raise LegacyExportError("Imported source differs from authenticated legacy material")
    return row


def verify_imported_fact(conn, fact, verifier):
    """Bind each fact version to a durable root-authenticated historical capsule."""
    from .contracts import RuntimeBinding, digest
    from .service import _fact_material

    event = conn.execute("SELECT * FROM l2_events WHERE seq=?", (fact["commit_seq"],)).fetchone()
    if event is None or event["kind"] != "import" or event["entity_id"] != fact["record_id"]:
        raise LegacyExportError("Imported fact has no exact import event")
    payload = json.loads(event["payload_json"])
    material = _capsule(conn, payload["export_id"], "record", fact["record_id"], verifier)
    record = material["record"]
    versions = _versions(record)
    version = fact["version"]
    if (
        type(version) is not int
        or not 1 <= version <= len(versions)
        or payload["legacy_record_id"] != record["legacy_record_id"]
        or payload["legacy_version"] != version - 1
        or payload["version"] != version
        or fact["values_json"] != _wire(versions[version - 1])
        or fact["source_id"] != material["source_id"]
        or fact["economic_event_id"] != material["economic_event_id"]
        or fact["review_id"] is not None
    ):
        raise LegacyExportError("Imported fact differs from authenticated historical version")
    expected = digest(
        _fact_material(
            fact["record_id"],
            version,
            fact["economic_event_id"],
            fact["source_id"],
            versions[version - 1],
            fact["predecessor_sha256"],
        )
    )
    if expected != fact["fact_sha256"] or payload["fact_sha256"] != expected:
        raise LegacyExportError("Imported fact hash differs from its exact archive capsule")
    source_row = conn.execute(
        "SELECT * FROM l2_sources WHERE source_id=?", (fact["source_id"],)
    ).fetchone()
    if source_row is None:
        raise LegacyExportError("Imported fact has no authenticated source")
    verify_imported_source(conn, source_row, RuntimeBinding(**material["target_binding"]), verifier)
    return material


def verify_import_archives(conn, verifier):
    """Whole-archive audit/recovery check, outside the ordinary indexed read path."""
    from .contracts import digest

    rows = conn.execute("SELECT * FROM l2_imports ORDER BY export_id").fetchall()
    if not rows:
        return
    verifier = _verifier(verifier)
    for row in rows:
        material = json.loads(row["material_json"])
        verifier.verify_payload(material)
        unsigned = {key: value for key, value in material.items() if key != "import_signature"}
        verifier._verify("import", unsigned, material["import_signature"])
        if digest(material) != row["export_sha256"]:
            raise LegacyExportError("Persisted complete legacy archive changed")
        event = conn.execute(
            "SELECT * FROM l2_events WHERE operation_id=?", (row["export_id"],)
        ).fetchone()
        if (
            event is None
            or event["kind"] != "legacy_archive"
            or json.loads(event["payload_json"])["export_sha256"] != row["export_sha256"]
        ):
            raise LegacyExportError("Complete legacy archive lacks its exact import event")
        expected_archived = {
            (item["kind"], item["id"]): item for item in material["manifest"]["archive_inventory"]
        }
        observed_archived = {}
        observed_records = {}
        for archived in conn.execute(
            "SELECT * FROM l2_archives WHERE export_id=? ORDER BY kind,identity",
            (row["export_id"],),
        ):
            if archived["kind"] in {"source", "record"}:
                capsule = _capsule(
                    conn, row["export_id"], archived["kind"], archived["identity"], verifier
                )
                if capsule["snapshot_sha256"] != material["manifest"]["snapshot_sha256"]:
                    raise LegacyExportError("Legacy capsule differs from its complete archive")
                if archived["kind"] == "record":
                    record = capsule["record"]
                    observed_records[record["legacy_record_id"]] = record
            else:
                observed_archived[(archived["kind"], archived["identity"])] = json.loads(
                    archived["material_json"]
                )
        expected_records = {
            record["legacy_record_id"]: record for record in material["manifest"]["records"]
        }
        if observed_archived != expected_archived or observed_records != expected_records:
            raise LegacyExportError("Legacy archive capsule inventory is incomplete or changed")
