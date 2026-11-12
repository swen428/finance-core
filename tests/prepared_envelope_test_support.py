"""Actual synthetic lifecycle preparation shared by content/envelope proofs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from lifecycle_recovery_material_support_v1 import make_service, stage_expense

from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2.prepared_finalization_v1 import (
    PreparedFinalizationServiceV1,
    PreparedFinalizationV1,
)
from finance_core.lifecycle_v2.service import LedgerService


@dataclass
class PreparedEnvelopeCase:
    service: LedgerService
    port: Any
    config: Any
    authenticator: Any
    preparation: PreparedFinalizationServiceV1
    reference: PreparedFinalizationV1
    review: Any


@contextmanager
def prepared_case(
    root,
    environment,
    *,
    key="expense",
    operation_id="finalize",
    previous_packages=(),
    envelope_padding=b"",
):
    """Acquire real test evidence and prepare through the guarded service.

    Padding belongs to each acquired synthetic raw envelope, before its archive
    and HMAC are fixed. It exercises real large content without fake checkers.
    The external test root is never taken from a package.
    """
    service, port, config, authenticator = make_service(root, environment)
    try:
        port._envelope_padding = envelope_padding
        _, _, review, _ = stage_expense(service, port, key, stage="delivery")
        port.register_action(service, review, "delivery-" + key, "action-" + key)
        preparation = PreparedFinalizationServiceV1(
            service, archive_source=port, authenticator=authenticator
        )
        reference = preparation.prepare(
            review.review_id,
            "action-" + key,
            operation_id,
            previous_packages=previous_packages,
        )
        yield PreparedEnvelopeCase(
            service, port, config, authenticator, preparation, reference, review
        )
    finally:
        if service.owner._lease.fd >= 0:
            service.owner.close()
        config.close()


def native_rows(service):
    """Snapshot actual native rows, excluding the separate preparation journal."""
    with lifecycle_read_operation(service.owner) as conn:
        return {
            name: tuple(tuple(row) for row in conn.execute("SELECT * FROM l2_" + name))
            for name in (
                "sources",
                "proposals",
                "reviews",
                "deliveries",
                "facts",
                "decisions",
                "events",
            )
        }
