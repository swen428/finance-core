"""Local terminal decision and HMAC authority for controlled correction."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import sys
import time
import unicodedata
from collections.abc import Callable
from typing import TextIO

from finance_core.application.corrections import (
    ConsumptionSeal,
    CorrectionPlan,
    ExpectedDecision,
    ExpectedHistory,
    SignedDecision,
    TrustedApprovalBinding,
    VerifiedApprovalHistory,
    VerifiedDecision,
)
from finance_core.calculation.authoritative_snapshot import canonical_json_bytes

from .policy import LocalPolicy, LocalPolicyError, _read_policy, load_policy_for_connection
from .wire import (
    CorrectionWireError,
    authentic_consumption,
    authentic_decision,
    consumption_seal,
    decision_digest,
    decision_signature,
    require_epoch,
    require_hash,
)

RENDERER = "correction-terminal-v1"
_MAX_DECISION_TTL = 300


def _escaped(value: str | None) -> str:
    if value is None:
        return "(unset)"
    out: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs"} or char in {"\u2028", "\u2029"}:
            point = ord(char)
            out.append(f"\\u{point:04x}" if point <= 0xFFFF else f"\\U{point:08x}")
        else:
            out.append(char)
    return "".join(out)


def render_plan(plan: CorrectionPlan) -> bytes:
    """Reconstruct the entire stable human display from a neutral plan."""
    before = plan.before
    after = plan.after
    lines = [
        "Finance correction — review every field before confirming",
        f"Target: {_escaped(plan.target_id)}",
        f"Route: {_escaped(plan.route)}",
        f"Current version: {plan.expected_version}",
        f"Original source SHA-256: {_escaped(plan.source_hash)}",
        f"Before amount: {_escaped(before.amount)}",
        f"After amount: {_escaped(after.amount)}",
        f"Before currency: {_escaped(before.currency)}",
        f"After currency: {_escaped(after.currency)}",
        f"Before date: {_escaped(before.transaction_date)}",
        f"After date: {_escaped(after.transaction_date)}",
        f"Before merchant: {_escaped(before.merchant)}",
        f"After merchant: {_escaped(after.merchant)}",
        f"Complete reason: {_escaped(plan.reason)}",
    ]
    if plan.route == "receipt":
        if plan.receipt_json is None:
            raise LocalPolicyError("receipt plan lacks full receipt material")
        lines.extend(
            (
                f"Before receipt total: {_escaped(before.amount)} {_escaped(before.currency)}",
                f"After receipt total: {_escaped(after.amount)} {_escaped(after.currency)}",
                f"Before personal share: {_escaped(before.amount)} {_escaped(before.currency)}",
                f"After personal share: {_escaped(after.amount)} {_escaped(after.currency)}",
            )
        )
        lines.append(f"Complete receipt calculation: {_escaped(plan.receipt_json)}")
    lines.extend(
        (
            f"Plan ID: {_escaped(plan.plan_id)}",
            f"Plan expires at UTC epoch: {plan.expires_at_epoch}",
            "Original evidence is retained in the source record; use show for its full references.",
        )
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _plan_binding(plan: CorrectionPlan, policy: LocalPolicy) -> None:
    if (
        plan.actor != policy.actor
        or plan.realm != policy.realm
        or plan.key_id != policy.key_id
        or plan.instance_id != policy.instance_id
    ):
        raise LocalPolicyError("plan does not match local approval policy")


def _envelope_for(
    plan: CorrectionPlan,
    *,
    rendered: bytes,
    challenge: str,
    nonce: str,
    issued: int,
    expires: int,
) -> dict[str, object]:
    return {
        "schema": "correction-decision-v1",
        "authority_id": plan.authority_id,
        "key_id": plan.key_id,
        "realm": plan.realm,
        "instance_id": plan.instance_id,
        "actor": plan.actor,
        "target_id": plan.target_id,
        "expected_version": plan.expected_version,
        "predecessor_hash": plan.predecessor_hash,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "source_hash": plan.source_hash,
        "before_hash": plan.before_hash,
        "after_hash": plan.after_hash,
        "fact_hash": plan.fact_hash,
        "snapshot_id": plan.snapshot_id,
        "snapshot_hash": plan.snapshot_hash,
        "reason": plan.reason,
        "renderer": RENDERER,
        "display_sha256": hashlib.sha256(rendered).hexdigest(),
        "challenge": challenge,
        "issued_at_epoch": issued,
        "expires_at_epoch": expires,
        "nonce": nonce,
    }


class LocalApprovalAuthority:
    """One configured OS-owner policy; no request-selected key or actor."""

    def __init__(self, *, clock: Callable[[], int] = lambda: int(time.time())) -> None:
        self._clock = clock

    def current_binding(
        self, connection: sqlite3.Connection, expected_actor: str
    ) -> TrustedApprovalBinding:
        policy = load_policy_for_connection(connection)
        if type(expected_actor) is not str or expected_actor != policy.actor:
            raise LocalPolicyError("original authenticated actor is not the local owner")
        return TrustedApprovalBinding(
            actor=policy.actor,
            key_id=policy.key_id,
            realm=policy.realm,
            instance_id=policy.instance_id,
        )

    def sign_with_terminal(
        self,
        plan: CorrectionPlan,
        *,
        input_stream: TextIO = sys.stdin,
        output_stream: TextIO = sys.stdout,
    ) -> SignedDecision:
        """Show one complete plan and require plan-specific fresh challenge input."""
        if not input_stream.isatty() or not output_stream.isatty():
            raise LocalPolicyError("confirmation requires an input and output terminal")
        policy = _read_policy()
        _plan_binding(plan, policy)
        rendered = render_plan(plan)
        now = require_epoch(self._clock(), "terminal decision time")
        if now >= plan.expires_at_epoch:
            raise LocalPolicyError("correction plan expired")
        challenge = secrets.token_hex(32)
        nonce = secrets.token_hex(32)
        expected_response = f"CONFIRM {plan.plan_id} {challenge}"
        output_stream.write(rendered.decode("utf-8"))
        output_stream.write(f"\nType exactly: {expected_response}\n> ")
        output_stream.flush()
        response = input_stream.readline(4096)
        if response.rstrip("\r\n") != expected_response:
            raise LocalPolicyError("terminal confirmation did not match plan and challenge")
        issued = require_epoch(self._clock(), "decision issuance time")
        if issued > plan.expires_at_epoch:
            raise LocalPolicyError("correction plan expired before decision")
        expires = min(plan.expires_at_epoch, issued + _MAX_DECISION_TTL)
        envelope = _envelope_for(
            plan,
            rendered=rendered,
            challenge=challenge,
            nonce=nonce,
            issued=issued,
            expires=expires,
        )
        signature = decision_signature(policy.key, envelope)
        return SignedDecision(canonical_json_bytes(envelope).decode("utf-8"), signature)

    def verify_fresh(
        self, signed_decision: SignedDecision, expected_decision: ExpectedDecision
    ) -> VerifiedDecision:
        if (
            type(signed_decision) is not SignedDecision
            or type(expected_decision) is not ExpectedDecision
        ):
            raise CorrectionWireError("typed decision evidence is required")
        policy = _read_policy()
        plan = expected_decision.plan
        _plan_binding(plan, policy)
        envelope = authentic_decision(
            policy.key, signed_decision.envelope_json, signed_decision.signature
        )
        now = require_epoch(self._clock(), "decision verification time")
        issued = require_epoch(envelope["issued_at_epoch"], "decision issuance time")
        expires = require_epoch(envelope["expires_at_epoch"], "decision expiry")
        plan_expiry = require_epoch(expected_decision.plan_expires_at_epoch, "plan expiry")
        if (
            plan_expiry != plan.expires_at_epoch
            or not (issued <= now <= expires <= plan_expiry)
            or expires - issued > _MAX_DECISION_TTL
        ):
            raise CorrectionWireError("decision is stale or outside plan lifetime")
        challenge = require_hash(envelope["challenge"], "challenge")
        nonce = require_hash(envelope["nonce"], "nonce")
        expected = _envelope_for(
            plan,
            rendered=render_plan(plan),
            challenge=challenge,
            nonce=nonce,
            issued=issued,
            expires=expires,
        )
        if envelope != expected:
            raise CorrectionWireError("decision does not match the persisted plan display")
        digest = decision_digest(envelope, signed_decision.signature)
        return VerifiedDecision(
            envelope=envelope,
            decision_digest=digest,
            checked_at_epoch=now,
            correction_id=plan.correction_id,
        )

    def seal_consumption(
        self, verified_decision: VerifiedDecision, result_core_hash: str
    ) -> ConsumptionSeal:
        if type(verified_decision) is not VerifiedDecision:
            raise CorrectionWireError("typed verified decision is required")
        require_hash(result_core_hash, "result core hash")
        policy = _read_policy()
        envelope = verified_decision.envelope
        checked = require_epoch(verified_decision.checked_at_epoch, "checked time")
        now = require_epoch(self._clock(), "consumption sealing time")
        if now > require_epoch(envelope["expires_at_epoch"], "decision expiry") or now < checked:
            raise CorrectionWireError("decision expired before consumption was sealed")
        if (
            envelope["key_id"] != policy.key_id
            or envelope["realm"] != policy.realm
            or envelope["instance_id"] != policy.instance_id
            or envelope["actor"] != policy.actor
        ):
            raise CorrectionWireError("decision policy identity changed")
        material: dict[str, object] = {
            "schema": "correction-consumption-v1",
            "key_id": policy.key_id,
            "realm": policy.realm,
            "actor": policy.actor,
            "target_id": envelope["target_id"],
            "instance_id": policy.instance_id,
            "authority_id": envelope["authority_id"],
            "plan_id": envelope["plan_id"],
            "correction_id": verified_decision.correction_id,
            "nonce": envelope["nonce"],
            "decision_digest": verified_decision.decision_digest,
            "result_core_hash": result_core_hash,
            "checked_at_epoch": checked,
        }
        return ConsumptionSeal(
            material_json=canonical_json_bytes(material).decode("utf-8"),
            seal=consumption_seal(policy.key, material),
        )

    def verify_history(
        self,
        decision: SignedDecision,
        consumption: ConsumptionSeal,
        expected_history: ExpectedHistory,
    ) -> VerifiedApprovalHistory:
        if (
            type(decision) is not SignedDecision
            or type(consumption) is not ConsumptionSeal
            or type(expected_history) is not ExpectedHistory
        ):
            raise CorrectionWireError("typed historical evidence is required")
        policy = _read_policy()
        plan = expected_history.plan
        _plan_binding(plan, policy)
        envelope = authentic_decision(policy.key, decision.envelope_json, decision.signature)
        material = authentic_consumption(policy.key, consumption.material_json, consumption.seal)
        checked = require_epoch(material["checked_at_epoch"], "sealed decision time")
        issued = require_epoch(envelope["issued_at_epoch"], "historical issuance")
        expires = require_epoch(envelope["expires_at_epoch"], "historical expiry")
        if (
            not (issued <= checked <= expires <= plan.expires_at_epoch)
            or expires - issued > _MAX_DECISION_TTL
        ):
            raise CorrectionWireError("historical decision lifetime is invalid")
        if checked != expected_history.checked_at_epoch:
            raise CorrectionWireError("historical apply time changed")
        expected_envelope = _envelope_for(
            plan,
            rendered=render_plan(plan),
            challenge=require_hash(envelope["challenge"], "challenge"),
            nonce=require_hash(envelope["nonce"], "nonce"),
            issued=issued,
            expires=expires,
        )
        if envelope != expected_envelope:
            raise CorrectionWireError("historical display or plan binding changed")
        digest = decision_digest(envelope, decision.signature)
        expected_material = {
            "schema": "correction-consumption-v1",
            "key_id": plan.key_id,
            "realm": plan.realm,
            "actor": plan.actor,
            "target_id": plan.target_id,
            "instance_id": plan.instance_id,
            "authority_id": plan.authority_id,
            "plan_id": plan.plan_id,
            "correction_id": plan.correction_id,
            "nonce": envelope["nonce"],
            "decision_digest": digest,
            "result_core_hash": require_hash(expected_history.result_core_hash, "result core hash"),
            "checked_at_epoch": checked,
        }
        if material != expected_material:
            raise CorrectionWireError("historical consumption material changed")
        return VerifiedApprovalHistory(decision_digest=digest, checked_at_epoch=checked)
