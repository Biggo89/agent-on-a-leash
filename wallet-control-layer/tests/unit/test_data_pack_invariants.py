"""Invariants of the organizers' data pack that our engine relies on.

If one of these ever fails, an assumption in GUIDELINES.md or specs/ has gone stale — fix the
docs, not the test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from leash.domain.money import FX_TO_CHF, Money, to_chf
from leash.domain.sanitize import detect_manipulation, is_lookalike
from sandbox.fixtures import DataPack

PACK = DataPack.load()


def test_pack_has_45_attempts_and_5_scenarios() -> None:
    assert len(PACK.attempts) == 45
    assert len(PACK.scenarios) == 5
    assert sum(int(s["event_count"]) for s in PACK.scenarios.values()) == 45


@pytest.mark.parametrize("attempt", PACK.attempts, ids=lambda a: a["authorization_id"])
def test_fx_and_subtotal_identities(attempt: dict[str, str]) -> None:
    """billing_amount_chf == amount * rate, and amount == items_subtotal + delivery_fee."""
    expected = to_chf(attempt["amount"], attempt["currency"])
    assert expected == Money.from_value(attempt["billing_amount_chf"])

    parts = Money.from_value(attempt["items_subtotal"]) + Money.from_value(attempt["delivery_fee"])
    assert parts == Money.from_value(attempt["amount"])


def test_spend_in_period_is_null_on_every_row() -> None:
    """Period tracking is deliberately ours — never read it from the fixture."""
    assert all(a["spend_in_period_before_chf"] == "" for a in PACK.attempts)


def test_fx_table_matches_the_pack() -> None:
    for currency, rate in PACK.fx.items():
        assert FX_TO_CHF[currency] == Decimal(str(rate))


def test_lookalike_merchant_pair_exists() -> None:
    """The pack contains at least one deliberately confusable name pair."""
    names = {m["merchant_id"]: m["merchant_name"] for m in PACK.merchants.values()}
    pairs = [(a, b) for a in names for b in names if a < b and is_lookalike(names[a], names[b])]
    assert pairs, "expected a confusable merchant pair in the pack"
    assert ("ME0022", "ME0059") in pairs


def test_merchant_text_contains_manipulation_attempts() -> None:
    """SCEN0004 ships live prompt injections; our detector must see them."""
    hits = {
        auth_id: detect_manipulation(line["item_details"], "item_details")
        for auth_id, lines in PACK.items.items()
        for line in lines
        if detect_manipulation(line["item_details"], "item_details")
    }
    assert set(hits) == {"AU0037", "AU0040"}, f"unexpected manipulation set: {sorted(hits)}"


def test_history_index_is_populated() -> None:
    """A silently empty history index would decline every familiarity-gated purchase.

    merchant_permitted cannot tell "0 because the merchant is new" from "0 because the file
    did not load", so the load itself is what gets guarded.
    """
    from leash.adapters.history import HistoryIndex
    from sandbox.fixtures import data_dir

    index = HistoryIndex.load(data_dir() / "authorization_history.csv")
    for card_id in ("CA0001", "CA0011", "CA0023", "CA0039"):
        assert len(index.known_merchants(card_id)) >= 10, f"{card_id} has too little history"
    assert index.merchant_approvals("CA0039", "ME0022") == 6
    assert index.merchant_approvals("CA0039", "ME0059") == 0


def test_identifiers_are_not_contiguous() -> None:
    """Never derive an identifier by incrementing another."""
    for missing in ("ME0042", "ME0043", "ME0050"):
        assert missing not in PACK.merchants
