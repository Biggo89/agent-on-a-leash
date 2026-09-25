"""The words an agent reads. Plain language over the IR, never a reason code.

An agent that cannot read the rules will find them by trial: three declines to learn a cap
that one sentence would have told it. So every tool answer carries a text a person could read
beside the structured object, and the text quotes the cardholder's own words where the IR
kept them (`provenance`) — the same "a rule that cannot quote you is not a rule" line the
review screen makes.
"""

from __future__ import annotations

from typing import Any

from ..domain.money import Money
from ..domain.provenance import quotes

UNCERTAINTY = {
    "ask": "when something is unclear, the cardholder is asked",
    "decline": "when something is unclear, the order is declined",
    "approve": "when something is unclear, the order goes through",
}

FIELD_NAMES = {
    "billing_amount_chf": "CHF",
    "merchant_country": "merchant country",
    "merchant_category": "kind of shop",
}


def chf(value: Any) -> str:
    try:
        return f"CHF {Money.from_value(value)}"
    except Exception:  # noqa: BLE001 — a label must never break a tool answer
        return f"CHF {value}"


def _quoted(carrier: dict[str, Any]) -> str:
    said = quotes(carrier)
    return f' (your words: "{said[0]}")' if said else ""


def rule_sentence(rule: dict[str, Any]) -> str:
    field = str(rule.get("field", ""))
    operator = str(rule.get("operator", ""))
    value = rule.get("value")
    scope = str(rule.get("scope", "purchase"))
    if field == "billing_amount_chf" and operator in ("<=", "<"):
        limit = chf(value)
        if scope == "period" and rule.get("period_days"):
            head = f"At most {limit} across any {int(rule['period_days'])} days"
        else:
            head = f"At most {limit} per order"
        if operator == "<":
            head = head.replace("At most", "Less than")
        return head + _quoted(rule)
    name = FIELD_NAMES.get(field, field.replace("_", " "))
    shown = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return f"{name} {operator} {shown}" + _quoted(rule)


def facet_sentence(facet: dict[str, Any]) -> str:
    kind = str(facet.get("kind", "")).replace("_", " ")
    require = facet.get("require") or {}
    parts = []
    for key, value in require.items():
        shown = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        parts.append(f"{str(key).replace('_', ' ')}: {shown}")
    body = f"{kind} — {'; '.join(parts)}" if parts else kind
    return body[0].upper() + body[1:] + _quoted(facet)


def mandate_sentences(ir: dict[str, Any]) -> list[str]:
    """Every rule and facet of an IR as one sentence each, hard rules first."""
    lines = [rule_sentence(r) for r in ir.get("rules") or ir.get("hard_rules") or []]
    lines += [facet_sentence(f) for f in ir.get("intent_facets") or []]
    policy = str(ir.get("uncertainty_policy") or "ask")
    lines.append(UNCERTAINTY.get(policy, UNCERTAINTY["ask"]).capitalize())
    return lines


def window_sentence(window: dict[str, Any] | None) -> str:
    if not window or window.get("period_days") is None:
        return ""
    spent = window.get("approved_spend_chf")
    limit = window.get("limit_chf")
    days = window.get("period_days")
    if limit:
        left = Money.from_value(limit) - Money.from_value(spent or "0")
        return (
            f"Spent so far in the {days}-day window: {chf(spent)} of {chf(limit)} "
            f"({chf(str(left))} left)"
        )
    return f"Spent so far in the {days}-day window: {chf(spent)}"


def decision_text(summary: dict[str, Any], *, seconds_remaining: float | None = None) -> str:
    """The one line an agent needs after `request_payment`."""
    decision = str(summary.get("decision"))
    message = str(summary.get("customer_message", ""))
    if decision == "approve":
        return f"APPROVED. {message}"
    if decision == "decline":
        return f"DECLINED. {message}"
    wait = (
        f" They have about {int(seconds_remaining)} seconds to answer."
        if seconds_remaining is not None
        else ""
    )
    return (
        f"PENDING — the cardholder has been asked on their phone.{wait} "
        f"Call payment_status with authorization_id {summary.get('authorization_id')} "
        f"to learn their answer. {message}"
    )


def status_text(status: dict[str, Any]) -> str:
    state = str(status.get("status"))
    where = f"{chf(status.get('amount_chf'))} at {status.get('merchant_name')}"
    if state == "pending":
        left = status.get("seconds_remaining")
        return (
            f"Still waiting for the cardholder: {where}. "
            f"About {int(left or 0)} seconds left before it expires."
        )
    if state == "expired":
        return f"Expired: {where}. The cardholder did not answer in time, so nothing was charged."
    if state == "approved":
        return f"Approved: {where}."
    return f"Declined: {where}. {status.get('customer_message', '')}".strip()
