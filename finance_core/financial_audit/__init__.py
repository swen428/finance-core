"""Append-only, tamper-evident audit chains for financial state changes."""

from finance_core.financial_audit.chain import (
    AUDIT_SCHEMA_VERSION,
    ZERO_AUDIT_HASH,
    AuditChainConflictError,
    AuditChainTransactionError,
    AuditChainVerification,
    AuditEventCommand,
    AuditVerificationError,
    FinancialAuditEvent,
    FinancialAuditRepository,
    UnsupportedAuditVersionError,
    append_financial_audit_event,
    append_financial_audit_event_atomically,
    derive_audit_event_public_id,
    financial_state_hash,
    verify_financial_audit_chain,
)

__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "ZERO_AUDIT_HASH",
    "AuditChainConflictError",
    "AuditChainTransactionError",
    "AuditChainVerification",
    "AuditEventCommand",
    "AuditVerificationError",
    "FinancialAuditEvent",
    "FinancialAuditRepository",
    "UnsupportedAuditVersionError",
    "append_financial_audit_event",
    "append_financial_audit_event_atomically",
    "derive_audit_event_public_id",
    "financial_state_hash",
    "verify_financial_audit_chain",
]
