import pytest

from finance_core.parser_proposals.lifecycle import (
    ALLOWED_TRANSITIONS,
    CONFIRMED,
    EDITED_PENDING_CONFIRMATION,
    EXPIRED,
    INITIAL_STATUS,
    PARSED_PENDING_CONFIRMATION,
    RAW_INTAKE_CONFIRMED,
    RAW_INTAKE_PARSED_PENDING_CONFIRMATION,
    RAW_INTAKE_REJECTED,
    RAW_INTAKE_STATUSES,
    REJECTED,
    SUPERSEDED,
    TERMINAL_STATUSES,
    is_terminal_status,
    new_proposal_status,
    raw_intake_status_for_proposal_status,
    status_implies_final_transaction_created,
    validate_transition,
)


def test_new_proposal_initial_status_is_parsed_pending_confirmation() -> None:
    assert INITIAL_STATUS == PARSED_PENDING_CONFIRMATION
    assert new_proposal_status() == PARSED_PENDING_CONFIRMATION


def test_allowed_transitions_from_parsed_pending_confirmation() -> None:
    assert ALLOWED_TRANSITIONS[PARSED_PENDING_CONFIRMATION] == frozenset(
        {
            CONFIRMED,
            REJECTED,
            EDITED_PENDING_CONFIRMATION,
            SUPERSEDED,
            EXPIRED,
        }
    )
    for next_status in ALLOWED_TRANSITIONS[PARSED_PENDING_CONFIRMATION]:
        assert validate_transition(PARSED_PENDING_CONFIRMATION, next_status) is True


def test_allowed_transitions_from_edited_pending_confirmation() -> None:
    assert ALLOWED_TRANSITIONS[EDITED_PENDING_CONFIRMATION] == frozenset(
        {
            CONFIRMED,
            REJECTED,
            SUPERSEDED,
            EXPIRED,
        }
    )
    for next_status in ALLOWED_TRANSITIONS[EDITED_PENDING_CONFIRMATION]:
        assert validate_transition(EDITED_PENDING_CONFIRMATION, next_status) is True


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_STATUSES))
def test_terminal_statuses_cannot_transition_further(terminal_status: str) -> None:
    assert is_terminal_status(terminal_status) is True
    with pytest.raises(ValueError, match="Terminal parser proposal status"):
        validate_transition(terminal_status, REJECTED)


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (PARSED_PENDING_CONFIRMATION, PARSED_PENDING_CONFIRMATION),
        (EDITED_PENDING_CONFIRMATION, EDITED_PENDING_CONFIRMATION),
        (EDITED_PENDING_CONFIRMATION, PARSED_PENDING_CONFIRMATION),
    ],
)
def test_invalid_transitions_are_rejected(from_status: str, to_status: str) -> None:
    with pytest.raises(ValueError, match="Invalid parser proposal status transition"):
        validate_transition(from_status, to_status)


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("draft", CONFIRMED),
        (PARSED_PENDING_CONFIRMATION, "finalized"),
        ("unknown", "also_unknown"),
    ],
)
def test_unknown_statuses_are_rejected(from_status: str, to_status: str) -> None:
    with pytest.raises(ValueError, match="Unknown parser proposal status"):
        validate_transition(from_status, to_status)


@pytest.mark.parametrize(
    ("proposal_status", "raw_intake_status"),
    [
        (PARSED_PENDING_CONFIRMATION, RAW_INTAKE_PARSED_PENDING_CONFIRMATION),
        (EDITED_PENDING_CONFIRMATION, RAW_INTAKE_PARSED_PENDING_CONFIRMATION),
        (CONFIRMED, RAW_INTAKE_CONFIRMED),
        (REJECTED, RAW_INTAKE_REJECTED),
        (SUPERSEDED, RAW_INTAKE_REJECTED),
        (EXPIRED, RAW_INTAKE_REJECTED),
    ],
)
def test_parser_lifecycle_maps_to_coarse_raw_intake_status(
    proposal_status: str,
    raw_intake_status: str,
) -> None:
    assert raw_intake_status_for_proposal_status(proposal_status) == raw_intake_status
    assert raw_intake_status in RAW_INTAKE_STATUSES


def test_unknown_status_cannot_be_mapped_to_raw_intake_status() -> None:
    with pytest.raises(ValueError, match="Unknown parser proposal status"):
        raw_intake_status_for_proposal_status("finalized")


def test_confirmed_does_not_imply_final_transaction_creation() -> None:
    assert is_terminal_status(CONFIRMED) is True
    assert status_implies_final_transaction_created(CONFIRMED) is False


def test_lifecycle_helper_has_no_database_side_effects(tmp_path) -> None:
    before = set(tmp_path.iterdir())

    assert new_proposal_status() == PARSED_PENDING_CONFIRMATION
    assert validate_transition(PARSED_PENDING_CONFIRMATION, CONFIRMED) is True
    assert status_implies_final_transaction_created(CONFIRMED) is False

    after = set(tmp_path.iterdir())
    assert after == before


def test_pending_proposals_cannot_become_final_facts_without_confirmation() -> None:
    proposal = {
        "status": new_proposal_status(),
        "confirmation_required": True,
        "is_final": False,
    }

    assert proposal["status"] == PARSED_PENDING_CONFIRMATION
    assert proposal["confirmation_required"] is True
    assert proposal["is_final"] is False
    assert status_implies_final_transaction_created(proposal["status"]) is False
    with pytest.raises(ValueError, match="Unknown parser proposal status"):
        validate_transition(proposal["status"], "finalized")
