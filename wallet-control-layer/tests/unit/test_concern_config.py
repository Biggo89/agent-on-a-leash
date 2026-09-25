"""The concern layer's configuration, pinned.

Weights and the threshold are the one place the engine's judgement is expressed as numbers, so
they get tests of their own rather than being asserted incidentally inside check vectors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from leash.domain.evaluator import (
    CHECKS,
    CONCERN_WEIGHTS,
    STEP_UP_THRESHOLD,
    combine,
    concern_weight,
)
from leash.domain.types import CheckResult, Decision, Verdict


def _concern(code: str) -> CheckResult:
    return CheckResult(code, Verdict.CONCERN, code)


def test_weights_are_pinned() -> None:
    """Changing one of these changes what the engine escalates. Do it deliberately."""
    assert CONCERN_WEIGHTS == {
        "merchant_text_manipulation": 2.0,
        "merchant_lookalike": 2.0,
        "device_novel": 2.0,
        "unrequested_addon": 2.0,
        "duplicate_order": 2.0,
        "split_order_suspected": 2.0,
        "goal_already_fulfilled": 2.0,
        "velocity_elevated": 1.0,
        "unusual_hour": 1.0,
    }
    assert STEP_UP_THRESHOLD == 2.0


@pytest.mark.parametrize("code", sorted(CONCERN_WEIGHTS))
def test_every_weight_is_emitted_by_some_check(code: str) -> None:
    """A weight that connects to no check is misleading config — a knob that moves nothing."""
    checks_dir = Path(__file__).parents[2] / "src/leash/domain/checks"
    sources = "\n".join(f.read_text() for f in checks_dir.glob("*.py"))
    assert f'"{code}"' in sources, f"{code} has a weight but no check emits it"


def test_unknown_concern_code_fails_loudly() -> None:
    """A new concern with no weight must not silently score zero and never escalate."""
    with pytest.raises(KeyError, match="CONCERN_WEIGHTS"):
        concern_weight("a_code_nobody_configured")


def test_adversarial_signals_escalate_alone() -> None:
    for code in (
        "merchant_text_manipulation",
        "merchant_lookalike",
        "device_novel",
        "unrequested_addon",
        "duplicate_order",
        "split_order_suspected",
        "goal_already_fulfilled",
    ):
        decision, codes = combine([_concern(code)], "ask")
        assert decision is Decision.STEP_UP, f"{code} should escalate on its own"
        assert code in codes


def test_ambient_signals_need_corroboration() -> None:
    """Velocity or an unusual hour alone is ordinary; together they are not."""
    for code in ("velocity_elevated", "unusual_hour"):
        decision, _ = combine([_concern(code)], "ask")
        assert decision is Decision.APPROVE, f"{code} should not escalate alone"

    decision, codes = combine([_concern("velocity_elevated"), _concern("unusual_hour")], "ask")
    assert decision is Decision.STEP_UP
    assert set(codes) == {"velocity_elevated", "unusual_hour"}


def test_a_violation_still_outranks_any_concern_pile() -> None:
    results = [
        CheckResult("x", Verdict.VIOLATION, "per_order_limit_exceeded"),
        _concern("device_novel"),
        _concern("velocity_elevated"),
    ]
    decision, codes = combine(results, "ask")
    assert decision is Decision.DECLINE
    assert codes == ("per_order_limit_exceeded",)


def test_all_checks_are_registered() -> None:
    assert len(CHECKS) == 16


# ------------------------------------------------- failure is not uncertainty


def test_a_crashed_check_never_approves_whatever_the_policy_says() -> None:
    """`uncertainty_policy: approve` resolves doubts, not the absence of an opinion.

    A check that raises becomes `unknown` with `engine_error_defaulted`. Routing that through
    the policy like an ordinary unknown approves a purchase on which nothing was evaluated,
    and tells the customer their rules were applied when none of them were.
    """
    broken = CheckResult("x", Verdict.UNKNOWN, "engine_error_defaulted")
    for policy, expected in (
        ("ask", Decision.STEP_UP),
        ("approve", Decision.STEP_UP),  # floored
        ("decline", Decision.DECLINE),  # the stricter choice is never overridden
    ):
        decision, codes = combine([broken], policy)
        assert decision is expected, f"{policy} produced {decision}"
        assert codes == ("engine_error_defaulted",)


def test_an_ordinary_unknown_still_follows_the_policy() -> None:
    """The floor is narrow on purpose: a missing fact is a doubt, and the customer owns it."""
    missing = CheckResult("x", Verdict.UNKNOWN, "return_terms_unknown")
    for policy, expected in (
        ("ask", Decision.STEP_UP),
        ("approve", Decision.APPROVE),
        ("decline", Decision.DECLINE),
    ):
        decision, _ = combine([missing], policy)
        assert decision is expected, f"{policy} produced {decision}"


def test_one_failure_among_ordinary_unknowns_still_floors_the_whole_decision() -> None:
    """Nothing was evaluated *somewhere*, and that is enough to keep it with the customer."""
    results = [
        CheckResult("a", Verdict.UNKNOWN, "return_terms_unknown"),
        CheckResult("b", Verdict.UNKNOWN, "engine_error_defaulted"),
    ]
    decision, _ = combine(results, "approve")
    assert decision is Decision.STEP_UP


def test_a_violation_still_outranks_a_failure() -> None:
    """The floor raises an approval to a question; it never lowers a decline."""
    results = [
        CheckResult("a", Verdict.VIOLATION, "per_order_limit_exceeded"),
        CheckResult("b", Verdict.UNKNOWN, "engine_error_defaulted"),
    ]
    decision, codes = combine(results, "approve")
    assert decision is Decision.DECLINE
    assert codes == ("per_order_limit_exceeded",)


def test_the_evaluator_really_does_default_a_raising_check() -> None:
    """End to end: a check that raises produces the floored decision, not an exception."""
    from leash.domain.evaluator import CHECKS, evaluate
    from tests.builders import make_event

    def exploding(ev: object, policy: object) -> CheckResult:
        raise RuntimeError("boom")

    original = tuple(CHECKS)
    try:
        import leash.domain.evaluator as ev_module

        ev_module.CHECKS = (exploding,)  # type: ignore[assignment]
        record = evaluate(make_event(), {"uncertainty_policy": "approve"})
    finally:
        ev_module.CHECKS = original  # type: ignore[assignment]

    assert record.decision is Decision.STEP_UP
    assert "engine_error_defaulted" in record.reason_codes
