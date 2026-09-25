"""Minimal EnrichedEvent builder for check-level vectors.

Keeps the YAML vectors compact: a case names only the fields it cares about and everything
else takes a neutral default, so a vector reads as the rule it is testing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from leash.domain.money import Money
from leash.domain.sanitize import ItemFacts, detect_manipulation
from leash.domain.types import EnrichedEvent, Enrichment

_MISSING = object()


def _windows(over: dict[str, Any]) -> dict[int, Money]:
    named = over.pop("approved_spend_windows", _MISSING)
    if named is not _MISSING:
        over.pop("period_days", None)
        over.pop("approved_spend_window_chf", None)
        return {int(days): Money.from_value(value) for days, value in (named or {}).items()}
    days = int(over.pop("period_days", 7))
    return {days: Money.from_value(over.pop("approved_spend_window_chf", "0.00"))}


def make_event(**over: Any) -> EnrichedEvent:
    enrichment = Enrichment(
        merchant_prior_approvals=over.pop("merchant_prior_approvals", 0),
        merchant_prior_approvals_customer=over.pop("merchant_prior_approvals_customer", 0),
        device_prior_approvals=over.pop("device_prior_approvals", 0),
        familiarity_basis=over.pop("familiarity_basis", "history"),
        run_approvals=over.pop("run_approvals", 0),
        merchant_lookalike_of=over.pop("merchant_lookalike_of", None),
        merchant_lookalike_name=over.pop("merchant_lookalike_name", None),
        # A vector may name windows explicitly ({7: "250.00"}), or give one figure and let
        # the builder file it under a default 7-day window — which is what every
        # single-window vector written before multi-window enrichment means.
        #
        # An explicit `{}` means enrichment computed nothing and must stay empty: it is how a
        # vector says "this window is unknown", and treating it as "no windows given, use the
        # default" would quietly hand the check a figure it should not have.
        approved_spend_windows=_windows(over),
        is_duplicate_of=over.pop("is_duplicate_of", None),
        is_requote_of=over.pop("is_requote_of", None),
        # A vector names these as a list of {id, chf} so the YAML reads as the orders it means.
        same_merchant_recent=tuple(
            (entry["id"], Money.from_value(entry["chf"]))
            for entry in over.pop("same_merchant_recent", [])
        ),
        # A vector names prior approved carts as [{id, items: [...]}], mirroring `items`.
        approved_carts=tuple(
            (entry["id"], tuple(entry.get("items", []))) for entry in over.pop("approved_carts", [])
        ),
        night_hours=over.pop("night_hours", False),
        manipulations=tuple(
            m
            for field, texts in (
                ("item_details", over.pop("manipulation_texts", [])),
                ("merchant_name", over.pop("manipulation_names", [])),
            )
            for text in texts
            for m in detect_manipulation(text, field)
        ),
        item_facts=tuple(
            ItemFacts(
                return_window_days=f.get("return_window_days"),
                size=f.get("size"),
                final_sale=f.get("final_sale", False),
                return_policy_stated=f.get(
                    "return_policy_stated",
                    f.get("return_window_days") is not None or f.get("final_sale", False),
                ),
            )
            for f in over.pop("item_facts", [])
        ),
    )
    defaults: dict[str, Any] = {
        "raw": {},
        "authorization_id": "AU_TEST",
        "timestamp": datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        "billing_amount_chf": Money.from_value("100.00"),
        "amount": Money.from_value("100.00"),
        "currency": "CHF",
        "items_subtotal": Money.from_value("100.00"),
        "delivery_fee": Money.zero(),
        "merchant_id": "ME0001",
        "merchant_name": "Test Merchant",
        "merchant_category": "groceries",
        "merchant_mcc": "5411",
        "merchant_country": "CH",
        "customer_device_id": "DVC-TEST",
        "recent_attempt_count_10m": 0,
        "order_returnable": "true",
        "order_cancellable": "unknown",
        "fulfillment_method": "delivery",
        "related_authorization_id": None,
        "related_authorization_status": None,
        "items": (),
        "enrichment": enrichment,
    }
    for key, value in over.items():
        if key in ("billing_amount_chf", "amount", "items_subtotal", "delivery_fee"):
            value = Money.from_value(value)
        elif key == "items":
            value = tuple(value)
        defaults[key] = value
    return EnrichedEvent(**defaults)


def make_policy(**over: Any) -> dict[str, Any]:
    return {
        "hard_rules": over.get("hard_rules", []),
        "uncertainty_policy": over.get("uncertainty_policy", "ask"),
        "intent_facets": over.get("facets", over.get("intent_facets", [])),
    }
