"""Classifying an edit: does it narrow the mandate, or widen it?

specs/customer-settings.md §6. The brief asks that a customer be able to *"tighten, update, or
revoke"* their wallet policy, and the three are not one operation. Everything downstream — the
`PATCH` versus the new draft, the instant Apply versus the confirmation — hangs off this
function, so the table below is the feature's real contract.

Invariant I3 has its own section: anything this cannot *prove* narrows the mandate must widen.
"""

from __future__ import annotations

from typing import Any

import pytest

from leash.domain.amend import CONFLICT, NOOP, TIGHTEN, WIDEN, classify


def policy(**over: Any) -> dict[str, Any]:
    return {
        "hard_rules": over.get("hard_rules", []),
        "intent_facets": over.get("facets", []),
        "uncertainty_policy": over.get("uncertainty_policy", "ask"),
    }


def cap(value: float, scope: str = "purchase", **extra: Any) -> dict[str, Any]:
    return {
        "field": "billing_amount_chf",
        "operator": "<=",
        "value": value,
        "currency": "CHF",
        "scope": scope,
        **extra,
    }


# ------------------------------------------------------------------ money


@pytest.mark.parametrize(
    ("before", "after", "expect"),
    [
        ([cap(400)], [cap(250)], TIGHTEN),
        ([cap(250)], [cap(400)], WIDEN),
        ([cap(400)], [cap(400)], NOOP),
        ([], [cap(400)], TIGHTEN),
        # Removing the ceiling entirely is the loosest possible move and must never be a patch.
        ([cap(400)], [], WIDEN),
    ],
)
def test_the_per_order_cap(
    before: list[dict[str, Any]], after: list[dict[str, Any]], expect: str
) -> None:
    assert classify(policy(hard_rules=before), policy(hard_rules=after)).kind == expect


def test_the_cap_is_compared_on_what_binds_not_on_what_is_listed() -> None:
    """A mandate already tightened once carries both rules; only the tightest is the cap."""
    before = policy(hard_rules=[cap(400), cap(250)])
    assert classify(before, policy(hard_rules=[cap(400), cap(250), cap(200)])).kind == TIGHTEN
    assert classify(before, policy(hard_rules=[cap(300)])).kind == WIDEN


@pytest.mark.parametrize(
    ("before", "after", "expect", "why"),
    [
        # Every window is enforced, so within one window the direction is the ordinary one.
        (
            [cap(300, "period", period_days=7)],
            [cap(200, "period", period_days=7)],
            TIGHTEN,
            "lower",
        ),
        ([cap(300, "period", period_days=7)], [cap(400, "period", period_days=7)], WIDEN, "higher"),
        # A window nobody had before is a constraint nobody had before.
        ([], [cap(300, "period", period_days=7)], TIGHTEN, "where there was none"),
        (
            [cap(300, "period", period_days=7)],
            [cap(300, "period", period_days=7), cap(1000, "period", period_days=30)],
            TIGHTEN,
            "where there was none",
        ),
        # Dropping a ceiling is a widening however the others moved.
        ([cap(300, "period", period_days=7)], [], WIDEN, "dropping"),
    ],
)
def test_period_limits_are_compared_window_by_window(
    before: list[dict[str, Any]], after: list[dict[str, Any]], expect: str, why: str
) -> None:
    """specs/decision-rules.md §9 — every window a policy names is enforced.

    Before multi-window enrichment this function had to call **every** period edit a widening,
    because the shortest window displaced the rest and a "tightening" could delete a ceiling.
    """
    got = classify(policy(hard_rules=before), policy(hard_rules=after))
    assert got.kind == expect
    assert any(why in c.why for c in got.changes), [c.why for c in got.changes]


def test_swapping_one_window_for_another_still_widens() -> None:
    """The §3.4 counter-example. It is a widening now for the honest reason: a ceiling went.

    `CHF 500 / 7 days` does not replace `CHF 1000 / 30 days` — it stands beside it. Replacing
    one with the other therefore drops a constraint, and the customer is told which.
    """
    got = classify(
        policy(hard_rules=[cap(1000, "period", period_days=30)]),
        policy(hard_rules=[cap(500, "period", period_days=7)]),
    )
    assert got.kind == WIDEN
    assert any("dropping the 30-day ceiling" in c.why for c in got.changes)


def test_a_period_rule_with_no_window_widens() -> None:
    """It cannot be enforced, so it reaches the check as `unknown`. I3 — uncertainty widens."""
    got = classify(
        policy(hard_rules=[cap(300, "period", period_days=7)]),
        policy(hard_rules=[cap(200, "period", period_days=7), cap(1000, "period")]),
    )
    assert got.kind == WIDEN


