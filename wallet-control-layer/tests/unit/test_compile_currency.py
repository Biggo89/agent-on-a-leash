"""Foreign-currency caps: written in EUR, enforced in CHF, never looser than the words.

specs/llm-compiler.md §"Foreign-currency caps" · specs/policy-ir.md compiler rule 7. The
instruction is the live API's SCEN0104 (2026-09-24), where the model returned EUR 200 as a
CHF 200 cap and nothing stopped it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from leash.compile import baseline
from leash.compile.contract import Guard, accept, apply_safety_floor
from leash.compile.currency import mentions, unconvertible
from leash.domain.checks.limits import check_per_order_limit, check_period_limit
from leash.domain.types import Verdict
from tests.builders import make_event, make_policy

CROSS_BORDER = (
    "Order hiking boots, size 46, from the Austrian outdoor retailer I already know. Pay no "
    "more than EUR 200 and only if they can be returned. Ask me if anything is unclear."
)
EUR_200 = {"amount": 200.0, "currency": "EUR", "rate": 0.95}


@pytest.mark.parametrize(
    ("text", "written", "chf"),
    [
        ("Pay no more than EUR 200", "EUR 200.00", "190.00"),
        ("at most 200 eur", "EUR 200.00", "190.00"),
        ("under €150", "EUR 150.00", "142.50"),
        ("£99.50 at most", "GBP 99.50", "111.44"),
        ("no more than $300", "USD 300.00", "261.00"),
        ("CHF 120 including delivery", "CHF 120.00", "120.00"),
    ],
)
def test_every_pack_currency_is_recognised_and_rounded_down(
    text: str, written: str, chf: str
) -> None:
    (mention,) = mentions(text)
    assert mention.describe() == written
    assert mention.chf() == Decimal(chf)


def test_rounding_down_never_lets_a_cent_over_the_words_through() -> None:
    (mention,) = mentions("USD 0.07")  # 0.0609 CHF: half-even would give 0.06, so would floor
    assert mention.chf() == Decimal("0.06")
    (mention,) = mentions("USD 0.09")  # 0.0783: half-even rounds UP to 0.08; the cap must not
    assert mention.chf() == Decimal("0.07")


def test_only_real_currency_codes_without_a_rate_are_questioned() -> None:
    assert unconvertible("no more than SEK 500") == ["SEK"]
    assert unconvertible("500 jpy max") == ["JPY"]
    assert unconvertible("a USB 3 cable up to CHF 20") == []
    assert unconvertible("buy 3 lenses") == []


# ------------------------------------------------------------------------ baseline


def test_the_baseline_converts_a_eur_cap_and_keeps_the_words() -> None:
    ir = baseline.compile_instruction(CROSS_BORDER)
    assert [(r["scope"], r["value"], r.get("stated")) for r in ir["rules"]] == [
        ("purchase", 190.0, EUR_200)
    ]
    assert ir["rules"][0]["provenance"] == "EUR 200"
    assert "Each order stays at or below CHF 190.00 (EUR 200.00)." in ir["guidance"]


def test_the_baseline_asks_rather_than_guessing_a_currency_it_cannot_convert() -> None:
    ir = baseline.compile_instruction("No more than SEK 500 per order.")
    assert ir["rules"] == []
    assert any("SEK" in q and "CHF" in q for q in ir["open_questions"])


def test_chf_instructions_compile_exactly_as_before() -> None:
    ir = baseline.compile_instruction("Keep each order at or below CHF 120 including delivery.")
    assert [(r["value"], "stated" in r) for r in ir["rules"]] == [(120.0, False)]


# ------------------------------------------------------------------------ guard, rail 12


def _rule(value: float, provenance: str = "Pay no more than EUR 200") -> dict[str, object]:
    return {
        "field": "billing_amount_chf",
        "operator": "<=",
        "value": value,
        "scope": "purchase",
        "provenance": provenance,
        "confidence": "high",
    }


@pytest.mark.parametrize(
    ("proposed", "enforced", "noted"),
    [
        (200, 190.0, True),  # copied the number: the live bug, corrected
        (190, 190.0, False),  # converted it itself: kept as is
        (180, 180.0, False),  # tighter on purpose: rail 9 lets a model tighten
        (250, 190.0, True),  # looser than the words: clamped
    ],
)
def test_rail_12_enforces_the_lower_of_the_model_and_the_conversion(
    proposed: float, enforced: float, noted: bool
) -> None:
    guard = Guard(CROSS_BORDER)
    rule = guard.rule(_rule(proposed))
    assert rule is not None
    assert (rule["value"], rule["currency"], rule["stated"]) == (enforced, "CHF", EUR_200)
    assert any(n.startswith("currency_converted") for n in guard.notes) is noted


def test_rail_12_drops_a_cap_it_cannot_convert_and_asks() -> None:
    guard = Guard("Keep it under SEK 500 please.")
    assert guard.rule(_rule(500, "under SEK 500")) is None
    assert any(n.startswith("currency_not_convertible") for n in guard.notes)
    assert guard.questions and "SEK" in guard.questions[0]


def test_rail_12_leaves_a_chf_amount_alone_even_beside_a_foreign_one() -> None:
    instruction = "Pay CHF 180 or EUR 200, whichever is less."
    rule = Guard(instruction).rule(_rule(180, "CHF 180 or EUR 200"))
    assert rule is not None and rule["value"] == 180.0 and "stated" not in rule


def test_the_live_failure_end_to_end_model_output_to_enforced_mandate() -> None:
    """What the model actually returned for SCEN0104 on 2026-09-24, through the whole guard."""
    raw = {"uncertainty_policy": "ask", "rules": [_rule(200)], "intent_facets": []}
    ir = accept(raw, CROSS_BORDER, model="test")
    assert ir is not None
    ir = apply_safety_floor(ir, baseline.compile_instruction(CROSS_BORDER))
    assert [(r["value"], r.get("stated")) for r in ir["rules"]] == [(190.0, EUR_200)]
    # Covered by the converted rule, so nothing was adopted and nothing called unsupported.
    assert not any(n.startswith(("safety_floor", "unsupported")) for n in ir["compiler_notes"])


# ------------------------------------------------------------------------ enforcement


def test_a_eur_cap_is_exact_at_the_boundary_and_says_what_was_written() -> None:
    policy = make_policy(hard_rules=[{**_rule(190.0), "currency": "CHF", "stated": EUR_200}])
    at_limit = make_event(amount="200.00", currency="EUR", billing_amount_chf="190.00")
    one_cent_over = make_event(amount="200.01", currency="EUR", billing_amount_chf="190.01")

    assert check_per_order_limit(at_limit, policy).verdict is Verdict.PASS
    over = check_per_order_limit(one_cent_over, policy)
    assert over.verdict is Verdict.VIOLATION
    assert "EUR 200.00 (CHF 190.00)" in over.detail


def test_a_eur_period_cap_names_the_words_too() -> None:
    rule = {
        **_rule(237.5),
        "scope": "period",
        "period_days": 7,
        "stated": {**EUR_200, "amount": 250.0},
    }
    (result,) = check_period_limit(
        make_event(billing_amount_chf="50.00", approved_spend_windows={7: "200.00"}),
        make_policy(hard_rules=[rule]),
    )
    assert result.verdict is Verdict.VIOLATION
    assert "EUR 250.00 (CHF 237.50)" in result.detail
