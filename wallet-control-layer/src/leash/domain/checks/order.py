"""Order-level checks: return terms, and whether this order has been placed already.

The requirement splits across two sources with two different trust levels:

    whether returns exist  -> authorization.order_returnable   (platform, trusted)
    for how long           -> items[].item_details             (merchant, untrusted)

The duration exists nowhere else in the event, so parsing it is legitimate fact extraction
(specs/decision-rules.md §3) — but the trusted field decides *whether*, and a merchant cannot
write its way past `order_returnable="false"` in a product description.

Specs: specs/check-order-terms.md, specs/check-duplicate-order.md
"""

from __future__ import annotations

from typing import Any

from ..policy import facet, requirement
from ..sanitize import safe_display
from ..types import CheckResult, EnrichedEvent, Evidence, Verdict

# A merchant claiming "final sale" states a window, and that window is zero days.
FINAL_SALE_DAYS = 0


def check_order_terms(
    ev: EnrichedEvent, policy: dict[str, Any]
) -> CheckResult | tuple[CheckResult, ...]:
    """ "…only if the order can be returned within 14 days or more." / "…refundable rate only."

    Runs only when the instruction states an order term. That gating is load-bearing: all ten
    SCEN0001 grocery orders carry `order_returnable="false"` because groceries are not
    returnable, and an ungated version of this check would decline every one of them.

    **One result per term the facet states**, returns first, the way `check_category_exclusion`
    reports each dimension. A booking that is refundable but cannot be returned is one finding.
    A booking that is neither is two, and reporting it as one would hide half of it. A facet
    stating only `cancellable` is not judged on returns at all. A facet stating neither key
    keeps the old `return_terms_unknown`.
    """
    required = facet(policy, "order_terms")
    if required is None:
        return CheckResult("order_terms", Verdict.NOT_APPLICABLE)

    cancellable = requirement(required, "cancellable") is True
    if not cancellable:
        return _return_terms(ev, required)
    if requirement(required, "return_window_days_min") is None:
        return _cancellation_terms(ev)
    return (_return_terms(ev, required), _cancellation_terms(ev))


def _cancellation_terms(ev: EnrichedEvent) -> CheckResult:
    """ "Refundable rate only" — read off the trusted `order_cancellable`, never the name.

    The live pack sells "Hotel room, refundable rate" (IT0171) beside "Hotel room,
    non-refundable rate" (IT0172), and a name match for `refundable` hits both, because
    `non-refundable` splits into `non` and `refundable`. The platform's own field is the fact,
    and the catalogue says so: "cancellation terms are carried on the order".
    specs/check-order-terms.md §"Cancellation".
    """
    cancellable = ev.order_cancellable
    evidence = (
        Evidence("order_cancellable", cancellable),
        Evidence("cancellable_required", "true"),
    )
    if cancellable == "true":
        return CheckResult(
            "order_terms",
            Verdict.PASS,
            "order_cancellable",
            evidence,
            "this order can be cancelled for a refund, as you asked",
        )
    if cancellable in ("false", "not_applicable"):
        return CheckResult(
            "order_terms",
            Verdict.VIOLATION,
            "order_not_cancellable",
            evidence,
            "this order cannot be cancelled for a refund, and you asked for a refundable one"
            if cancellable == "false"
            else "cancellation does not apply to this kind of order, and you asked for a "
            "refundable one",
            "The same order at a refundable rate would meet your terms.",
        )
    # "unknown" means the term was not stated. That is uncertainty, never a negative.
    return CheckResult(
        "order_terms",
        Verdict.UNKNOWN,
        "cancellation_terms_unknown",
        evidence,
        "the seller did not state whether this order can be cancelled for a refund",
    )


