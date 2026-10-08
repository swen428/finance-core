"""Independent one-confirmation posting using guarded financial owners.

Only durable references enter operational calls. Composition owns trust roots,
ports and clock. Core owns immutable decision consumption, not the adapter.
"""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from finance_core.application.admission import (
    AdmissionService,
    ExpectedHumanDecision,
    SourceVerifier,
    TrustedBinding,
    VerifiedHumanDecision,
    VerifiedSource,
    _digest,
    _positive_int,
    _proposal_version,
    _reference,
    validate_verified_source,
)
from finance_core.application.posting_contract import (
    POSTING_DECISION_SCHEMA,
    POSTING_REVIEW_SCHEMA,
    posting_review_sha256,
    require_posting_schema,
)
from finance_core.application.review import get_proposal_review, review_snapshot
from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text,
    canonical_json_value,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.financial_audit import (
    FinancialAuditRepository,
    derive_audit_event_public_id,
    verify_financial_audit_chain,
)
from finance_core.parser_proposals import decision_owner
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.conversion_state import has_receipt_ocr_proposal_link
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.receipt_facts_conversion import (
    ReceiptFactsConversionCommand,
    convert_confirmed_receipt_proposal_to_facts,
    resolve_receipt_conversion_payload_fields,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ReceiptItemAllocationFactsCommand,
    persist_receipt_item_allocation_facts,
)
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.receipt_finalization import fact_set_bridge
from finance_core.receipt_finalization.d2_conditional import build_d2_receipt_projection
from finance_core.sqlite_connection import require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database

_failure_injection_hook: Callable[[str], None] | None = None


class PostingError(ValueError):
    """Independent posting is unavailable or its durable proof is contradictory."""


@dataclass(frozen=True)
class ExpectedPostingDecision(ExpectedHumanDecision):
    review_id: str
    review_projection: Mapping[str, object]
    accepted_attempt_id: str | None = None


@dataclass(frozen=True)
class VerifiedPostingDecision(VerifiedHumanDecision):
    review_id: str


class PostingHumanDecisionAuthority(Protocol):
    """Verify signed source/display/decision history in the supplied snapshot.

    With accepted_attempt_id absent, require a currently delivered private
    human-owned display and a fresh, unconsumed confirm. With it present,
    checked_at is the immutable Core acceptance time: verify delivery and
    human ownership at that time from retained history, even after the card
    is closed or replaced. Core alone owns decision consumption; the signed
    external report consumed=False records its state at acceptance and is
    not a claim that Core has not consumed the decision now.
    """

    def verify_persisted(
        self,
        connection: sqlite3.Connection,
        decision_record_id: str,
        expected: ExpectedPostingDecision,
    ) -> VerifiedPostingDecision: ...


@dataclass(frozen=True)
class PostingReview:
    review_id: str
    review_hash: str
    projection: Mapping[str, object]
    expires_at: int


@dataclass(frozen=True)
class PostingStatus:
    attempt_id: str
    state: str
    transaction_public_id: str | None
    attention_reason: str | None


def _posting_reference(value: object) -> bool:
    return _reference(value) and unicodedata.normalize("NFC", str(value)) == value


def _inject(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_text(value).encode()).hexdigest()


def source_event_key(source: VerifiedSource) -> str:
    return _sha([source.namespace, source.instance_id, source.source_event_id])


def _material(row: sqlite3.Row, key: str = "material_json") -> dict[str, Any]:
    value = canonical_json_value(row[key], label="posting evidence")
    if not isinstance(value, dict) or canonical_json_text(value) != row[key]:
        raise PostingError("Posting evidence is not a canonical object")
    return value


class _AcceptedDecisionAuthority:
    def __init__(self, service: PostingService, attempt_id: str) -> None:
        self.service = service
        self.attempt_id = attempt_id

    def verify_in_transaction(
        self,
        connection,
        *,
        proposal,
        content_hash,
        proposal_version,
        authenticated_actor_id,
        decision,
        decision_epoch,
    ):
        row, review, accepted = self.service._accepted(self.attempt_id, verify_confirmation=False)
        frozen = review["proposal_review"]
        if (
            connection is not self.service._conn
            or not connection.in_transaction
            or proposal["public_id"] != frozen["proposal_public_id"]
            or content_hash != frozen["effective_content_hash"]
            or proposal_version != frozen["proposal_version"]
            or authenticated_actor_id != accepted.human_principal_id
            or decision != "confirmed"
            or row["accepted_at"] != decision_epoch
        ):
            raise PostingError("Confirmation does not match the accepted independent decision")

    def persist_effect_in_transaction(self, connection, *, confirmation_public_id):
        row = connection.execute(
            "SELECT confirmation_public_id FROM application_posting_decisions WHERE attempt_id=?",
            (self.attempt_id,),
        ).fetchone()
        if row is None or row[0] != confirmation_public_id:
            raise PostingError("Confirmation lost its atomic decision/attempt binding")


