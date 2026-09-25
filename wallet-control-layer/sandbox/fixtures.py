"""Load the organizers' data pack and turn fixture rows into schema-valid live events.

This mirrors what the private sandbox does at replay time. Keeping one source of truth (the
CSVs) is what makes offline development and the real API agree exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from leash.adapters.datapack import DataPack, data_dir

__all__ = ["DataPack", "build_event", "data_dir", "run_scoped_id"]


def _num(value: str) -> float:
    """CSV gives strings; the event schema wants numbers."""
    return float(value)


def _opt(value: str) -> str | None:
    """An empty optional CSV field is a missing value, i.e. null in the event."""
    return value if value else None


def run_scoped_id(fixture_id: str, run_id: str) -> str:
    """An authorization ID is stable within a run but gets a new suffix per run."""
    return f"{fixture_id}-{run_id.removeprefix('RUN_')}"


def build_event(
    pack: DataPack,
    attempt: dict[str, str],
    *,
    run_id: str,
    mandate: dict[str, Any],
    approved_spend_in_period_chf: float | None,
    recent_authorizations: list[dict[str, Any]],
    deadline_seconds: int,
    request_seq: int,
) -> dict[str, Any]:
    """Produce one `authorization.request` matching authorization_event.schema.json."""
    merchant = pack.merchants[attempt["merchant_id"]]
    related = _opt(attempt["related_authorization_id"])
    now = datetime.now(UTC)

    return {
        "type": "authorization.request",
        "request_id": f"req_{run_id}_{request_seq:04d}",
        "deadline_at": (now + timedelta(seconds=deadline_seconds))
        .isoformat()
        .replace("+00:00", "Z"),
        "authorization": {
            "authorization_id": run_scoped_id(attempt["authorization_id"], run_id),
            "source_authorization_id": attempt["authorization_id"],
            "scenario_id": attempt["scenario_id"],
            "replay_order": int(attempt["replay_order"]),
            "mandate_id": mandate["mandate_id"],
            "profile_id": mandate["profile_id"],
            "card_id": attempt["card_id"],
            "initiator_type": "agent",
            "merchant": {
                "merchant_id": merchant["merchant_id"],
                "merchant_name": merchant["merchant_name"],
                "merchant_category": merchant["merchant_category"],
                "merchant_mcc": merchant["merchant_mcc"],
                "merchant_country": merchant["merchant_country"],
                "merchant_city": merchant["merchant_city"],
                "availability": merchant["availability"],
                "recurring_capable": merchant["recurring_capable"],
            },
            "timestamp": attempt["timestamp"],
            "amount": _num(attempt["amount"]),
            "currency": attempt["currency"],
            "billing_amount_chf": _num(attempt["billing_amount_chf"]),
            "items_subtotal": _num(attempt["items_subtotal"]),
            "delivery_fee": _num(attempt["delivery_fee"]),
            "channel": attempt["channel"],
            "customer_device_id": attempt["customer_device_id"],
            "authority_status": attempt["authority_status"],
            "card_status_at_attempt": attempt["card_status_at_attempt"],
            # Null on every row of this pack: period tracking is deliberately the team's job.
            "spend_in_period_before_chf": None,
            "recent_attempt_count_10m": int(attempt["recent_attempt_count_10m"]),
            "fulfillment_method": attempt["fulfillment_method"],
            "delivery_by": _opt(attempt["delivery_by"]),
            "order_returnable": attempt["order_returnable"],
            "order_cancellable": attempt["order_cancellable"],
            # The live event rewrites a fixture link to the run-scoped ID.
            "related_authorization_id": run_scoped_id(related, run_id) if related else None,
            "related_authorization_status": _opt(attempt["related_authorization_status"]),
            "purchase_description": attempt["purchase_description"],
            "items": [
                {
                    "line_no": int(line["line_no"]),
                    "item_id": line["item_id"],
                    "item_name": line["item_name"],
                    "item_category": line["item_category"],
                    "quantity": int(line["quantity"]),
                    "unit_price": _num(line["unit_price"]),
                    "currency": line["currency"],
                    "item_details": line["item_details"],
                }
                for line in pack.items.get(attempt["authorization_id"], [])
            ],
        },
        "mandate": {
            "mandate_id": mandate["mandate_id"],
            "status": mandate["status"],
            "customer_id": mandate["customer_id"],
            "card_id": mandate["card_id"],
            "instruction": mandate["instruction"],
            "hard_rules": mandate["hard_rules"],
            "uncertainty_policy": mandate["uncertainty_policy"],
            "profile_id": mandate["profile_id"],
        },
        "context": {
            "approved_spend_in_period_chf": approved_spend_in_period_chf,
            "recent_authorizations": recent_authorizations,
        },
        "runtime": {
            "received_at": now.isoformat().replace("+00:00", "Z"),
            "history_window_minutes": 10,
            "context_basis": "run_decisions_and_scenario_timestamps",
        },
    }
