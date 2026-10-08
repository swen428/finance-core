"""Independent pre-confirmation six-field edits using financial owning operations."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from finance_core.application.admission import (
    SourceVerifier,
    VerifiedSource,
    _digest,
    _positive_int,
    _reference,
    validate_verified_source,
)
from finance_core.application.amendment_contract import (
    AMENDMENT_REVIEW_SCHEMA,
    AMENDMENT_SCHEMA,
    AmendmentBinding,
    AmendmentError,
    AmendmentResult,
    AmendmentReview,
    ExpectedHumanAmendment,
    HumanAmendmentAuthority,
    VerifiedHumanAmendment,
    amendment_patch_sha256,
    amendment_review_sha256,
    require_amendment_schema,
)
from finance_core.application.review import get_proposal_review, review_snapshot
from finance_core.calculation.authoritative_snapshot import (
    canonical_json_text,
    canonical_json_value,
)
from finance_core.money import money_decimal, normalize_currency
from finance_core.parser_proposals.amendment_lineage import (
    require_independent_source_edit_history,
    sha,
    verify_amendment_record,
    verify_independent_amendment_descendant,
)
from finance_core.parser_proposals.completion import (
    _validate_and_canonicalize_field_updates,
    complete_proposal,
)
from finance_core.parser_proposals.content_hash import (
    canonicalize_proposal_money,
    compute_effective_proposal_content_hash,
)
from finance_core.parser_proposals.conversion_state import has_receipt_ocr_proposal_link
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.receipt_supersession import supersede_receipt_total_proposal
from finance_core.parser_proposals.repository import ParserProposalRepository
from finance_core.parser_proposals.text_supersession import supersede_text_proposal
from finance_core.sqlite_connection import require_foreign_keys_enabled
from finance_core.staging_guard import require_staging_database

EDITABLE_FIELDS = frozenset(
    {"amount", "currency", "transaction_date", "merchant", "description", "category"}
)
_failure_injection_hook: Callable[[str], None] | None = None


def _inject(stage: str) -> None:
    if _failure_injection_hook is not None:
        _failure_injection_hook(stage)


def _source_key(source: VerifiedSource) -> str:
    return sha([source.namespace, source.instance_id, source.source_event_id])


def canonicalize_amendment_patch(
    patch: Mapping[str, object], payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(patch, Mapping) or not patch or set(patch) - EDITABLE_FIELDS:
        raise AmendmentError("Only the six supported nonempty human edit fields are allowed")
    try:
        nonmoney = {k: v for k, v in patch.items() if k not in {"amount", "currency"}}
        canonical = _validate_and_canonicalize_field_updates(nonmoney) if nonmoney else {}
        for key, value in canonical.items():
            if len(value) > 1024 or any(ord(c) < 32 for c in value):
                raise AmendmentError("Edit values must be complete bounded printable text")
            canonical[key] = unicodedata.normalize("NFC", value)
        if set(patch) & {"amount", "currency"}:
            currency_value = patch.get("currency", payload.get("currency"))
            if not isinstance(currency_value, str):
                raise AmendmentError("A monetary edit requires a valid resulting currency")
            currency = normalize_currency(currency_value)
            amount = canonicalize_proposal_money(
                patch.get("amount", payload.get("amount")), currency
            )
            if "amount" in patch:
                canonical["amount"] = amount
            if "currency" in patch:
                canonical["currency"] = currency
        material = {}
        for key, value in canonical.items():
            old = payload.get(key, payload.get("date") if key == "transaction_date" else None)
            if key == "amount" and old is not None:
                try:
                    if money_decimal(old) == money_decimal(value):
                        continue
                except ValueError:
                    pass
            if key == "currency" and isinstance(old, str):
                try:
                    old = normalize_currency(old)
                except ValueError:
                    pass
            if old != value:
                material[key] = value
        if not material:
            raise AmendmentError("The supplied patch has no material change")
        return canonical, material
    except (TypeError, ValueError) as exc:
        if isinstance(exc, AmendmentError):
            raise
        raise AmendmentError("Whole edit patch validation failed") from exc


class _PublicationAuthority:
    def __init__(
        self,
        service: AmendmentService,
        review_id: str,
        evidence_id: str,
        amendment_id: str,
        canonical: dict[str, Any],
        applied: dict[str, Any],
        kind: str,
        publication_id: str,
    ) -> None:
        self.service, self.review_id, self.evidence_id, self.amendment_id = (
            service,
            review_id,
            evidence_id,
            amendment_id,
        )
        self.canonical, self.applied, self.kind, self.publication_id = (
            canonical,
            applied,
            kind,
            publication_id,
        )
        self.accepted: VerifiedHumanAmendment | None = None
        self.review: dict[str, Any] | None = None
        self.now = 0

    def verify_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        persisted_operation: dict[str, Any] | None,
        proposal: dict[str, Any],
        canonical_patch: dict[str, Any],
    ) -> None:
        require_amendment_schema(connection)
        require_foreign_keys_enabled(connection)
        if connection is not self.service._conn or not connection.in_transaction:
            raise AmendmentError("Publication authority requires the financial owner's snapshot")
        expected_patch = self.canonical if self.kind == "receipt_supersession" else self.applied
        if canonical_patch != expected_patch:
            raise AmendmentError("Owner patch differs from signed canonical patch")
        row = connection.execute(
            "SELECT * FROM application_amendment_records WHERE amendment_id=?", (self.amendment_id,)
        ).fetchone()
        if (row is None) != (persisted_operation is None):
            raise AmendmentError("Publication and independent consumption disagree")
        if row is not None:
            if (
                row["review_id"] != self.review_id
                or row["evidence_id"] != self.evidence_id
                or row["publication_public_id"] != self.publication_id
                or row["publication_kind"] != self.kind
            ):
                raise AmendmentError("Amendment replay identity conflict")
            material = self.service._accepted(row)
            if material["canonical_patch"] != self.canonical:
                raise AmendmentError("Amendment evidence changed on replay")
            return
        self.now = self.service._now()
        self.review = self.service._read_review(self.review_id)
        view = self.review["proposal_review"]
        if proposal["public_id"] != view["proposal_public_id"]:
            raise AmendmentError("Publication targets another reviewed proposal")
        current = self.service._prepare_material(
            proposal["public_id"], self.review["created_at"], self.review["expires_at"]
        )
        if current != self.review or self.now >= self.review["expires_at"]:
            raise AmendmentError("Old amendment review is stale or expired")
        self.accepted = self.service._proof(
            self.review_id, self.review, self.evidence_id, self.now, None
        )
        checked, applied = canonicalize_amendment_patch(
            self.accepted.patch, json.loads(self.review["effective_payload_json"])
        )
        if checked != self.canonical or applied != self.applied:
            raise AmendmentError("Durable edit patch changed before publication")
        _inject("after_authority_verification")

    def persist_effect_in_transaction(
        self, connection: sqlite3.Connection, *, publication_result: dict[str, Any]
    ) -> None:
        row = connection.execute(
            "SELECT * FROM application_amendment_records WHERE amendment_id=?", (self.amendment_id,)
        ).fetchone()
        if row is not None:
            self.service._accepted(row)
            return
        if self.accepted is None or self.review is None:
            raise AmendmentError("Publication has no in-transaction accepted proof")
        base = self.review["proposal_review"]
        base_proposal = ParserProposalRepository(connection).get_by_public_id(
            base["proposal_public_id"]
        )
        assert base_proposal is not None
        result_id = publication_result.get("replacement_parser_output_id", base_proposal["id"])
        result = ParserProposalRepository(connection).get_lineage_row_by_id(result_id)
        assert result is not None
        result_payload, _, version = resolve_effective_payload(connection, result)
        result_hash = compute_effective_proposal_content_hash(connection, result)
        material = {
            "schema": AMENDMENT_SCHEMA,
            "review_hash": amendment_review_sha256(self.review),
            "canonical_patch": self.canonical,
            "material_patch": self.applied,
            "patch_digest": amendment_patch_sha256(self.canonical),
            "material_patch_digest": amendment_patch_sha256(self.applied),
            "accepted_proof": dataclasses.asdict(self.accepted),
            "accepted_at": self.now,
            "base_payload_json": self.review["effective_payload_json"],
            "base_status": base["parse_status"],
            "base_version": base["proposal_version"],
            "result_payload_json": json.dumps(
                result_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ),
            "resulting_version": version,
            "publication_kind": self.kind,
            "publication_public_id": self.publication_id,
        }
        _inject("before_amendment_seal")
        connection.execute(
            "INSERT INTO application_amendment_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.amendment_id,
                self.service._binding.amendment_namespace,
                self.evidence_id,
                self.review_id,
                base_proposal["id"],
                base["proposal_version"],
                base["effective_content_hash"],
                result_id,
                version,
                result_hash,
                self.kind,
                self.publication_id,
                _source_key(VerifiedSource(**self.review["source"])),
                self.now,
                canonical_json_text(material),
                sha(material),
            ),
        )
        connection.execute(
            "INSERT INTO application_amendment_invalidations VALUES (?,?,?,?)",
            (
                self.amendment_id,
                base_proposal["id"],
                base["proposal_version"],
                base["effective_content_hash"],
            ),
        )
        row = connection.execute(
            "SELECT * FROM application_amendment_records WHERE amendment_id=?", (self.amendment_id,)
        ).fetchone()
        assert row is not None
        verify_amendment_record(connection, row)
        _inject("before_commit")


class AmendmentService:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        source_verifier: SourceVerifier,
        human_amendment_authority: HumanAmendmentAuthority,
        binding: AmendmentBinding,
        clock: Callable[[], int],
    ) -> None:
        if type(binding) is not AmendmentBinding or any(
            not _reference(v) or unicodedata.normalize("NFC", v) != v
            for v in [
                *vars(binding.binding).values(),
                binding.amendment_namespace,
                binding.amendment_key_id,
            ]
        ):
            raise AmendmentError("Fixed amendment binding is malformed")
        self._conn, self._source_port, self._authority, self._binding, self._clock = (
            connection,
            source_verifier,
            human_amendment_authority,
            binding,
            clock,
        )

    def _now(self) -> int:
        now = self._clock()
        if not _positive_int(now):
            raise AmendmentError("Amendment clock is unavailable")
        return now

    def _source(self, intake: str) -> VerifiedSource:
        return validate_verified_source(
            self._source_port.verify_persisted(self._conn, intake),
            binding=self._binding.binding,
            intake_public_id=intake,
            now=self._now(),
        )

    def _prepare_material(self, proposal_id: str, created: int, expires: int) -> dict[str, Any]:
        proposal = ParserProposalRepository(self._conn).get_by_public_id(proposal_id)
        if proposal is None or proposal["parse_status"] not in {
            "parsed_pending_confirmation",
            "edited_pending_confirmation",
        }:
            raise AmendmentError("Only a current unconfirmed proposal can be edited")
        view = get_proposal_review(self._conn, proposal_id)
        payload, _, version = resolve_effective_payload(self._conn, proposal)
        lineage = verify_independent_amendment_descendant(
            self._conn,
            proposal,
            content_hash=view["effective_content_hash"],
            proposal_version=version,
        )
        if (
            view["classification"] != "personal"
            or view["account_status"] != "absent"
            or (
                payload.get("intent") not in {None, "personal_expense_log", "simple_expense_log"}
                and not (
                    payload.get("intent") == "personal_expense"
                    and view["proposal_origin"] == "ai_fallback"
                )
            )
        ):
            raise AmendmentError(
                "Amendments support personal expenses with unspecified account only"
            )
        if lineage is None and (
            version != 0 or proposal["parse_status"] != "parsed_pending_confirmation"
        ):
            raise AmendmentError("Unsealed legacy edited history cannot gain independent authority")
        require_independent_source_edit_history(self._conn, proposal)
        intake = self._conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
            (view["intake_public_id"],),
        ).fetchone()
        if intake is None or intake[0] != proposal["id"]:
            raise AmendmentError("Proposal is not the current unique intake leaf")
        for table in (
            "parser_proposal_authorizations",
            "parser_proposal_conversion_audit",
            "receipt_proposal_conversions",
        ):
            if (
                self._conn.execute(
                    f"SELECT 1 FROM {table} AS evidence JOIN parser_outputs AS p ON "
                    f"p.id=evidence.parser_output_id WHERE p.source_public_id=? LIMIT 1",
                    (proposal["source_public_id"],),
                ).fetchone()
                is not None
            ):
                raise AmendmentError("An event with confirmation or conversion cannot be edited")
        if (
            self._conn.execute(
                "SELECT 1 FROM application_posting_attempts WHERE intake_public_id=?",
                (view["intake_public_id"],),
            ).fetchone()
            is not None
        ):
            raise AmendmentError("Accepted posting blocks all subsequent pre-confirmation edits")
        if (
            self._conn.execute(
                "SELECT 1 FROM parser_human_drafts AS d JOIN parser_outputs AS p ON "
                "p.id=d.decision_target_parser_output_id WHERE p.source_public_id=?",
                (proposal["source_public_id"],),
            ).fetchone()
            is not None
        ):
            raise AmendmentError("D1 draft/publication requires its original owner")
        editable = {
            field: payload.get(field, payload.get("date") if field == "transaction_date" else None)
            for field in sorted(EDITABLE_FIELDS)
        }
        for value in editable.values():
            if isinstance(value, str) and (len(value) > 1024 or any(ord(c) < 32 for c in value)):
                raise AmendmentError("An amendment requires the complete bounded old display")
            if value is not None and not isinstance(value, (str, int)):
                raise AmendmentError("The old editable value has no safe exact display")
        source = self._source(view["intake_public_id"])
        material = {
            "schema": AMENDMENT_REVIEW_SCHEMA,
            "binding": dataclasses.asdict(self._binding),
            "source": dataclasses.asdict(source),
            "proposal_review": view,
            "editable_values": editable,
            "effective_payload_json": json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            ),
            "created_at": created,
            "expires_at": expires,
        }
        normalized = canonical_json_value(canonical_json_text(material), label="amendment review")
        assert isinstance(normalized, dict)
        return normalized

    def prepare(self, proposal_public_id: str) -> AmendmentReview:
        if not _reference(proposal_public_id):
            raise AmendmentError("Proposal reference is invalid")
        require_staging_database(self._conn)
        require_foreign_keys_enabled(self._conn)
        if self._conn.in_transaction:
            raise AmendmentError("Prepare requires a connection without caller work")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            require_amendment_schema(self._conn)
            now = self._now()
            material = self._prepare_material(proposal_public_id, now, now + 900)
            digest = amendment_review_sha256(material)
            review_id = "aer_" + digest
            existing = self._conn.execute(
                "SELECT * FROM application_amendment_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            if existing is None:
                proposal = ParserProposalRepository(self._conn).get_by_public_id(proposal_public_id)
                assert proposal is not None
                source = VerifiedSource(**material["source"])
                view = material["proposal_review"]
                self._conn.execute(
                    "INSERT INTO application_amendment_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        review_id,
                        proposal["id"],
                        source.intake_public_id,
                        _source_key(source),
                        view["proposal_version"],
                        view["effective_content_hash"],
                        canonical_json_text(material),
                        digest,
                        now,
                        now + 900,
                    ),
                )
            elif self._read_review(review_id) != material:
                raise AmendmentError("Review identity conflict")
            self._conn.commit()
            return AmendmentReview(review_id, digest, material, now + 900)
        except ValueError as exc:
            self._conn.rollback()
            if isinstance(exc, AmendmentError):
                raise
            raise AmendmentError("Amendment target/source is unavailable or contradictory") from exc
        except BaseException:
            self._conn.rollback()
            raise

    def _read_review(self, review_id: str) -> dict[str, Any]:
        require_amendment_schema(self._conn)
        row = self._conn.execute(
            "SELECT * FROM application_amendment_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise AmendmentError("Amendment review is absent")
        value = canonical_json_value(row["material_json"], label="amendment review")
        if (
            not isinstance(value, dict)
            or canonical_json_text(value) != row["material_json"]
            or amendment_review_sha256(value) != row["review_hash"]
            or value["binding"] != dataclasses.asdict(self._binding)
        ):
            raise AmendmentError("Immutable review or trusted binding changed")
        return value

    def _proof(
        self,
        review_id: str,
        review: dict[str, Any],
        evidence_id: str,
        checked: int,
        accepted: str | None,
    ) -> VerifiedHumanAmendment:
        source = self._source(review["source"]["intake_public_id"])
        if dataclasses.asdict(source) != review["source"]:
            raise AmendmentError("Reviewed source proof changed")
        view = review["proposal_review"]
        expected = ExpectedHumanAmendment(
            self._binding,
            source,
            review_id,
            review,
            amendment_review_sha256(review),
            view["proposal_public_id"],
            view["proposal_version"],
            view["effective_content_hash"],
            checked,
            accepted,
        )
        proof = self._authority.verify_persisted(self._conn, evidence_id, expected)
        b = self._binding.binding
        if (
            type(proof) is not VerifiedHumanAmendment
            or proof.schema != AMENDMENT_SCHEMA
            or proof.namespace != self._binding.amendment_namespace
            or proof.key_id != self._binding.amendment_key_id
            or proof.instance_id != b.instance_id
            or proof.human_principal_id != b.human_principal_id
            or proof.evidence_id != evidence_id
            or proof.source_evidence_id != source.evidence_id
            or proof.source_evidence_digest != source.evidence_digest
            or proof.intake_public_id != source.intake_public_id
            or proof.source_event_id != source.source_event_id
            or proof.proposal_public_id != view["proposal_public_id"]
            or type(proof.proposal_version) is not int
            or proof.proposal_version != view["proposal_version"]
            or proof.proposal_content_hash != view["effective_content_hash"]
            or proof.review_id != review_id
            or proof.review_projection_hash != expected.review_projection_hash
            or proof.action != "amend"
            or not _reference(proof.decision_id)
            or not _reference(proof.display_id)
            or proof.reply_to_display_id != proof.display_id
            or any(
                not _digest(v)
                for v in [
                    proof.display_evidence_digest,
                    proof.reply_evidence_digest,
                    proof.evidence_digest,
                    proof.patch_digest,
                ]
            )
            or proof.revoked is not False
            or type(proof.consumed) is not bool
            or any(
                not _positive_int(v) for v in [proof.observed_at, proof.issued_at, proof.expires_at]
            )
            or not review["created_at"]
            <= proof.observed_at
            <= proof.issued_at
            <= checked
            < proof.expires_at
            or accepted is None
            and proof.consumed
        ):
            raise AmendmentError(
                "Durable amendment display/reply authority does not match its full review"
            )
        canonical, _ = canonicalize_amendment_patch(
            proof.patch, json.loads(review["effective_payload_json"])
        )
        if proof.patch_digest != amendment_patch_sha256(canonical):
            raise AmendmentError("Durable whole patch digest does not match canonical values")
        return dataclasses.replace(proof, patch=canonical)

    def _accepted(self, row: sqlite3.Row) -> dict[str, Any]:
        material = verify_amendment_record(self._conn, row)
        review = self._read_review(row["review_id"])
        proof = self._proof(
            row["review_id"], review, row["evidence_id"], row["accepted_at"], row["amendment_id"]
        )
        persisted = dict(material["accepted_proof"])
        current = dataclasses.asdict(proof)
        # Consumption alone may change after acceptance; the signed immutable
        # accepted-time material and revocation remain required by the port.
        current["consumed"] = persisted["consumed"]
        if current != persisted:
            raise AmendmentError("Accepted durable amendment authority changed")
        proposal = ParserProposalRepository(self._conn).get_lineage_row_by_id(
            row["resulting_parser_output_id"]
        )
        assert proposal is not None
        payload, _, version = resolve_effective_payload(self._conn, proposal)
        verify_independent_amendment_descendant(
            self._conn,
            proposal,
            content_hash=compute_effective_proposal_content_hash(self._conn, proposal),
            proposal_version=version,
        )
        # Historical authority still requires the original parser/AI proof.
        # Read the actual current leaf so later legitimate edits can replay
        # without passing a historical subject as current AI custody.
        current = self._conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
            (proposal["source_public_id"],),
        ).fetchone()
        if current is None:
            raise AmendmentError("Accepted amendment source leaf is absent")
        leaf = ParserProposalRepository(self._conn).get_lineage_row_by_id(current[0])
        if leaf is None:
            raise AmendmentError("Accepted amendment current source proposal is absent")
        from finance_core.parser_proposals.ai_fallback import (
            AiFallbackServiceError,
            verify_ai_fallback_child,
        )

        try:
            verify_ai_fallback_child(
                self._conn,
                leaf,
                content_hash=compute_effective_proposal_content_hash(self._conn, leaf),
                proposal_version=resolve_effective_payload(self._conn, leaf)[2],
                require_resolved=False,
            )
        except AiFallbackServiceError as exc:
            raise AmendmentError(
                "Accepted amendment AI source evidence no longer verifies"
            ) from exc
        return material

    def amend(
        self, review_id: str, amendment_evidence_id: str, amendment_id: str
    ) -> AmendmentResult:
        if any(not _reference(v) for v in [review_id, amendment_evidence_id, amendment_id]):
            raise AmendmentError("Amendment references are invalid")
        if self._conn.in_transaction:
            raise AmendmentError("Amend requires a connection without caller work")
        try:
            with review_snapshot(self._conn):
                review = self._read_review(review_id)
                existing = self._conn.execute(
                    "SELECT * FROM application_amendment_records WHERE amendment_id=?",
                    (amendment_id,),
                ).fetchone()
                checked = self._now() if existing is None else existing["accepted_at"]
                proof = self._proof(
                    review_id,
                    review,
                    amendment_evidence_id,
                    checked,
                    None if existing is None else amendment_id,
                )
                canonical, applied = canonicalize_amendment_patch(
                    proof.patch, json.loads(review["effective_payload_json"])
                )
                proposal = ParserProposalRepository(self._conn).get_by_public_id(
                    review["proposal_review"]["proposal_public_id"]
                )
                if proposal is None:
                    raise AmendmentError("Reviewed base proposal is absent")
                kind = "completion"
                if set(applied) & {"amount", "currency"}:
                    kind = (
                        "receipt_supersession"
                        if has_receipt_ocr_proposal_link(self._conn, proposal["id"])
                        else "text_supersession"
                    )
                prefix = {
                    "completion": "pco_",
                    "receipt_supersession": "rcor_",
                    "text_supersession": "apub_",
                }[kind]
                public_id = prefix + sha([self._binding.amendment_namespace, amendment_id])
            authority = _PublicationAuthority(
                self,
                review_id,
                amendment_evidence_id,
                amendment_id,
                canonical,
                applied,
                kind,
                public_id,
            )

            def clock() -> str:
                return datetime.fromtimestamp(self._now(), UTC).isoformat()

            kwargs = {
                "actor": self._binding.binding.human_principal_id,
                "expected_content_hash": review["proposal_review"]["effective_content_hash"],
                "clock": clock,
                "amendment_authority": authority,
            }
            if kind == "completion":
                complete_proposal(
                    self._conn,
                    proposal["id"],
                    field_updates=applied,
                    completion_public_id=public_id,
                    completion_channel="independent_application",
                    **kwargs,
                )
            elif kind == "receipt_supersession":
                supersede_receipt_total_proposal(
                    self._conn,
                    proposal["id"],
                    field_updates=canonical,
                    correction_public_id=public_id,
                    correction_channel="independent_application",
                    **kwargs,
                )
            else:
                supersede_text_proposal(
                    self._conn,
                    proposal["id"],
                    field_updates=applied,
                    publication_public_id=public_id,
                    amendment_id=amendment_id,
                    **kwargs,
                )
            _inject("after_commit")
            return self.get_status(amendment_id)
        except (ValueError, sqlite3.Error) as exc:
            if self._conn.in_transaction:
                self._conn.rollback()
            if isinstance(exc, AmendmentError):
                raise
            raise AmendmentError("Independent amendment publication refused") from exc
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def get_status(self, amendment_id: str) -> AmendmentResult:
        if not _reference(amendment_id):
            raise AmendmentError("Amendment reference is invalid")
        try:
            with review_snapshot(self._conn):
                row = self._conn.execute(
                    "SELECT * FROM application_amendment_records WHERE amendment_id=?",
                    (amendment_id,),
                ).fetchone()
                if row is None:
                    raise AmendmentError("Amendment result is absent")
                self._accepted(row)
                proposal = ParserProposalRepository(self._conn).get_lineage_row_by_id(
                    row["resulting_parser_output_id"]
                )
                assert proposal is not None
                current = self._conn.execute(
                    "SELECT parser_output_id FROM raw_intake_records WHERE public_id=?",
                    (proposal["source_public_id"],),
                ).fetchone()
                _, _, version = resolve_effective_payload(self._conn, proposal)
                is_current = (
                    current is not None
                    and current[0] == proposal["id"]
                    and version == row["resulting_version"]
                    and compute_effective_proposal_content_hash(self._conn, proposal)
                    == row["resulting_content_hash"]
                )
                return AmendmentResult(
                    amendment_id,
                    proposal["public_id"],
                    row["resulting_version"],
                    row["resulting_content_hash"],
                    row["publication_kind"],
                    row["publication_public_id"],
                    is_current,
                )
        except (ValueError, sqlite3.Error) as exc:
            if isinstance(exc, AmendmentError):
                raise
            raise AmendmentError("Historical amendment proof does not verify") from exc