class PostingService:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        source_verifier: SourceVerifier,
        human_decision_authority: PostingHumanDecisionAuthority,
        binding: TrustedBinding,
        clock: Callable[[], int],
    ) -> None:
        # Admission validates fixed trusted binding while retaining PR03 semantics.
        self._admission = AdmissionService(
            connection=connection,
            source_verifier=source_verifier,
            human_decision_authority=human_decision_authority,  # type: ignore[arg-type]
            binding=binding,
            clock=clock,
        )
        if any(unicodedata.normalize("NFC", value) != value for value in vars(binding).values()):
            raise PostingError("Posting trust identifiers must be Unicode NFC")
        self._conn, self._source_port = connection, source_verifier
        self._decision_port, self._binding, self._clock = human_decision_authority, binding, clock

    def _now(self) -> int:
        now = self._clock()
        if not _positive_int(now):
            raise PostingError("Posting clock is unavailable")
        return now

    def _begin(self) -> None:
        require_staging_database(self._conn)
        require_foreign_keys_enabled(self._conn)
        if self._conn.in_transaction:
            raise PostingError("Posting requires a connection without caller-owned work")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            require_posting_schema(self._conn)
        except BaseException:
            self._conn.rollback()
            raise

    def _source(self, intake_id: str) -> VerifiedSource:
        source = validate_verified_source(
            self._source_port.verify_persisted(self._conn, intake_id),
            binding=self._binding,
            intake_public_id=intake_id,
            now=self._now(),
        )
        if any(
            isinstance(value, str) and unicodedata.normalize("NFC", value) != value
            for value in vars(source).values()
        ):
            raise PostingError("Posting source identifiers must be Unicode NFC")
        return source

    def _revalidate_pending_write(self, connection, attempt_id, *, require_payer=True):
        if connection is not self._conn or not connection.in_transaction:
            raise PostingError("Independent write revalidation requires its owning transaction")
        require_posting_schema(connection)
        _row, material, _decision = self._accepted(attempt_id)
        if require_payer and material["posting_path"] == "personal_receipt":
            if self._payer() != material["payer_participant_public_id"]:
                raise PostingError("Personal participant authority changed")

    def _review_material(
        self,
        proposal_id: str,
        *,
        prepared_at: int,
        expires_at: int,
    ) -> dict[str, Any]:
        proposal = ParserProposalRepository(self._conn).get_by_public_id(proposal_id)
        if proposal is None or proposal["parse_status"] != "parsed_pending_confirmation":
            raise PostingError("Posting requires a current pending proposal")
        view = get_proposal_review(self._conn, proposal_id)
        payload, _, _ = resolve_effective_payload(self._conn, proposal)
        if view["classification"] != "personal" or view["account_status"] != "absent":
            raise PostingError("Only personal expenses with an unspecified account are supported")
        if not view["confirm_available"]:
            raise PostingError("Unresolved proposal ambiguity cannot be prepared for posting")
        if payload.get("intent") not in {None, "personal_expense_log", "simple_expense_log"}:
            raise PostingError("Posting supports only personal expense intent")
        if payload.get("category") is not None and not isinstance(payload["category"], str):
            raise PostingError("Posting category must have an exact text representation")
        if "oversized_display_field" in view["ambiguity_indicators"]:
            raise PostingError("Posting cannot approve truncated display fields")
        raw = self._conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
            (view["intake_public_id"],),
        ).fetchone()
        if raw is None or raw[0] != proposal["id"]:
            raise PostingError("Posting proposal is not the current intake leaf")
        if (
            self._conn.execute(
                "SELECT 1 FROM parser_human_drafts "
                "WHERE decision_target_parser_output_id=? LIMIT 1",
                (proposal["id"],),
            ).fetchone()
            is not None
        ):
            raise PostingError("Human correction/draft authority requires its separate owner")
        source = self._source(view["intake_public_id"])
        payer = None
        route = "text"
        if has_receipt_ocr_proposal_link(self._conn, int(proposal["id"])):
            route = "personal_receipt"
            if view["ambiguity_indicators"]:
                raise PostingError("Receipt posting requires resolved complete inputs")
            fields = resolve_receipt_conversion_payload_fields(self._conn, payload)
            payer = self._payer()
            amount, currency = fields["canonical_amount"], fields["currency"]
            calculation = calculate_receipt_split(
                {
                    "currency": currency,
                    "participants": [payer],
                    "payer": payer,
                    "receipts": [
                        {
                            "merchant": fields["merchant"],
                            "paid_by": payer,
                            "net_paid": amount,
                            "items": [
                                {
                                    "description": "Receipt total",
                                    "amount": amount,
                                    "owners": [payer],
                                }
                            ],
                        }
                    ],
                }
            )
            financial = build_d2_receipt_projection(
                merchant=fields["merchant"],
                receipt_date=fields["receipt_date"],
                currency=currency,
                payer_participant_public_id=payer,
                calculation=calculation,
            )
        else:
            fields = decision_owner.resolve_simple_expense_conversion_fields(self._conn, proposal)
            financial = {**fields, "account": "unspecified"}
        return {
            "schema": POSTING_REVIEW_SCHEMA,
            "binding": vars(self._binding),
            "source": vars(source),
            "proposal_review": view,
            "posting_path": route,
            "financial_projection": financial,
            "payer_participant_public_id": payer,
            "prepared_at": prepared_at,
            "expires_at": expires_at,
            "source_evidence_id": source.evidence_id,
            "source_evidence_digest": source.evidence_digest,
            "proposal_public_id": proposal_id,
            "proposal_version": view["proposal_version"],
            "effective_content_hash": view["effective_content_hash"],
        }

    def _payer(self) -> str:
        rows = self._conn.execute(
            "SELECT public_id FROM participants WHERE is_self=1 AND is_active=1 ORDER BY public_id"
        ).fetchall()
        if len(rows) != 1:
            raise PostingError("Personal receipt requires exactly one active self participant")
        return str(rows[0][0])

    def prepare(self, proposal_public_id: str) -> PostingReview:
        if not _posting_reference(proposal_public_id):
            raise PostingError("Proposal reference is invalid")
        self._begin()
        try:
            now = self._now()
            material = self._review_material(
                proposal_public_id, prepared_at=now, expires_at=now + 900
            )
            normalized = canonical_json_value(canonical_json_text(material), label="posting review")
            assert isinstance(normalized, dict)
            material = normalized
            digest = posting_review_sha256(material)
            review_id = "apr_" + digest
            existing = self._conn.execute(
                "SELECT * FROM application_posting_reviews WHERE review_id=?",
                (review_id,),
            ).fetchone()
            if existing is None:
                proposal = ParserProposalRepository(self._conn).get_by_public_id(proposal_public_id)
                assert proposal is not None
                self._conn.execute(
                    "INSERT INTO application_posting_reviews VALUES (?,?,?,?)",
                    (review_id, proposal["id"], canonical_json_text(material), digest),
                )
            elif _material(existing) != material or existing["review_hash"] != digest:
                raise PostingError("Posting review identity conflict")
            self._conn.commit()
            return PostingReview(review_id, digest, material, now + 900)
        except BaseException:
            self._conn.rollback()
            raise

    def _read_review(self, review_id: str) -> tuple[sqlite3.Row, dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM application_posting_reviews WHERE review_id=?",
            (review_id,),
        ).fetchone()
        if row is None:
            raise PostingError("Posting review is unavailable")
        material = _material(row)
        if (
            material.get("schema") != POSTING_REVIEW_SCHEMA
            or posting_review_sha256(material) != row["review_hash"]
            or review_id != "apr_" + row["review_hash"]
        ):
            raise PostingError("Posting review commitment changed")
        return row, material

    def _decision(
        self,
        review_id: str,
        material: dict[str, Any],
        decision_id: str,
        checked_at: int,
        accepted_attempt_id: str | None = None,
    ) -> VerifiedPostingDecision:
        if material["binding"] != vars(self._binding):
            raise PostingError("Posting trust configuration changed")
        source = self._source(material["source"]["intake_public_id"])
        if vars(source) != material["source"]:
            raise PostingError("Accepted source evidence changed")
        expected = ExpectedPostingDecision(
            self._binding,
            source,
            material["proposal_public_id"],
            material["proposal_version"],
            material["effective_content_hash"],
            posting_review_sha256(material),
            checked_at,
            review_id,
            material,
            accepted_attempt_id,
        )
        decision = self._decision_port.verify_persisted(self._conn, decision_id, expected)
        if type(decision) is not VerifiedPostingDecision or (
            decision.schema != POSTING_DECISION_SCHEMA
            or decision.namespace != self._binding.decision_namespace
            or decision.key_id != self._binding.decision_key_id
            or decision.instance_id != self._binding.instance_id
            or decision.human_principal_id != self._binding.human_principal_id
            or decision.decision_id != decision_id
            or decision.review_id != review_id
            or decision.source_evidence_id != source.evidence_id
            or decision.source_evidence_digest != source.evidence_digest
            or decision.proposal_public_id != expected.proposal_public_id
            or not _proposal_version(decision.proposal_version)
            or decision.proposal_version != expected.proposal_version
            or decision.proposal_content_hash != expected.proposal_content_hash
            or decision.review_projection_hash != expected.review_projection_hash
            or not _posting_reference(decision.display_id)
            or not _digest(decision.display_evidence_digest)
            or decision.action != "confirm"
            or not _positive_int(decision.issued_at)
            or not _positive_int(decision.expires_at)
            or not decision.issued_at <= checked_at < decision.expires_at
            or type(decision.consumed) is not bool
            or decision.consumed
            or not _digest(decision.decision_digest)
        ):
            raise PostingError("Human decision does not match independent posting review")
        return decision

    def submit_post(self, review_id: str, decision_record_id: str) -> PostingStatus:
        if not _posting_reference(review_id) or not _posting_reference(decision_record_id):
            raise PostingError("Posting decision reference is invalid")
        self._begin()
        try:
            row, material = self._read_review(review_id)
            now = self._now()
            if now >= material["expires_at"]:
                raise PostingError("Posting review expired")
            fresh = self._review_material(
                material["proposal_public_id"],
                prepared_at=material["prepared_at"],
                expires_at=material["expires_at"],
            )
            if canonical_json_text(fresh) != canonical_json_text(material):
                raise PostingError("Posting review no longer matches current proposal")
            decision = self._decision(review_id, material, decision_record_id, now)
            consumed = self._conn.execute(
                "SELECT 1 FROM application_posting_decisions WHERE decision_namespace=? "
                "AND decision_id=?",
                (decision.namespace, decision.decision_id),
            ).fetchone()
            if consumed is not None:
                raise PostingError("Human decision was already consumed")
            event_key = source_event_key(VerifiedSource(**material["source"]))
            attempt_id = "apa_" + _sha(
                [self._binding.instance_id, decision.namespace, decision.decision_id]
            )
            confirmation_id = "pca_application_" + _sha([attempt_id])
            self._conn.execute(
                "INSERT INTO application_posting_attempts VALUES (?,?,?,?,'accepted',NULL,NULL)",
                (attempt_id, review_id, event_key, material["source"]["intake_public_id"]),
            )
            self._conn.execute(
                "INSERT INTO application_posting_decisions VALUES (?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    decision.namespace,
                    decision.decision_id,
                    decision.decision_digest,
                    confirmation_id,
                    canonical_json_text(vars(decision)),
                    now,
                ),
            )
            decision_owner.confirm_parser_proposal(
                self._conn,
                int(row["parser_output_id"]),
                authenticated_actor_id=self._binding.human_principal_id,
                confirmation_channel="independent_application",
                confirmation_public_id=confirmation_id,
                expected_content_hash=material["effective_content_hash"],
                expected_version=material["proposal_version"],
                decision_authority=_AcceptedDecisionAuthority(self, attempt_id),
                clock=lambda: datetime.fromtimestamp(now, UTC).isoformat(),
                _caller_owns_transaction=True,
            )
            self._conn.execute(
                "INSERT INTO application_posting_events"
                "(attempt_id,from_stage,to_stage,created_at) "
                "VALUES (?,NULL,'accepted',?)",
                (attempt_id, now),
            )
            _inject("before_acceptance_commit")
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        _inject("after_acceptance_commit")
        return self.resume_post(attempt_id)

    def _accepted(
        self,
        attempt_id: str,
        *,
        verify_confirmation: bool = True,
    ) -> tuple[sqlite3.Row, dict[str, Any], VerifiedPostingDecision]:
        row = self._conn.execute(
            "SELECT attempts.*,decisions.decision_namespace,decisions.decision_id, "
            "decisions.decision_digest,decisions.confirmation_public_id, "
            "decisions.material_json AS decision_material_json,decisions.accepted_at "
            "FROM application_posting_attempts AS attempts JOIN application_posting_decisions "
            "AS decisions USING(attempt_id) WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise PostingError("Accepted posting attempt is unavailable")
        review_row, material = self._read_review(row["review_id"])
        if not material["prepared_at"] <= row["accepted_at"] < material["expires_at"]:
            raise PostingError("Posting acceptance was outside review validity")
        decision = self._decision(
            row["review_id"], material, row["decision_id"], row["accepted_at"], attempt_id
        )
        if (
            canonical_json_text(vars(decision)) != row["decision_material_json"]
            or decision.decision_digest != row["decision_digest"]
            or decision.namespace != row["decision_namespace"]
            or row["intake_public_id"] != material["source"]["intake_public_id"]
            or attempt_id
            != "apa_" + _sha([self._binding.instance_id, decision.namespace, decision.decision_id])
            or row["source_event_key"] != source_event_key(VerifiedSource(**material["source"]))
        ):
            raise PostingError("Accepted decision evidence changed")
        proposal = ParserProposalRepository(self._conn).get(int(review_row["parser_output_id"]))
        if proposal is None:
            raise PostingError("Accepted proposal is unavailable")
        _, _, version = resolve_effective_payload(self._conn, proposal)
        if (
            proposal["public_id"] != material["proposal_public_id"]
            or version != material["proposal_version"]
            or compute_effective_proposal_content_hash(self._conn, proposal)
            != material["effective_content_hash"]
        ):
            raise PostingError("Accepted financial content changed")
        if verify_confirmation:
            authorization = self._conn.execute(
                "SELECT * FROM parser_proposal_authorizations WHERE confirmation_public_id=?",
                (row["confirmation_public_id"],),
            ).fetchone()
            if (
                authorization is None
                or authorization["parser_output_id"] != proposal["id"]
                or authorization["proposal_content_hash"] != material["effective_content_hash"]
                or authorization["authenticated_actor_id"] != decision.human_principal_id
                or authorization["confirmation_state"] != "confirmed"
                or authorization["revoked_at"] is not None
                or authorization["confirmation_channel"] != "independent_application"
                or proposal["parse_status"] != "confirmed"
                or authorization["decided_at"]
                != datetime.fromtimestamp(row["accepted_at"], UTC).isoformat()
            ):
                raise PostingError("Independent confirmation evidence changed")
            accepted_event = self._conn.execute(
                "SELECT from_stage,created_at FROM application_posting_events "
                "WHERE attempt_id=? AND to_stage='accepted'",
                (attempt_id,),
            ).fetchone()
            if accepted_event is None or tuple(accepted_event) != (None, row["accepted_at"]):
                raise PostingError("Independent acceptance event changed")
            self._verify_parser_audit(
                material,
                decision,
                row["confirmation_public_id"],
                "parser_proposal_confirmed",
                row["confirmation_public_id"],
            )
        return row, material, decision

    def _verify_parser_audit(self, material, decision, confirmation_id, event_type, causation):
        proposal_id = material["proposal_public_id"]
        chain = verify_financial_audit_chain(
            self._conn,
            aggregate_type="parser_proposal",
            aggregate_public_id=proposal_id,
        )
        event_id = derive_audit_event_public_id(
            aggregate_type="parser_proposal",
            aggregate_public_id=proposal_id,
            event_type=event_type,
            causation_public_id=causation,
        )
        event = FinancialAuditRepository(self._conn).fetch(event_id)
        if (
            event is None
            or not chain.valid
            or (
                event.actor_type != "human"
                or event.actor_public_id != decision.human_principal_id
                or event.authorization_public_id != confirmation_id
                or event.correlation_public_id != proposal_id
                or event.causation_public_id != causation
                or f"parser-output:{proposal_id}" not in event.source_evidence_references
                or f"source:{material['source']['intake_public_id']}"
                not in event.source_evidence_references
            )
        ):
            raise PostingError("Independent parser audit chain changed")
        payload = canonical_json_value(event.event_payload_json, label="parser audit payload")
        if not isinstance(payload, dict) or (
            payload.get("proposal_content_hash") != material["effective_content_hash"]
            or (
                event_type == "parser_proposal_converted"
                and payload.get("transaction_public_id") != causation
            )
        ):
            raise PostingError("Independent parser audit material changed")

    def _evidence(self, conn, attempt_id, kind, public_id, digest, material):
        expected = (public_id, digest, canonical_json_text(material))
        existing = conn.execute(
            "SELECT evidence_public_id,evidence_hash,material_json FROM "
            "application_posting_receipt_evidence WHERE attempt_id=? AND evidence_type=?",
            (attempt_id, kind),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO application_posting_receipt_evidence VALUES (?,?,?,?,?)",
                (attempt_id, kind, *expected),
            )
        elif tuple(existing) != expected:
            raise PostingError("Receipt stage evidence changed")

    def _advance(self, attempt_id, stage, evidence_id=None, transaction_id=None):
        self._begin()
        try:
            row, _, _ = self._accepted(attempt_id)
            old = row["stage"]
            if stage == "finalized":
                verified = self.get_status(attempt_id)
                if verified.transaction_public_id != transaction_id:
                    raise PostingError("Finalized catchup lacks verified canonical truth")
            if old == stage:
                self._conn.rollback()
                return
            if old in {"finalized", "needs_attention"}:
                raise PostingError("Terminal posting state cannot advance")
            self._conn.execute(
                "UPDATE application_posting_attempts SET stage=?,transaction_public_id=? "
                "WHERE attempt_id=? AND stage=?",
                (stage, transaction_id, attempt_id, old),
            )
            self._conn.execute(
                "INSERT INTO application_posting_events(attempt_id,from_stage,to_stage, "
                "evidence_public_id,created_at) VALUES (?,?,?,?,?)",
                (attempt_id, old, stage, evidence_id, self._now()),
            )
            if stage == "finalized":
                _inject("before_status_catchup_commit")
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def _attention(self, attempt_id: str, reason: str) -> None:
        self._begin()
        try:
            row = self._conn.execute(
                "SELECT stage FROM application_posting_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is not None and row[0] not in {"finalized", "needs_attention"}:
                self._conn.execute(
                    "UPDATE application_posting_attempts "
                    "SET stage='needs_attention',attention_reason=? "
                    "WHERE attempt_id=?",
                    (reason, attempt_id),
                )
                self._conn.execute(
                    "INSERT INTO application_posting_events"
                    "(attempt_id,from_stage,to_stage,created_at) "
                    "VALUES (?,?,'needs_attention',?)",
                    (attempt_id, row[0], self._now()),
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def resume_post(self, attempt_id: str) -> PostingStatus:
        if not _posting_reference(attempt_id):
            raise PostingError("Attempt reference is invalid")
        require_staging_database(self._conn)
        require_foreign_keys_enabled(self._conn)
        if self._conn.in_transaction:
            raise PostingError("Posting requires a connection without caller-owned work")
        with review_snapshot(self._conn):
            require_posting_schema(self._conn)
            row, material, _ = self._accepted(attempt_id)
        status = self.get_status(attempt_id)
        if status.state == "finalized":
            if row["stage"] != "finalized":
                self._advance(
                    attempt_id,
                    "finalized",
                    status.transaction_public_id,
                    status.transaction_public_id,
                )
            return status
        if row["stage"] == "needs_attention":
            return status
        if material["posting_path"] == "text":

            def bind_text(connection, result):
                self._revalidate_pending_write(connection, attempt_id)

            result = decision_owner.convert_confirmed_parser_proposal(
                self._conn,
                int(
                    self._conn.execute(
                        "SELECT parser_output_id FROM application_posting_reviews "
                        "WHERE review_id=?",
                        (row["review_id"],),
                    ).fetchone()[0]
                ),
                persistence_effect=bind_text,
            )
            _inject("after_text_conversion_commit")
            self._advance(
                attempt_id,
                "finalized",
                result["transaction_public_id"],
                result["transaction_public_id"],
            )
        elif material["posting_path"] == "personal_receipt":
            self._resume_receipt(attempt_id)
        else:
            raise PostingError("Unknown independent posting route")
        return self.get_status(attempt_id)

    def _resume_receipt(self, attempt_id: str) -> None:
        while True:
            with review_snapshot(self._conn):
                row, material, decision = self._accepted(attempt_id)
                stage = row["stage"]
            if stage in {"finalized", "needs_attention"}:
                return
            payer = material["payer_participant_public_id"]
            if self._payer() != payer:
                self._attention(attempt_id, "personal_participant_authority_changed")
                raise PostingError("Personal participant authority changed")
            projection = material["financial_projection"]
            conversion_id = "rpfc_application_" + _sha([attempt_id])[:24]
            fact_id = "riaf_application_" + _sha([attempt_id])[:24]
            if stage == "accepted":
                command = ReceiptFactsConversionCommand(
                    command_public_id=conversion_id,
                    proposal_public_id=material["proposal_public_id"],
                    expected_content_hash=material["effective_content_hash"],
                    payer_participant_public_id=payer,
                    participants=({"participant_public_id": payer, "is_included": 1},),
                    authenticated_actor_id=decision.human_principal_id,
                    channel="independent_application",
                )

                def bind_conversion(conn, result):
                    self._revalidate_pending_write(conn, attempt_id)
                    self._evidence(
                        conn,
                        attempt_id,
                        "conversion",
                        result.command_public_id,
                        result.conversion_result_hash,
                        {
                            "command_public_id": result.command_public_id,
                            "receipt_public_id": result.receipt_public_id,
                            "conversion_result_hash": result.conversion_result_hash,
                        },
                    )

                converted = convert_confirmed_receipt_proposal_to_facts(
                    self._conn,
                    command,
                    persistence_effect=bind_conversion,
                )
                _inject("after_conversion_commit")
                self._advance(attempt_id, "conversion_persisted", converted.command_public_id)
                continue
            conversion = self._conn.execute(
                "SELECT conversions.*,receipts.public_id AS receipt_public_id FROM "
                "receipt_proposal_conversions AS conversions JOIN receipts ON receipts.id="
                "conversions.receipt_id WHERE command_public_id=?",
                (conversion_id,),
            ).fetchone()
            if conversion is None:
                raise PostingError("Receipt conversion proof is unavailable")
            if stage == "conversion_persisted":
                amount, currency = projection["amount"], projection["currency"]
                command_facts = ReceiptItemAllocationFactsCommand(
                    command_public_id=fact_id,
                    receipt_public_id=conversion["receipt_public_id"],
                    expected_conversion_command_public_id=conversion_id,
                    expected_conversion_result_hash=conversion["conversion_result_hash"],
                    expected_current_fact_set="none",
                    items=(
                        {
                            "line_number": 1,
                            "item_name": "Receipt total",
                            "line_amount": amount,
                            "currency": currency,
                        },
                    ),
                    allocations=(
                        {
                            "line_number": 1,
                            "allocation_method": "manual",
                            "participants": (
                                {
                                    "participant_public_id": payer,
                                    "share_amount": amount,
                                    "currency": currency,
                                },
                            ),
                        },
                    ),
                    adjustments=(),
                    authenticated_actor_id=decision.human_principal_id,
                    channel="independent_application",
                )

                def bind_facts(conn, result):
                    self._revalidate_pending_write(conn, attempt_id)
                    value = {key: val for key, val in vars(result).items() if key != "idempotent"}
                    self._evidence(
                        conn,
                        attempt_id,
                        "fact_set",
                        result.fact_set_public_id,
                        result.fact_set_result_hash,
                        value,
                    )

                facts = persist_receipt_item_allocation_facts(
                    self._conn,
                    command_facts,
                    persistence_effect=bind_facts,
                )
                _inject("after_fact_set_commit")
                self._advance(attempt_id, "fact_set_persisted", facts.fact_set_public_id)
                continue
            prepared = fact_set_bridge.prepare_receipt_calculation(
                self._conn,
                conversion["receipt_public_id"],
                actor_type="system",
                actor_id="independent-posting-owner",
            )
            if stage == "fact_set_persisted":
                self._begin()
                try:
                    self._accepted(attempt_id)
                    self._evidence(
                        self._conn,
                        attempt_id,
                        "snapshot",
                        prepared.calculation_snapshot_id,
                        prepared.calculation_snapshot_hash,
                        {
                            "snapshot_id": prepared.calculation_snapshot_id,
                            "snapshot_hash": prepared.calculation_snapshot_hash,
                        },
                    )
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
                _inject("after_snapshot_commit")
                self._advance(attempt_id, "snapshot_persisted", prepared.calculation_snapshot_id)
                continue
            actual = build_d2_receipt_projection(
                merchant=prepared.confirmed_receipt_identity.merchant,
                receipt_date=prepared.confirmed_receipt_identity.receipt_date,
                currency=prepared.currency,
                payer_participant_public_id=payer,
                calculation=prepared.calculation_result,
            )
            if canonical_json_text(actual) != canonical_json_text(projection):
                self._attention(attempt_id, "authoritative_projection_mismatch")
                raise PostingError("Actual receipt calculation differs from approved projection")

            def revalidate_receipt(connection, result):
                self._revalidate_pending_write(
                    connection,
                    attempt_id,
                    require_payer=getattr(result, "status", "") != "already_finalized",
                )

            authorization = fact_set_bridge.authorize_application_conditional_receipt_finalization(
                self._conn,
                prepared,
                attempt_id=attempt_id,
                persistence_effect=revalidate_receipt,
            )
            if stage == "snapshot_persisted":
                self._begin()
                try:
                    self._accepted(attempt_id)
                    self._evidence(
                        self._conn,
                        attempt_id,
                        "authorization",
                        authorization.authorization_id,
                        authorization.content_hash,
                        {
                            "authorization_id": authorization.authorization_id,
                            "content_hash": authorization.content_hash,
                        },
                    )
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
                _inject("after_conditional_authorization_commit")
                self._advance(
                    attempt_id,
                    "conditional_authorization_persisted",
                    authorization.authorization_id,
                )
                continue
            if stage == "conditional_authorization_persisted":
                final = fact_set_bridge.finalize_prepared_receipt(
                    self._conn, authorization, persistence_effect=revalidate_receipt
                )
                _inject("after_receipt_finalization_commit")
                self._advance(
                    attempt_id,
                    "finalized",
                    final.transaction_public_id,
                    final.transaction_public_id,
                )
                return
            raise PostingError("Receipt posting stage is unsupported")

    def get_status(self, attempt_id: str) -> PostingStatus:
        if not _posting_reference(attempt_id):
            raise PostingError("Attempt reference is invalid")
        require_staging_database(self._conn)
        with review_snapshot(self._conn):
            require_posting_schema(self._conn)
            row, material, decision = self._accepted(attempt_id)
            transaction = row["transaction_public_id"]
            if material["posting_path"] == "text":
                proposal_row = self._conn.execute(
                    "SELECT parser_output_id FROM application_posting_reviews WHERE review_id=?",
                    (row["review_id"],),
                ).fetchone()
                converted = self._conn.execute(
                    "SELECT 1 FROM parser_proposal_conversion_audit WHERE parser_output_id=?",
                    (proposal_row[0],),
                ).fetchone()
                if converted is not None:
                    result = decision_owner.verify_converted_parser_proposal(
                        self._conn, proposal_row[0]
                    )
                    transaction = result["transaction_public_id"]
                    self._verify_parser_audit(
                        material,
                        decision,
                        row["confirmation_public_id"],
                        "parser_proposal_converted",
                        transaction,
                    )
            else:
                conversion_id = "rpfc_application_" + _sha([attempt_id])[:24]
                receipt = self._conn.execute(
                    "SELECT receipts.public_id FROM receipt_proposal_conversions JOIN receipts "
                    "ON receipts.id=receipt_proposal_conversions.receipt_id "
                    "WHERE command_public_id=?",
                    (conversion_id,),
                ).fetchone()
                if receipt is not None:
                    authorization = self._conn.execute(
                        "SELECT authorization_id FROM application_conditional_authorization_proofs "
                        "WHERE attempt_id=?",
                        (attempt_id,),
                    ).fetchone()
                    if authorization is not None:
                        version = self._conn.execute(
                            "SELECT authorization_version FROM receipt_finalization_authorizations "
                            "WHERE authorization_id=?",
                            (authorization[0],),
                        ).fetchone()
                        if version is None or version[0] != "application_conditional_v1":
                            raise PostingError("Independent receipt authorization version changed")
                        loaded = fact_set_bridge.load_persisted_receipt_finalization_authorization(
                            self._conn,
                            authorization[0],
                        )
                        # Final result may have committed before coordination catchup.
                        result_row = self._conn.execute(
                            "SELECT audits.transaction_public_id "
                            "FROM receipt_finalization_idempotency AS replay "
                            "JOIN receipt_finalization_audit AS audits ON audits.finalization_id="
                            "replay.finalization_audit_id WHERE replay.idempotency_key=?",
                            (loaded.prepared.idempotency_key,),
                        ).fetchone()
                        if result_row is not None:
                            final = fact_set_bridge.verify_finalized_prepared_receipt(
                                self._conn, loaded.authorization_id
                            )
                            transaction = final.transaction_public_id
            if (
                transaction is not None
                and self._conn.execute(
                    "SELECT 1 FROM correction_targets AS targets "
                    "JOIN correction_versions AS versions "
                    "ON versions.target_id=targets.target_id WHERE targets.target_id=? LIMIT 1",
                    (transaction,),
                ).fetchone()
                is not None
            ):
                raise PostingError("Corrected transaction requires its effective correction reader")
            if row["stage"] == "finalized" and transaction != row["transaction_public_id"]:
                raise PostingError("Final posting status lost its verified canonical result")
            state = "finalized" if transaction is not None else row["stage"]
            return PostingStatus(attempt_id, state, transaction, row["attention_reason"])
