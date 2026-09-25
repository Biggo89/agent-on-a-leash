"""Tightening a mandate — the three defects rehearsal found in beat 5.

specs/decision-rules.md §9 · specs/service-contract.md §2

Beat 5's story is that the customer is the final authority: they can narrow a mandate and they
can revoke it. Rehearsing it found that narrowing did not work, that the compiler could lose a
stated cap on the way in, and that a malformed PATCH reported success. One test file, because
the three only produce a wrong *decision* in combination.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from leash.compile.baseline import compile_instruction
from leash.domain.checks.limits import check_per_order_limit, check_period_limit
from leash.domain.policy import period_caps, period_windows
from tests.builders import make_event, make_policy


def cap(value: float, scope: str = "purchase", **extra: object) -> dict[str, object]:
    return {
        "field": "billing_amount_chf",
        "operator": "<=",
        "value": value,
        "currency": "CHF",
        "scope": scope,
        **extra,
    }


# ------------------------------------------------------- 1. tightening actually tightens


@pytest.mark.parametrize(
    ("rules", "amount", "expect"),
    [
        ([cap(400)], "300.00", "pass"),
        # PATCH appends, so the tightening is the later entry — the case that was broken.
        ([cap(400), cap(250)], "300.00", "violation"),
        # …and the same answer with the list the other way round. No order dependence.
        ([cap(250), cap(400)], "300.00", "violation"),
        ([cap(400), cap(250)], "200.00", "pass"),
    ],
)
def test_a_tighter_cap_binds_whatever_the_list_order(
    rules: list[dict[str, object]], amount: str, expect: str
) -> None:
    ev = make_event(billing_amount_chf=amount, amount=amount)
    result = check_per_order_limit(ev, make_policy(hard_rules=rules))
    assert str(result.verdict) == expect, result.detail


def test_the_message_names_the_cap_that_actually_bound() -> None:
    """A remedy quoting the limit the customer replaced is worse than no remedy."""
    ev = make_event(billing_amount_chf="300.00", amount="300.00")
    result = check_per_order_limit(ev, make_policy(hard_rules=[cap(400), cap(250)]))
    assert "250.00" in result.detail and "400.00" not in result.detail
    assert result.follow_up == "An order of CHF 250.00 or less would be within your limit."


def test_every_window_the_mandate_states_is_enforced() -> None:
    """specs/decision-rules.md §9 — the shortest window no longer displaces the rest.

    Before multi-window enrichment `binding_cap` kept the 7-day rule and dropped the 30-day
    one, which is how an add-only PATCH could delete a monthly ceiling. Now enrichment
    computes both and each is its own result.
    """
    rules = [cap(1000, "period", period_days=30), cap(300, "period", period_days=7)]
    assert period_windows(rules) == (7, 30)
    assert [days for days, _ in period_caps(rules)] == [7, 30]

    ev = make_event(
        billing_amount_chf="80.00",
        amount="80.00",
        approved_spend_windows={7: "250.00", 30: "400.00"},
    )
    weekly, monthly = check_period_limit(ev, make_policy(hard_rules=rules))
    assert str(weekly.verdict) == "violation"
    assert "300.00" in weekly.detail and "7 days" in weekly.detail
    assert str(monthly.verdict) == "pass"
    assert "1000.00" in monthly.detail and "30 days" in monthly.detail


def test_a_shorter_window_cannot_delete_a_longer_one() -> None:
    """The counter-example from specs/customer-settings.md §3.4, as a regression test.

    CHF 400 on each of day -25, -15, -5 is 1200 in thirty days and 400 in seven. Adding a
    weekly CHF 500 rule beside a monthly CHF 1000 one used to make this approve.
    """
    rules = [cap(1000, "period", period_days=30), cap(500, "period", period_days=7)]
    ev = make_event(
        billing_amount_chf="50.00",
        amount="50.00",
        approved_spend_windows={7: "400.00", 30: "1200.00"},
    )
    results = check_period_limit(ev, make_policy(hard_rules=rules))
    assert [str(r.verdict) for r in results] == ["pass", "violation"]


# ------------------------------------------- 2. a stated cap is never lost on the way in

TWO_CAPS = (
    "Spending limit: CHF 250 in total across any fourteen days, and never more than "
    "CHF 75 on a single order. If you are not sure, decline."
)


def test_a_per_order_cap_phrased_without_the_words_per_order_is_still_per_order() -> None:
    rules = compile_instruction(TWO_CAPS)["rules"]
    scoped = {(float(r["value"]), r["scope"]) for r in rules}
    assert (75.0, "purchase") in scoped, rules
    assert (250.0, "period") in scoped, rules


@pytest.mark.parametrize(("amount", "expect"), [("50.00", "pass"), ("200.00", "violation")])
def test_the_stated_per_order_cap_is_enforced(amount: str, expect: str) -> None:
    """Was: scoped as a second 14-day rule, then dropped for being the looser one.

    A CHF 200 single order approved on an instruction that says never more than 75.
    """
    policy = make_policy(hard_rules=compile_instruction(TWO_CAPS)["rules"])
    ev = make_event(billing_amount_chf=amount, amount=amount)
    assert str(check_per_order_limit(ev, policy).verdict) == expect


@pytest.mark.parametrize(
    "phrasing",
    [
        "at or below CHF 75 per order",
        "at or below CHF 75 for each order",
        "no more than CHF 75 on a single order",
        "no more than CHF 75 on any single order",
        "no more than CHF 75 for one purchase",
        "no more than CHF 75 per transaction",
    ],
)
def test_per_order_phrasings_all_scope_to_the_order(phrasing: str) -> None:
    rules = compile_instruction(f"Keep spending {phrasing}. Ask me when uncertain.")["rules"]
    assert [r["scope"] for r in rules] == ["purchase"], rules


def test_the_cue_needs_a_quantifier_so_a_bare_noun_never_matches() -> None:
    """ "one ordinary grocery item" must not read as "one order" (SCEN0000)."""
    rules = compile_instruction(
        "Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly."
    )["rules"]
    assert [(float(r["value"]), r["scope"]) for r in rules] == [(20.0, "purchase")]


# --------------------------------------- 3. a PATCH that changes nothing is not a success


@pytest.fixture
def client() -> TestClient:
    from leash.service.app import app

    return TestClient(app)


def test_patching_the_create_shape_is_refused_not_ignored(client: TestClient) -> None:
    """`{"instruction": …}` used to return 200 and change nothing."""
    response = client.patch("/v1/mandates/TM_NOT_REAL", json={"instruction": "anything"})
    assert response.status_code == 422, response.text
    assert "instruction" in response.text


def test_a_typo_in_a_known_field_is_refused(client: TestClient) -> None:
    response = client.patch("/v1/mandates/TM_NOT_REAL", json={"hard_rule": [cap(250)]})
    assert response.status_code == 422, response.text


def test_the_documented_fields_are_still_accepted(client: TestClient) -> None:
    """The refusal must reject the shape, not every PATCH — 404 means it got past validation."""
    response = client.patch(
        "/v1/mandates/TM_NOT_REAL",
        json={"hard_rules": [cap(250)], "uncertainty_policy": "decline"},
    )
    assert response.status_code != 422, response.text
