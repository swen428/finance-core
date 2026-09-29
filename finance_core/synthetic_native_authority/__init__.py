"""Synthetic-only native-independent authority proof.

This package cannot issue a production profile, SQLite, backup, restore or
managed-open grant. Its anchors and keys are disposable test inputs.
"""

from .format import FormatError
from .store import (
    AuthorityError,
    CapacityError,
    FreshTestGrant,
    LeaseBusyError,
    PoisonedError,
    QuarantinedError,
    SyntheticAuthorityStore,
    SyntheticTestIssuer,
    TestAnchor,
    Token,
    UnsupportedPlatformError,
    VerifiedRestoreTestGrant,
    VerifiedSnapshot,
)

__all__ = [
    "AuthorityError",
    "CapacityError",
    "FormatError",
    "FreshTestGrant",
    "LeaseBusyError",
    "PoisonedError",
    "QuarantinedError",
    "SyntheticAuthorityStore",
    "SyntheticTestIssuer",
    "TestAnchor",
    "Token",
    "UnsupportedPlatformError",
    "VerifiedRestoreTestGrant",
    "VerifiedSnapshot",
]
