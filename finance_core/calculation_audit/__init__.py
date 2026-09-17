"""Calculation audit layer -- deterministic audit snapshot creation and validation.

Exports:
    AuditSnapshot           -- immutable audit record model
    AuditValidationError    -- validation failure
    AuditRoundingEntry      -- rounding adjustment audit entry
    AuditSettlementEntry    -- settlement obligation audit entry
    create_audit_snapshot   -- create a snapshot from a calculator result
    validate_audit_snapshot -- validate an existing snapshot
    to_decimal_safe         -- Decimal-safe value conversion helper
"""

from finance_core.calculation_audit.audit import (
    create_audit_snapshot as create_audit_snapshot,
)
from finance_core.calculation_audit.audit import (
    validate_audit_snapshot as validate_audit_snapshot,
)
from finance_core.calculation_audit.models import (
    AuditRoundingEntry as AuditRoundingEntry,
)
from finance_core.calculation_audit.models import (
    AuditSettlementEntry as AuditSettlementEntry,
)
from finance_core.calculation_audit.models import (
    AuditSnapshot as AuditSnapshot,
)
from finance_core.calculation_audit.models import (
    AuditValidationError as AuditValidationError,
)
from finance_core.calculation_audit.models import (
    to_decimal_safe as to_decimal_safe,
)