def _return_terms(ev: EnrichedEvent, required: dict[str, Any]) -> CheckResult:
    """The return window, exactly as it was judged before cancellation existed."""
    min_days = requirement(required, "return_window_days_min")
    returnable = ev.order_returnable
    base: tuple[Evidence, ...] = (Evidence("order_returnable", returnable),)

    if min_days is None:
        return CheckResult(
            "order_terms",
            Verdict.UNKNOWN,
            "return_terms_unknown",
            base,
            "the return window your instruction requires could not be established",
        )
    min_days = int(min_days)
    base = (*base, Evidence("return_window_days_required", str(min_days)))

    # The trusted field is authoritative for whether returns exist at all.
    if returnable == "false":
        return CheckResult(
            "order_terms",
            Verdict.VIOLATION,
            "order_not_returnable",
            base,
            f"this order cannot be returned, and you asked for at least {min_days} days",
            f"A seller offering {min_days} days or more would meet your terms.",
        )
    if returnable == "not_applicable":
        return CheckResult(
            "order_terms",
            Verdict.VIOLATION,
            "order_not_returnable",
            base,
            f"returns do not apply to this kind of order, and you asked for at least "
            f"{min_days} days",
            f"A seller offering {min_days} days or more would meet your terms.",
        )
    # "unknown" means the term was not stated. That is uncertainty, never a negative.
    if returnable != "true":
        return CheckResult(
            "order_terms",
            Verdict.UNKNOWN,
            "return_terms_unknown",
            base,
            "the seller did not state whether this order can be returned",
        )

    stated: list[int] = []
    for line in ev.enrichment.item_facts:
        if line.final_sale:
            stated.append(FINAL_SALE_DAYS)
        elif line.return_window_days is not None:
            stated.append(line.return_window_days)
    if not stated:
        return CheckResult(
            "order_terms",
            Verdict.UNKNOWN,
            "return_terms_unknown",
            base,
            "the seller did not state how long this order can be returned within",
        )

    # An order is only as returnable as its least returnable line.
    window = min(stated)
    evidence = (*base, Evidence("return_window_days", str(window)))

    if window < min_days:
        return CheckResult(
            "order_terms",
            Verdict.VIOLATION,
            "return_window_too_short",
            evidence,
            f"this order can only be returned within {window} days, and you asked for at "
            f"least {min_days}",
            f"A seller offering {min_days} days or more would meet your terms.",
        )
    # Boundary: "14 days or more" is >=, so exactly the minimum satisfies it (AU0019).
    return CheckResult(
        "order_terms",
        Verdict.PASS,
        "order_terms_acceptable",
        evidence,
        f"this order can be returned within {window} days, meeting your {min_days}-day minimum",
    )


# Weight lives in evaluator.CONCERN_WEIGHTS['duplicate_order'].


def check_duplicate_order(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """Has this exact order already been placed — or is it a legitimate re-quote?

    The platform's own velocity fields cannot answer this: `recent_attempt_count_10m` and
    `context.recent_authorizations` are ten-minute windows, and AU0036 arrives 25 minutes after
    AU0035. The answer comes from our own run-scoped memory instead.

    Platform redelivery is a different thing and is already handled by idempotency, so anything
    reaching this check carries a NEW authorization id: a genuinely new order that happens to
    be identical. The customer might actually want it, so this escalates rather than refuses.

    AU0042 is the counterpart: a re-quote at a lower price after a decline. Penalising a retry
    is over-blocking, and the brief lists it among the attempts that look alarming and are
    legitimate — so it earns an explicit positive reason code rather than silence.
    """
    duplicate = ev.enrichment.is_duplicate_of
    if duplicate:
        return CheckResult(
            "duplicate_order",
            Verdict.CONCERN,
            "duplicate_order",
            (
                Evidence("duplicate_of_authorization_id", duplicate),
                Evidence("merchant_name", safe_display(ev.merchant_name)),
                Evidence("billing_amount_chf", str(ev.billing_amount_chf)),
            ),
            f"you already placed this same CHF {ev.billing_amount_chf} order at "
            f"{safe_display(ev.merchant_name)} a short time ago",
        )

    requote = ev.enrichment.is_requote_of
    if requote:
        return CheckResult(
            "duplicate_order",
            Verdict.PASS,
            "legitimate_requote",
            (
                Evidence("requote_of_authorization_id", requote),
                Evidence("related_authorization_status", ev.related_authorization_status or ""),
                Evidence("billing_amount_chf", str(ev.billing_amount_chf)),
            ),
            "this is a new price for an order that was previously declined, not a repeat",
        )

    # No reason code: an ordinary first order should not clutter its own approval.
    return CheckResult("duplicate_order", Verdict.PASS)
