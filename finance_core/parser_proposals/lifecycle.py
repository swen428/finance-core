from __future__ import annotations

from types import MappingProxyType

PARSED_PENDING_CONFIRMATION = "parsed_pending_confirmation"
CONFIRMED = "confirmed"
REJECTED = "rejected"
EDITED_PENDING_CONFIRMATION = "edited_pending_confirmation"
SUPERSEDED = "superseded"
EXPIRED = "expired"

INITIAL_STATUS = PARSED_PENDING_CONFIRMATION
RAW_INTAKE_PENDING_PARSE = "pending_parse"
RAW_INTAKE_PARSED_PENDING_CONFIRMATION = PARSED_PENDING_CONFIRMATION
RAW_INTAKE_CONFIRMED = CONFIRMED
RAW_INTAKE_REJECTED = REJECTED

ALLOWED_STATUSES = frozenset(
    {
        PARSED_PENDING_CONFIRMATION,
        CONFIRMED,
        REJECTED,
        EDITED_PENDING_CONFIRMATION,
        SUPERSEDED,
        EXPIRED,
    }
)

RAW_INTAKE_STATUSES = frozenset(
    {
        RAW_INTAKE_PENDING_PARSE,
        RAW_INTAKE_PARSED_PENDING_CONFIRMATION,
        RAW_INTAKE_CONFIRMED,
        RAW_INTAKE_REJECTED,
    }
)

TERMINAL_STATUSES = frozenset(
    {
        CONFIRMED,
        REJECTED,
        SUPERSEDED,
        EXPIRED,
    }
)

ALLOWED_TRANSITIONS = MappingProxyType(
    {
        PARSED_PENDING_CONFIRMATION: frozenset(
            {
                CONFIRMED,
                REJECTED,
                EDITED_PENDING_CONFIRMATION,
                SUPERSEDED,
                EXPIRED,
            }
        ),
        EDITED_PENDING_CONFIRMATION: frozenset(
            {
                CONFIRMED,
                REJECTED,
                SUPERSEDED,
                EXPIRED,
            }
        ),
        CONFIRMED: frozenset(),
        REJECTED: frozenset(),
        SUPERSEDED: frozenset(),
        EXPIRED: frozenset(),
    }
)


def new_proposal_status() -> str:
    """Return the only valid initial status for a newly parsed proposal."""
    return INITIAL_STATUS


def is_terminal_status(status: str) -> bool:
    """Return whether status is terminal after verifying it is known."""
    _validate_known_status(status)
    return status in TERMINAL_STATUSES


def validate_transition(from_status: str, to_status: str) -> bool:
    """Validate a parser proposal lifecycle transition.

    Returns True for valid transitions and raises ValueError for unknown or
    disallowed transitions.
    """
    _validate_known_status(from_status)
    _validate_known_status(to_status)

    if is_terminal_status(from_status):
        raise ValueError(f"Terminal parser proposal status cannot transition: {from_status}")

    if to_status not in ALLOWED_TRANSITIONS[from_status]:
        raise ValueError(f"Invalid parser proposal status transition: {from_status} -> {to_status}")

    return True


def status_implies_final_transaction_created(status: str) -> bool:
    """Parser proposal statuses never prove final transaction creation."""
    _validate_known_status(status)
    return False


def raw_intake_status_for_proposal_status(status: str) -> str:
    """Map detailed parser proposal lifecycle to coarse raw intake status."""
    _validate_known_status(status)
    if status == CONFIRMED:
        return RAW_INTAKE_CONFIRMED
    if status in {REJECTED, SUPERSEDED, EXPIRED}:
        return RAW_INTAKE_REJECTED
    return RAW_INTAKE_PARSED_PENDING_CONFIRMATION


def _validate_known_status(status: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Unknown parser proposal status: {status}")