# ------------------------------------------------------------------ uncertainty


@pytest.mark.parametrize(
    ("before", "after", "expect"),
    [
        ("ask", "decline", TIGHTEN),
        ("approve", "ask", TIGHTEN),
        ("decline", "ask", WIDEN),
        ("ask", "approve", WIDEN),
        ("ask", "ask", NOOP),
    ],
)
def test_uncertainty_moves_only_toward_decline_for_free(
    before: str, after: str, expect: str
) -> None:
    got = classify(policy(uncertainty_policy=before), policy(uncertainty_policy=after))
    assert got.kind == expect


# ------------------------------------------------------------------ facets


def familiarity(minimum: int) -> dict[str, Any]:
    return {"kind": "merchant_familiarity", "require": {"prior_approvals_min": minimum}}


def shops(*categories: str) -> dict[str, Any]:
    return {"kind": "merchant_type", "require": {"merchant_category_in": list(categories)}}


@pytest.mark.parametrize(
    ("before", "after", "expect"),
    [
        ([], [familiarity(1)], TIGHTEN),
        ([familiarity(1)], [], WIDEN),
        ([familiarity(1)], [familiarity(3)], TIGHTEN),
        ([familiarity(3)], [familiarity(1)], WIDEN),
        ([{"kind": "no_additions"}], [], WIDEN),
        ([], [{"kind": "no_additions"}], TIGHTEN),
        ([shops("groceries", "household")], [shops("groceries")], TIGHTEN),
        ([shops("groceries")], [shops("groceries", "household")], WIDEN),
    ],
)
def test_facet_direction(
    before: list[dict[str, Any]], after: list[dict[str, Any]], expect: str
) -> None:
    assert classify(policy(facets=before), policy(facets=after)).kind == expect


def test_trading_one_category_for_another_widens() -> None:
    """Neither a subset nor a superset: the mandate now permits something it did not.

    Swapping `groceries` for `household` reads like an edit rather than a loosening, and that
    is exactly why it must not be applied silently — a shop the customer never sanctioned
    becomes usable the moment it lands.
    """
    got = classify(policy(facets=[shops("groceries")]), policy(facets=[shops("household")]))
    assert got.kind == WIDEN
    assert "added as well as removed" in got.changes[0].why


def test_an_empty_allow_list_is_a_conflict_not_a_tightening() -> None:
    """An empty list reaches the check as UNKNOWN and turns every order into a step-up.

    Refusing it here is the only place the customer can be told *why*: at decision time the
    message is "the kind of shop your instruction allows could not be established", which
    nobody would connect to the setting they just cleared.
    """
    got = classify(policy(facets=[shops("groceries")]), policy(facets=[shops()]))
    assert got.kind == CONFLICT
    assert got.conflicts and got.conflicts[0].setting == "merchant_type"


# ------------------------------------------------------------------ I3


def test_one_widening_field_makes_the_whole_amendment_a_widening() -> None:
    """They are applied together or not at all, so the loosest field sets the price."""
    before = policy(hard_rules=[cap(400)], facets=[familiarity(1)])
    after = policy(hard_rules=[cap(250)], facets=[])  # narrower cap, dropped rule
    got = classify(before, after)
    assert got.kind == WIDEN
    assert {c.kind for c in got.changes} == {TIGHTEN, WIDEN}


def test_a_requirement_that_cannot_be_compared_widens() -> None:
    """I3 — uncertainty widens. A false widen costs one tap; a false tighten spends money."""
    before = policy(facets=[{"kind": "item_attribute", "require": {"size": 43}}])
    after = policy(facets=[{"kind": "item_attribute", "require": {"size": 44}}])
    got = classify(before, after)
    assert got.kind == WIDEN


def test_an_unreadable_value_widens_rather_than_scoring_as_narrower() -> None:
    before = policy(facets=[familiarity(2)])
    after = policy(
        facets=[{"kind": "merchant_familiarity", "require": {"prior_approvals_min": "many"}}]
    )
    assert classify(before, after).kind == WIDEN


def test_changes_carry_the_before_and_after_the_ui_renders() -> None:
    got = classify(policy(hard_rules=[cap(400)]), policy(hard_rules=[cap(250)]))
    change = got.changes[0]
    assert (change.before, change.after) == ("CHF 400.00", "CHF 250.00")
    assert change.as_dict()["label"] == "Per-order limit"
