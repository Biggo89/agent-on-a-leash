"""An agent's order → the ``authorization.request`` the engine already judges.

The agent describes what it wants to buy in its own terms: a shop, a few lines, a delivery
fee. The engine judges events shaped by the organizers' schema, so the connector builds one —
and fills every field the platform would have filled from *its* data, never from the agent's.
The merchant record comes from the data pack when the shop is known there; the FX rate from the
pack; the device from the token; the count of recent attempts from the errand's own memory.
What the agent says about a shop is a name to look up, never a fact to trust (AGENTS.md §3.7).

Money is Decimal from the first character and quantized half-even to centimes, then written
to the event as the JSON numbers the schema requires — the same boundary rule as
``adapters/parse.py``, which reads them straight back into ``Money``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from ..adapters.datapack import item_catalogue
from ..domain.sanitize import normalize_name
from ..domain.types import EnrichedEvent

CENT = Decimal("0.01")
#: The platform's decision deadline (technical_details.md). A tool call has the same budget, so
#: the deadline guard sees the same number it sees on a live run.
DECISION_SECONDS = 8
VELOCITY_WINDOW_MINUTES = 10
MAX_LINES = 50
FULFILLMENT = ("delivery", "pickup", "digital", "in_store")
TRISTATE = ("true", "false", "unknown")
UNKNOWN_MERCHANT_CATEGORY = "unknown"


class OrderError(ValueError):
    """The order cannot become an event. The message is written for the agent to act on."""


def _money(value: Any, what: str, *, minimum: Decimal | None = Decimal("0")) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise OrderError(f"{what} must be a number")
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError) as exc:
        raise OrderError(f"{what} must be a number, not {value!r}") from exc
    if minimum is not None and amount < minimum:
        raise OrderError(f"{what} must be at least {minimum}")
    return amount


def _int(value: Any, what: str, *, default: int, minimum: int = 1) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or int(value) != value:
        raise OrderError(f"{what} must be a whole number")
    if int(value) < minimum:
        raise OrderError(f"{what} must be at least {minimum}")
    return int(value)


def _choice(value: Any, what: str, options: tuple[str, ...], default: str) -> str:
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text not in options:
        raise OrderError(f"{what} must be one of {', '.join(options)}")
    return text


def resolve_merchant(
    merchants: dict[str, dict[str, str]], name: str, merchant_id: str | None = None
) -> dict[str, Any]:
    """The merchant block for an event: the pack's record when the shop is known, else a
    synthetic one whose category is `unknown`.

    Matching is exact (after `normalize_name`), never fuzzy: a shop whose name merely
    *resembles* a known one must stay a different merchant, or the lookalike check could
    never fire on it.
    """
    if merchant_id and merchant_id in merchants:
        return _merchant_block(merchants[merchant_id])
    wanted = normalize_name(name)
    if wanted:
        for row in merchants.values():
            if normalize_name(row["merchant_name"]) == wanted:
                return _merchant_block(row)
    if not name.strip():
        raise OrderError("merchant is required: the name of the shop")
    digest = hashlib.sha256(wanted.encode()).hexdigest()[:8].upper()
    return {
        "merchant_id": f"ME-AGENT-{digest}",
        "merchant_name": name.strip(),
        "merchant_category": UNKNOWN_MERCHANT_CATEGORY,
        "merchant_mcc": "0000",
        "merchant_country": "ZZ",
        "merchant_city": "",
        "availability": "online",
        "recurring_capable": "false",
        "known": False,
    }


def _merchant_block(row: dict[str, str]) -> dict[str, Any]:
    return {
        "merchant_id": row["merchant_id"],
        "merchant_name": row["merchant_name"],
        "merchant_category": row["merchant_category"],
        "merchant_mcc": row["merchant_mcc"],
        "merchant_country": row["merchant_country"],
        "merchant_city": row.get("merchant_city", ""),
        "availability": row.get("availability", "online"),
        "recurring_capable": row.get("recurring_capable", "false"),
        "known": True,
    }


def recent_attempts(seen: list[EnrichedEvent], now: datetime) -> int:
    """`recent_attempt_count_10m` as the platform defines it: every earlier attempt of this
    session inside the window, whatever its outcome, the current one excluded."""
    start = now - timedelta(minutes=VELOCITY_WINDOW_MINUTES)
    return sum(1 for ev in seen if start <= ev.timestamp < now)


def build_event(
    order: dict[str, Any],
    *,
    card_id: str,
    customer_id: str,
    device_id: str,
    mandate: dict[str, Any],
    merchants: dict[str, dict[str, str]],
    fx: dict[str, float],
    seen: list[EnrichedEvent],
    decided: dict[str, str],
    sequence: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One `authorization.request`, valid against the organizers' event schema.

    `decided` maps every earlier authorization of this errand to its decision string, so a
    re-quote can name the order it follows.
    """
    if not isinstance(order, dict):
        raise OrderError("the order must be an object")
    now = now or datetime.now(UTC)

    merchant = resolve_merchant(
        merchants, str(order.get("merchant") or ""), order.get("merchant_id") or None
    )
    currency = str(order.get("currency") or "CHF").upper()
    if currency not in fx:
        raise OrderError(f"currency must be one of {', '.join(sorted(fx))}")

    raw_items = order.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise OrderError("items must be a non-empty list of {name, quantity, unit_price}")
    if len(raw_items) > MAX_LINES:
        raise OrderError(f"at most {MAX_LINES} lines per order")
    catalogue = {
        normalize_name(name): category
        for category, names in item_catalogue().items()
        for name in names
    }
    items: list[dict[str, Any]] = []
    subtotal = Decimal("0")
    for line_no, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise OrderError(f"line {line_no} must be an object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise OrderError(f"line {line_no} needs a name")
        quantity = _int(raw.get("quantity"), f"line {line_no} quantity", default=1)
        unit_price = _money(raw.get("unit_price"), f"line {line_no} unit_price")
        subtotal += unit_price * quantity
        # The catalogue's category wins over the agent's: the data dictionary guarantees a
        # catalogue name carries its category, and the item checks read that field.
        category = catalogue.get(normalize_name(name)) or str(raw.get("category") or "unknown")
        items.append(
            {
                "line_no": line_no,
                "item_id": f"IT-AGENT-{hashlib.sha256(name.encode()).hexdigest()[:8].upper()}",
                "item_name": name,
                "item_category": category,
                "quantity": quantity,
                "unit_price": float(unit_price),
                "currency": currency,
                "item_details": str(raw.get("details") or ""),
            }
        )
    delivery_fee = _money(order.get("delivery_fee", 0), "delivery_fee")
    amount = (subtotal + delivery_fee).quantize(CENT)
    rate = Decimal(str(fx[currency]))
    billing = (amount * rate).quantize(CENT, rounding=ROUND_HALF_EVEN)

    related = str(order.get("related_authorization_id") or "") or None
    related_status: str | None = None
    if related:
        if related not in decided:
            raise OrderError(f"related_authorization_id {related} is not an order of this errand")
        # The wire vocabulary is the platform's, not the engine's: a declined predecessor is
        # what makes a re-quote a re-quote rather than a duplicate (adapters/parse.py).
        related_status = {"approve": "approved", "decline": "declined", "step_up": "step_up"}.get(
            decided[related], decided[related]
        )

    auth_id = f"AG{uuid.uuid4().hex[:10].upper()}"
    stamp = now.isoformat().replace("+00:00", "Z")
    known = bool(merchant.pop("known"))
    return {
        "type": "authorization.request",
        "request_id": f"req_{uuid.uuid4().hex[:12]}",
        "deadline_at": (now + timedelta(seconds=DECISION_SECONDS))
        .isoformat()
        .replace("+00:00", "Z"),
        "authorization": {
            "authorization_id": auth_id,
            "source_authorization_id": auth_id,
            "scenario_id": "agent",
            "replay_order": sequence,
            "mandate_id": str(mandate.get("mandate_id", "")),
            "profile_id": str(mandate.get("profile_id") or f"PROFILE_{customer_id}"),
            "card_id": card_id,
            "initiator_type": "agent",
            "merchant": merchant,
            "timestamp": stamp,
            "amount": float(amount),
            "currency": currency,
            "billing_amount_chf": float(billing),
            "items_subtotal": float(subtotal.quantize(CENT)),
            "delivery_fee": float(delivery_fee),
            "channel": "ecommerce",
            "customer_device_id": device_id,
            "authority_status": "active",
            "card_status_at_attempt": "active",
            "spend_in_period_before_chf": None,
            "recent_attempt_count_10m": recent_attempts(seen, now),
            "fulfillment_method": _choice(
                order.get("fulfillment"), "fulfillment", FULFILLMENT, "delivery"
            ),
            "delivery_by": None,
            "order_returnable": _choice(order.get("returnable"), "returnable", TRISTATE, "unknown"),
            "order_cancellable": _choice(
                order.get("cancellable"), "cancellable", TRISTATE, "unknown"
            ),
            "related_authorization_id": related,
            "related_authorization_status": related_status,
            "purchase_description": str(order.get("description") or "").strip(),
            "items": items,
        },
        "mandate": {
            "mandate_id": str(mandate.get("mandate_id", "")),
            "status": str(mandate.get("status", "active")),
            "customer_id": customer_id,
            "card_id": card_id,
            "instruction": str(mandate.get("instruction", "")),
            "hard_rules": list(mandate.get("hard_rules") or []),
            "uncertainty_policy": str(mandate.get("uncertainty_policy", "ask")),
            "profile_id": str(mandate.get("profile_id") or f"PROFILE_{customer_id}"),
        },
        "context": {"approved_spend_in_period_chf": None, "recent_authorizations": []},
        "runtime": {
            "received_at": stamp,
            "history_window_minutes": VELOCITY_WINDOW_MINUTES,
            "context_basis": "agent_errand",
            # Ours, not the schema's: whether the shop was found in the merchant records. The
            # explanation the agent reads mentions it when it was not.
            "merchant_known": known,
        },
    }
