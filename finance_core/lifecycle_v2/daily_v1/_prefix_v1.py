"""Pure eligibility checks for the four supported daily lifecycle prefixes."""

from ..contracts import LifecycleError
from .contracts import BundleFactRefV1, exact, positive, text

_PHASE_LENGTHS = {"proposal": 2, "review": 3, "delivery": 4, "confirm": 5}


def validate_order(stage, sequences):
    """Sequences are capture, proposal, then the applicable later phases."""
    if (
        type(stage) is not str
        or stage not in _PHASE_LENGTHS
        or type(sequences) is not tuple
        or len(sequences) != _PHASE_LENGTHS[stage]
        or any(type(seq) is not int or seq <= 0 for seq in sequences)
        or any(left >= right for left, right in zip(sequences, sequences[1:]))
    ):
        raise LifecycleError("Daily lifecycle prefix order mismatch")


def validate_eligibility(current_target, expected_target, current_proposal, expected_proposal):
    """Compare exact state at one point, independently of today's inventory."""
    for target in (current_target, expected_target):
        if target is not None:
            exact(target, BundleFactRefV1.__dataclass_fields__, "prefix target")
            BundleFactRefV1(**target)
    for proposal in (current_proposal, expected_proposal):
        if proposal is not None:
            if type(proposal) is not tuple or len(proposal) != 2:
                raise LifecycleError("Exact prefix proposal identity required")
            text(proposal[0], "proposal id")
            positive(proposal[1])
    if current_target != expected_target:
        raise LifecycleError("stale_bundle_ref")
    if current_proposal != expected_proposal:
        raise LifecycleError("stale_proposal")
