"""Raw authorization.request -> EnrichedEvent.

The boundary where JSON becomes typed domain data. Money is parsed to centimes here so no
float ever reaches a check, and timestamps become timezone-aware UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ..domain.ledger import Ledger
from ..domain.money import Money
from ..domain.sanitize import detect_manipulation, extract_item_facts, is_lookalike
from ..domain.types import EnrichedEvent, Enrichment
from .history import HistoryIndex


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cart_signature(items: list[dict[str, Any]]) -> tuple[tuple[str, int, float], ...]:
    """Order-independent fingerprint of a cart: what, how many, at what unit price.

    Two different products that happen to cost the same at the same merchant are not a
    duplicate, so the amount alone is not enough to identify one.
    """
    return tuple(
        sorted(
            (str(i.get("item_id", "")), int(i.get("quantity", 1)), float(i.get("unit_price", 0)))
            for i in items
        )
    )


def parse_event(
    event: dict[str, Any],
    *,
    history: HistoryIndex,
    ledger: Ledger,
    merchants: dict[str, dict[str, str]],
    period_windows: Sequence[int] | None,
    seen_in_run: list[EnrichedEvent] | None = None,
    duplicate_window_minutes: int = 60,
    split_window_minutes: int = 30,
) -> EnrichedEvent:
    a = event["authorization"]
    card_id, merchant_id = a["card_id"], a["merchant"]["merchant_id"]
    ts = _ts(a["timestamp"])

    item_facts = tuple(extract_item_facts(i.get("item_details", "")) for i in a["items"])
    # decision-rules.md §3 names three untrusted sources. Only item_details carries an
    # injection in this pack, but the other two cost nothing to cover.
    manipulations = (
        tuple(m for f in item_facts for m in f.manipulations)
        + detect_manipulation(a["merchant"]["merchant_name"], "merchant_name")
        + detect_manipulation(a["purchase_description"], "purchase_description")
    )

    device_id = a["customer_device_id"]
    # Orders approved earlier in this run, by the engine or by the customer on a step-up (the
    # ledger moves a step-up into `approvals` when it is resolved).
    approved_ids = {e.authorization_id for e in ledger.approvals}
    run_approved = [
        prior for prior in (seen_in_run or []) if prior.authorization_id in approved_ids
    ]

    # Where familiarity comes from. A card with **no approved history row at all** is a card we
    # know nothing about, not one that never shopped: the live API's cardholders are all like
    # that, and reading their empty file as "never" declined or stepped up every order. For such
    # a card the run's own approvals are the history (specs/check-merchant-permitted.md §"No
    # history at all", specs/check-session-integrity.md §"A card with no history"). No repo-pack
    # card qualifies — each has at least 38 approved rows — so the board cannot move.
    if history.has_history(card_id):
        basis = "history"
        merchant_prior = history.merchant_approvals(card_id, merchant_id)
        merchant_prior_customer = history.customer_merchant_approvals(card_id, merchant_id)
        device_prior = history.device_approvals(card_id, device_id)
        known_merchants: list[tuple[str, str]] = [
            (known_id, merchants[known_id]["merchant_name"])
            for known_id in history.known_merchants(card_id)
            if known_id in merchants
        ]
    else:
        basis = "run"
        merchant_prior = sum(1 for prior in run_approved if prior.merchant_id == merchant_id)
        # The history file is the only source of a card's owner, so there is no person to
        # count for beyond this card.
        merchant_prior_customer = merchant_prior
        device_prior = sum(
            1 for prior in run_approved if device_id and prior.customer_device_id == device_id
        )
        known_merchants = list(
            dict.fromkeys((p.merchant_id, p.merchant_name) for p in run_approved)
        )

    # Lookalike: confusably similar to a merchant this card HAS used, different id, no history here.
    lookalike: str | None = None
    lookalike_name: str | None = None
    if merchant_prior == 0:
        for known_id, known_name in known_merchants:
            if known_id != merchant_id and is_lookalike(a["merchant"]["merchant_name"], known_name):
                lookalike = known_id
                lookalike_name = known_name
                break

    billing = Money.from_value(a["billing_amount_chf"])

    # Duplicate: same merchant, amount and cart as an earlier attempt in this run that we
    # actually committed to, inside the window, and with no link to a declined predecessor.
    #
    # Repeating a DECLINED order is a retry, not a double-spend: nothing was charged, and the
    # other checks will judge it again on its own merits. Only approvals and stepped-up
    # authorizations awaiting the customer can be duplicated, so the committed set is read
    # from the ledger the engine already maintains.
    duplicate_of: str | None = None
    signature = _cart_signature(list(a["items"]))
    committed = {e.authorization_id for e in ledger.approvals} | set(ledger.pending)
    if not a.get("related_authorization_id"):
        for prior in reversed(seen_in_run or []):
            if (
                prior.authorization_id in committed
                and prior.merchant_id == merchant_id
                and prior.billing_amount_chf == billing
                and _cart_signature(list(prior.items)) == signature
                and 0 <= (ts - prior.timestamp).total_seconds() <= duplicate_window_minutes * 60
            ):
                duplicate_of = prior.authorization_id
                break

    requote_of = (
        a["related_authorization_id"]
        if a.get("related_authorization_status") == "declined"
        else None
    )

    # Split order: committed orders at THIS merchant inside a shorter window. The same walk as
    # the duplicate scan above with a different predicate — no cart or amount match, because
    # splitting is precisely the case where the two orders differ. `committed` is reused: a
    # declined prior order charged nothing and cannot contribute to a total
    # (specs/check-split-order.md).
    same_merchant_recent = tuple(
        (prior.authorization_id, prior.billing_amount_chf)
        for prior in (seen_in_run or [])
        if prior.authorization_id in committed
        and prior.merchant_id == merchant_id
        and 0 <= (ts - prior.timestamp).total_seconds() <= split_window_minutes * 60
    )

    # Goal fulfilment reads *approved* orders only, which is a different set from the
    # `committed` one above: a stepped-up order is awaiting the customer and has bought
    # nothing (specs/check-goal-fulfilled.md).
    approved_carts = tuple((prior.authorization_id, prior.items) for prior in run_approved)

    return EnrichedEvent(
        raw=event,
        authorization_id=a["authorization_id"],
        timestamp=ts,
        billing_amount_chf=billing,
        amount=Money.from_value(a["amount"]),
        currency=a["currency"],
        items_subtotal=Money.from_value(a["items_subtotal"]),
        delivery_fee=Money.from_value(a["delivery_fee"]),
        merchant_id=merchant_id,
        merchant_name=a["merchant"]["merchant_name"],
        merchant_category=a["merchant"]["merchant_category"],
        merchant_mcc=a["merchant"]["merchant_mcc"],
        merchant_country=a["merchant"]["merchant_country"],
        customer_device_id=a["customer_device_id"],
        recent_attempt_count_10m=a["recent_attempt_count_10m"],
        order_returnable=a["order_returnable"],
        order_cancellable=a["order_cancellable"],
        fulfillment_method=a["fulfillment_method"],
        related_authorization_id=a.get("related_authorization_id"),
        related_authorization_status=a.get("related_authorization_status"),
        items=tuple(a["items"]),
        enrichment=Enrichment(
            merchant_prior_approvals=merchant_prior,
            merchant_prior_approvals_customer=merchant_prior_customer,
            device_prior_approvals=device_prior,
            familiarity_basis=basis,
            run_approvals=len(run_approved),
            merchant_lookalike_of=lookalike,
            merchant_lookalike_name=lookalike_name,
            # One figure per window the policy names (specs/decision-rules.md §4). The check
            # looks up the window belonging to the rule it is evaluating; anything absent from
            # this map is `unknown` there, never zero.
            approved_spend_windows={
                days: ledger.spend_in_window(ts, days)
                for days in sorted(set(period_windows or ()))
                if days > 0
            },
            is_duplicate_of=duplicate_of,
            is_requote_of=requote_of,
            same_merchant_recent=same_merchant_recent,
            approved_carts=approved_carts,
            night_hours=0 <= ts.hour < 5,
            item_facts=item_facts,
            manipulations=manipulations,
        ),
    )
