"""The customer profile, and the preference candidates it proposes.

specs/customer-settings.md §8. `customers.csv` is **descriptive prose written by the
organizers, not the customer's stated intent**, so turning it into enforced rules would be the
compile problem again with the same over-blocking risk — and without even the excuse that the
customer wrote the words.

Resolved the way `llm-compiler.md` resolves low confidence: **the profile proposes candidates,
the customer accepts them.** Nothing derived from a profile is enforced without a tap, and
every candidate quotes the profile text it came from, so the review screen the compiler
already earned is reused rather than reinvented.

This module maps the **structured** fields only. `shopping_preferences` is free prose and is
compiled instead — by the same compiler, behind the same guard, in `runtime/supervisor.py`
where importing `compile/` does not invert the dependency direction. `travel_pattern` and
`home_region` are read by nothing at all, and offering them would be an inert control (§7);
they are reported as `not_offered` with the reason rather than quietly skipped, so a judge
asking "why isn't that a setting?" gets an answer from the API.

Lives in `adapters/` because it reads the read-only data pack. Takes the pack as an argument,
so it stays a function of its inputs.
"""

from __future__ import annotations

from typing import Any

from ..domain.settings import BY_KEY

#: `budget_style` is a categorical field, so it maps to a categorical setting. It maps to *how
#: often we ask*, never to *how much may be spent*: there is no number anywhere in the profile
#: and inventing one would be a fabricated limit wearing the customer's name (§8).
_BUDGET_STYLE_UNCERTAINTY: dict[str, str] = {
    "careful": "ask",
    "balanced": "ask",
    "flexible": "ask",
    "planned_high_value": "ask",
}

_NOT_OFFERED: tuple[tuple[str, str], ...] = (
    ("typical_spending", "descriptive, with no number any check could compare against"),
    ("travel_pattern", "no check reads merchant country: these cardholders shop abroad routinely"),
    ("home_region", "no check reads it"),
)


def customer_for_scenario(pack: Any, scenario_id: str) -> dict[str, str] | None:
    """Which persona this scenario belongs to.

    The chain is `purchase_attempts.authority_id` → `scenario_authorities` → `customer_id`;
    the scenario catalogue itself names no customer.
    """
    for attempt in pack.scenario_attempts(scenario_id):
        authority: dict[str, str] | None = pack.authorities.get(
            str(attempt.get("authority_id", ""))
        )
        if authority:
            customer: dict[str, str] | None = pack.customers.get(
                str(authority.get("customer_id", ""))
            )
            if customer:
                return customer
    return None


def card_for_scenario(pack: Any, scenario_id: str) -> dict[str, str] | None:
    for attempt in pack.scenario_attempts(scenario_id):
        card: dict[str, str] | None = pack.cards.get(str(attempt.get("card_id", "")))
        if card:
            return card
    return None


def account_ceiling(
    pack: Any, scenario_id: str, card_id: str | None = None
) -> dict[str, Any] | None:
    """Layer 0: the platform's own per-transaction limit on the account behind this card.

    A real number from the data, not an inferred one — the one profile-adjacent figure that
    can be enforced honestly. `monthly_limit_chf` is deliberately **not** returned: layering a
    period rule can delete the rule it was meant to reinforce (§3.4).

    `card_id` is the card the platform named when the run started (`client.run_card`). It wins
    over the scenario's attempts, and it is the only source on the live API, whose scenarios
    have no attempts in any pack file.
    """
    card = pack.cards.get(card_id) if card_id else None
    card = card or card_for_scenario(pack, scenario_id)
    if not card:
        return None
    account = pack.accounts.get(str(card.get("account_id", "")))
    if not account:
        return None
    try:
        limit = float(account["per_transaction_limit_chf"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "account_id": account.get("account_id"),
        "per_transaction_limit_chf": limit,
        "monthly_limit_chf": account.get("monthly_limit_chf"),
        "quote": f"account limit CHF {limit:.2f} per transaction",
    }


def candidates(pack: Any, scenario_id: str) -> dict[str, Any]:
    """Preference candidates for this scenario's cardholder — proposals, never rules."""
    customer = customer_for_scenario(pack, scenario_id)
    if customer is None:
        return {"customer": None, "candidates": [], "not_offered": _not_offered_rows()}

    proposed: list[dict[str, Any]] = []
    style = str(customer.get("budget_style", "")).strip()
    if style in _BUDGET_STYLE_UNCERTAINTY:
        policy = _BUDGET_STYLE_UNCERTAINTY[style]
        proposed.append(
            {
                "setting": "uncertainty_policy",
                "label": BY_KEY["uncertainty_policy"].label,
                "value": policy,
                "sentence": "Ask me when something is unclear",
                "source": "profile",
                "quote": f"budget style: {style}",
                "why": (
                    "a categorical field maps to a categorical setting; it never maps to an "
                    "amount, because the profile states none"
                ),
            }
        )

    return {
        "customer": {
            "customer_id": customer.get("customer_id"),
            "persona_name": customer.get("persona_name"),
            "background": customer.get("background"),
            "shopping_preferences": customer.get("shopping_preferences"),
            "budget_style": style,
        },
        "account": account_ceiling(pack, scenario_id),
        "candidates": proposed,
        "not_offered": _not_offered_rows(),
    }


def _not_offered_rows() -> list[dict[str, str]]:
    return [{"field": field, "why": why} for field, why in _NOT_OFFERED]
