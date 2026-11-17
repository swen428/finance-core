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
