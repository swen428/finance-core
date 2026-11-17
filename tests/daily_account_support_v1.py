"""Trusted synthetic source/proposal/display/action port for daily tests."""

import hashlib
import json
from dataclasses import asdict

from finance_core.lifecycle_v2 import (
    VerifiedAction,
    VerifiedDelivery,
    VerifiedObservation,
    VerifiedSource,
)
from finance_core.lifecycle_v2.contracts import digest
from finance_core.lifecycle_v2.daily_v1.contracts import VerifiedDailyProposalV1

NOW = 1794877200


class DailyPort:
    def __init__(self):
        self.sources = {}
        self.commands = {}
        self.observations = {}
        self.movement_observations = {}
        self.movement_sources = {}
        self.movement_latest = {}
        self.movement_links = {}
        self.movement_link_origins = {}
        self.movement_questions = {}
        self.movement_delivered_questions = {}
        self.movement_latest_question = {}
        self.deliveries = {}
        self.actions = {}
        self.native_observations = {}
        self.native_raw = {}
        self.bad_actor = False
        self.on_action = None
        self.add_source("source1", "Create a MYR bank account with opening 12.30")

    def add_source(self, identity, text):
        source = VerifiedSource(
            identity,
            "user",
            "chat",
            "message-" + identity,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            NOW,
        )
        self.sources[identity] = source
        return source

    def source(self, conn, identity):
        return self.sources[identity]

    def daily_proposal(self, conn, identity, expected):
        if identity not in self.observations:
            self.observations[identity] = VerifiedDailyProposalV1(
                identity,
                expected.source_id,
                "user",
                "chat",
                digest(asdict(expected)),
                json.dumps(self.commands[identity]),
            )
        return self.observations[identity]

    def remember_movement_source(self, captured, evidence_id):
        # Called by the synthetic transport after a real capture, before Core
        # asks for a link. No expected callback context is used to manufacture it.
        self.movement_sources[captured.source_id] = {
            "source_ref": asdict(captured),
            "evidence_id": evidence_id,
        }

    def remember_movement_proposal(self, proposal):
        self.movement_latest[proposal.source_id] = {
            "proposal_id": proposal.proposal_id,
            "version": proposal.version,
            "proposal_sha256": proposal.proposal_sha256,
        }

    def _registered_target(self, binding, source_id, instruction_id, kind, fact_ref=None):
        from finance_core.lifecycle_v2.daily_v1 import movement_record_id_v1
        from finance_core.lifecycle_v2.daily_v1.contracts import instruction_ref

        registered = self.movement_sources[source_id]
        return {
            "binding": asdict(binding),
            "source_id": source_id,
            "source_sha256": registered["source_ref"]["source_sha256"],
            "source_instruction_ref": instruction_ref(
                asdict(self.sources[registered["evidence_id"]])
            ),
            "instruction_ref": instruction_ref(asdict(self.sources[instruction_id])),
            "record_id": fact_ref.record_id if fact_ref else movement_record_id_v1(source_id),
            "command_kind": kind,
            "current_proposal": self.movement_latest.get(source_id),
            "target_fact_ref": asdict(fact_ref) if fact_ref else None,
        }

    def register_movement_instruction(
        self, binding, source_id, instruction_id, kind="movement_create", fact_ref=None
    ):
        from finance_core.lifecycle_v2.contracts import canonical

        target = self._registered_target(binding, source_id, instruction_id, kind, fact_ref)
        initial = (
            target["current_proposal"] is None
            and fact_ref is None
            and target["instruction_ref"] == target["source_instruction_ref"]
        )
        if initial:
            association = {
                "evidence_id": instruction_id,
                "sha256": self.sources[instruction_id].original_sha256,
            }
        else:
            identity = "selection-" + instruction_id
            origin = {
                "format": "synthetic-explicit-target-selection-v1",
                "selected_target": target,
                "instruction_id": instruction_id,
            }
            self.movement_link_origins[identity] = origin
            association = {"evidence_id": identity, "sha256": digest(origin)}
        material = {
            "format": "finance-daily-movement-instruction-link-v1",
            "mode": "initial_instruction" if initial else "direct_targeted_instruction",
            "target": target,
            "association_ref": association,
            "question": None,
        }
        self.movement_links[instruction_id] = {
            "evidence_id": "instruction-link-" + instruction_id,
            "link_json": canonical(material),
        }

    def register_opening_question(
        self,
        binding,
        source_id,
        question_id,
        subject,
        *,
        version=1,
        kind="movement_create",
        fact_ref=None,
    ):
        from finance_core.lifecycle_v2.daily_v1.movement_contracts import question_target

        registered = self.movement_sources[source_id]
        target = question_target(
            self._registered_target(binding, source_id, registered["evidence_id"], kind, fact_ref)
        )
        delivery_id = "question-delivery-" + question_id + "-" + str(version)
        delivered = {
            "question_id": question_id,
            "question_version": version,
            "purpose": "opening_coverage",
            "target": target,
            "subject": subject,
        }
        self.movement_delivered_questions[delivery_id] = delivered
        question = {
            **delivered,
            "delivery_ref": {"evidence_id": delivery_id, "sha256": digest(delivered)},
        }
        self.movement_questions[question_id + ":" + str(version)] = question
        self.movement_latest_question[question_id] = version
        return question

    def register_opening_reply(self, question, instruction_id, *, actual_delivery_id=None):
        from finance_core.lifecycle_v2.contracts import canonical
        from finance_core.lifecycle_v2.daily_v1.contracts import instruction_ref

        identity = "reply-association-" + instruction_id
        origin = {
            "format": "synthetic-actual-question-reply-v1",
            "instruction_id": instruction_id,
            "instruction_sha256": self.sources[instruction_id].original_sha256,
            "question_id": question["question_id"],
            "question_version": question["question_version"],
            "delivery_evidence_id": actual_delivery_id or question["delivery_ref"]["evidence_id"],
            "current_version_at_reply": self.movement_latest_question[question["question_id"]],
        }
        self.movement_link_origins[identity] = origin
        material = {
            "format": "finance-daily-movement-instruction-link-v1",
            "mode": "question_reply",
            "target": {
                **question["target"],
                "instruction_ref": instruction_ref(asdict(self.sources[instruction_id])),
            },
            "association_ref": {"evidence_id": identity, "sha256": digest(origin)},
            "question": question,
        }
        self.movement_links[instruction_id] = {
            "evidence_id": "instruction-link-" + instruction_id,
            "link_json": canonical(material),
        }

    def movement_instruction_link(self, conn, identity, expected_target):
        from finance_core.lifecycle_v2 import LifecycleError
        from finance_core.lifecycle_v2.daily_v1 import VerifiedMovementInstructionLinkV1
        from finance_core.lifecycle_v2.daily_v1.contracts import strict_json

        # Lookup and independent original-relation verification only. In
        # particular, expected_target is never copied into an evidence result.
        value = self.movement_links[identity]
        link = strict_json(value["link_json"])
        if link["mode"] != "initial_instruction":
            reference = link["association_ref"]
            original = self.movement_link_origins[reference["evidence_id"]]
            if digest(original) != reference["sha256"]:
                raise LifecycleError("Synthetic actual association evidence missing or changed")
            if link["mode"] == "question_reply":
                question = link["question"]
                retained = self.movement_questions[
                    question["question_id"] + ":" + str(question["question_version"])
                ]
                delivered = self.movement_delivered_questions[original["delivery_evidence_id"]]
                if (
                    retained != question
                    or original["current_version_at_reply"] != question["question_version"]
                    or original["instruction_id"] != identity
                    or original["instruction_sha256"] != self.sources[identity].original_sha256
                    or question["delivery_ref"]["evidence_id"] != original["delivery_evidence_id"]
                    or digest(delivered) != question["delivery_ref"]["sha256"]
                ):
                    raise LifecycleError(
                        "Synthetic actual question/delivery/reply association mismatch"
                    )
            elif original["selected_target"] != link["target"]:
                raise LifecycleError("Synthetic actual selected target differs")
        return VerifiedMovementInstructionLinkV1(**value)

    def movement_proposal(self, conn, identity, expected):
        from finance_core.lifecycle_v2.daily_v1 import VerifiedMovementProposalV1

        if identity not in self.movement_observations:
            self.movement_observations[identity] = VerifiedMovementProposalV1(
                identity,
                expected.source_id,
                "user",
                "chat",
                digest(asdict(expected)),
                json.dumps(self.commands[identity]),
            )
        return self.movement_observations[identity]

    def provider(self, conn, identity, expected):
        if identity not in self.native_observations:
            self.native_observations[identity] = VerifiedObservation(
                identity,
                expected.source_id,
                "user",
                self.native_raw[identity],
                digest(asdict(expected)),
                expected.projection["reply_text"],
            )
        return self.native_observations[identity]

    def delivery(self, conn, identity, expected):
        if identity not in self.deliveries:
            self.deliveries[identity] = VerifiedDelivery(
                identity, expected.review_id, expected.review_sha256, "user", "chat"
            )
        return self.deliveries[identity]

    def action(self, conn, identity, expected):
        if self.on_action:
            self.on_action(conn, identity, expected)
        if identity not in self.actions:
            self.actions[identity] = VerifiedAction(
                identity,
                expected.review_id,
                expected.review_sha256,
                expected.delivery_evidence_id,
                "other" if self.bad_actor else "user",
                "chat",
            )
        return self.actions[identity]


def unknown_time():
    return {
        "format": "finance-daily-as-of-v1",
        "precision": "unknown",
        "timestamp": None,
        "local_date": None,
        "timezone": "Asia/Singapore",
    }


def command(kind="account_create", fields=None, reason_span=None):
    if fields is None:
        fields = {
            "name": "Daily",
            "account_type": "bank",
            "currency": "MYR",
            "opening": {"amount": "12.30", "as_of": unknown_time()},
        }
    return {
        "format": "finance-daily-command-v1",
        "command_kind": kind,
        "fields": fields,
        "reason_span": reason_span,
    }
