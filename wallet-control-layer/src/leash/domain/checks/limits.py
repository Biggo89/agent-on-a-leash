"""The limit checks: per-order, rolling-period, and the pattern that walks past both.

`check_per_order_limit` and `check_period_limit` exist as worked examples of the check
contract — pure function, one verdict, a reason code from the closed vocabulary, evidence
naming the observed field. Copy that shape for every new check; spec it first
(specs/TEMPLATE.md).

Both are mechanical: they read a threshold straight out of the mandate's hard_rules and
compare it against a CHF amount. Nothing there is a judgement call, which is exactly why they
are safe to ship as reference implementations.

`check_split_order` is the third, and it is not mechanical. A per-order cap constrains one
order, so two orders minutes apart at the same shop walk past it while satisfying it twice
over. That check reads the run's own memory rather than a single field, and it reports a
concern rather than a breach — the customer wrote a cap, not a rule against shopping twice.
Spec: specs/check-split-order.md.
"""

from __future__ import annotations

from typing import Any

from ..money import Money
from ..policy import binding_cap, period_caps
from ..provenance import attribution, source_of
from ..sanitize import safe_display
from ..types import CheckResult, EnrichedEvent, Evidence, Verdict


def _as_written(limit: Money, rule: dict[str, Any]) -> str:
    """The limit in the currency the customer wrote it, then the CHF figure the check used.

    "EUR 200.00 (CHF 190.00)" for a converted cap, "CHF 190.00" for everything else. A customer
    who wrote EUR 200 and reads of a CHF 190 limit concludes the engine is wrong.
    specs/llm-compiler.md §"Foreign-currency caps".
    """
    stated = rule.get("stated")
    if isinstance(stated, dict) and stated.get("currency") not in (None, "CHF"):
        written = f"{stated['currency']} {Money.from_value(stated['amount'])} (CHF {limit})"
    else:
        written = f"CHF {limit}"
    # A cap derived from a price per unit shows its arithmetic: the customer wrote "CHF 200
    # per night", and reading of a CHF 600 limit without the "3 nights" beside it looks like
    # a number nobody wrote. specs/llm-compiler.md §"Derived caps".
    derived = rule.get("derived")
    if isinstance(derived, dict) and derived.get("count") and derived.get("unit"):
        unit_price = Money.from_value(derived.get("unit_amount", 0))
        written += (
            f" ({derived['count']} {derived['unit']}s × "
            f"{derived.get('unit_currency', 'CHF')} {unit_price})"
        )
    return written


def _threshold(hard_rules: list[dict[str, Any]], scope: str) -> tuple[Money, dict[str, Any]] | None:
    """The cap that binds this scope — the tightest, not the first. decision-rules.md §9.

    `PATCH` is tighten-only, so narrowing a mandate adds a rule beside the old one. Reading
    the first match would answer with the cap the customer replaced, and would make the
    decision depend on the order of a JSON list.
    """
    found = binding_cap(hard_rules, scope)
    if found is None:
        return None
    return Money.from_value(found[0]["value"]), found[0]


def check_per_order_limit(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """Per-order cap.

    Compares ``billing_amount_chf`` — never ``amount``. Fixture AU0032 is 260.00 EUR =
    247.00 CHF against a 250 cap; comparing the row currency declines a legitimate purchase.

    The cap applies to ``amount`` as a whole, which the schema guarantees equals
    ``items_subtotal + delivery_fee``. "CHF 120 including delivery" therefore needs no special
    handling — but ``items_subtotal`` alone would be the wrong field.
    """
    found = _threshold(policy.get("hard_rules", []), "purchase")
    if found is None:
        return CheckResult("per_order_limit", Verdict.NOT_APPLICABLE)

    limit, rule = found
    strict = rule.get("operator") == "<"
    over = ev.billing_amount_chf > limit if not strict else ev.billing_amount_chf >= limit
    # The layer the binding cap came from. Empty for an instruction-sourced rule, because
    # "your CHF 200 per-order limit" already refers to words the customer wrote; named for
    # every other layer, or they read their instruction, see a different number, and conclude
    # the engine is wrong. specs/customer-settings.md §5.
    whose = attribution(rule)
    # "your CHF 200 limit" reads well when the limit is theirs by authorship; once the clause
    # names another layer, the possessive belongs to that clause and not to the noun.
    mine = "your " if not whose else "the "
    evidence = (
        Evidence("billing_amount_chf", str(ev.billing_amount_chf)),
        Evidence("per_order_limit_chf", str(limit)),
        Evidence("per_order_limit_source", source_of(rule)),
    )
    if over:
        return CheckResult(
            "per_order_limit",
            Verdict.VIOLATION,
            "per_order_limit_exceeded",
            evidence,
            f"CHF {ev.billing_amount_chf} exceeds the {_as_written(limit, rule)} per-order limit"
            f"{whose}",
            # A remedy names the threshold, never a promise of approval: every other check
            # also ran. specs/customer-message.md §4.
            f"An order of {_as_written(limit, rule)} or less would be within your limit.",
        )
    # Boundary: "at or below CHF 120" is `<=`, so an exact-limit order passes (fixture AU0003).
    return CheckResult(
        "per_order_limit",
        Verdict.PASS,
        "within_per_order_limit",
        evidence,
        f"within {mine}{_as_written(limit, rule)} per-order limit{whose}",
    )


def check_period_limit(
    ev: EnrichedEvent, policy: dict[str, Any]
) -> CheckResult | tuple[CheckResult, ...]:
    """Rolling-period caps — **one result per window the policy states**.

    specs/decision-rules.md §2.1 and §9. A mandate may carry more than one period rule and
    every one is enforced: `CHF 300 / 7 days` and `CHF 1000 / 30 days` are two constraints
    over the same ledger, neither strictly safer than the other, so a purchase must satisfy
    both. Reporting them separately is what lets a customer inside their weekly limit and over
    their monthly one be told which.

    Each result reads the window belonging to **its own** rule. Enrichment computes the set
    (`Enrichment.approved_spend_windows`); a window missing from it is `unknown`, never zero,
    because a missing figure read as "nothing spent yet" approves everything.

    The figures come from the rolling window in ``domain.ledger`` — final approvals only,
    lower bound inclusive, upper exclusive. A cumulative total since run start declines
    fixture AU0011 at 387.50/300 when the true in-window figure is 223.00/300.
    """
    caps = period_caps(policy.get("hard_rules", []))
    if not caps:
        return CheckResult("period_limit", Verdict.NOT_APPLICABLE)
    return tuple(_one_window(ev, days, rule) for days, rule in caps)


def _one_window(ev: EnrichedEvent, days: int | None, rule: dict[str, Any]) -> CheckResult:
    limit = Money.from_value(rule["value"])
    whose = attribution(rule)
    source = Evidence("period_limit_source", source_of(rule))

    # A period rule with no window is not a window. Before multi-window enrichment it scored
    # zero days, won the shortest-window contest and disabled period enforcement outright — a
    # weekly CHF 300 ceiling silently became a per-order CHF 1000 one.
    already = ev.enrichment.spend_in(days) if days is not None else None
    if days is None or already is None:
        return CheckResult(
            "period_limit",
            Verdict.UNKNOWN,
            "insufficient_evidence",
            (
                Evidence("period_limit_chf", str(limit)),
                Evidence("period_days", str(days) if days is not None else "(unstated)"),
                source,
            ),
            "how much of this limit you have already used could not be established",
        )

    projected = already + ev.billing_amount_chf
    evidence = (
        Evidence(f"approved_spend_{days}d_chf", str(already)),
        Evidence("projected_total_chf", str(projected)),
        Evidence("period_limit_chf", str(limit)),
        source,
    )
    if projected > limit:
        return CheckResult(
            "period_limit",
            Verdict.VIOLATION,
            "period_limit_exceeded",
            evidence,
            f"CHF {projected} would exceed the {_as_written(limit, rule)} limit across {days} "
            f"days{whose}",
            # The only sentence in the build that says the window *rolls* — which is the
            # mechanism a cumulative counter gets wrong (GUIDELINES.md §8, fixture AU0011).
            f"CHF {limit - already if limit > already else Money.zero()} of your "
            f"{_as_written(limit, rule)} "
            f"is still available in this {days}-day window, and it rises again as earlier "
            f"orders age out.",
        )
    return CheckResult(
        "period_limit",
        Verdict.PASS,
        "within_period_limit",
        evidence,
        f"CHF {projected} of {_as_written(limit, rule)} across {days} days",
    )


# Weight lives in evaluator.CONCERN_WEIGHTS['split_order_suspected'].


def check_split_order(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """Two orders at one shop, minutes apart, together over the per-order cap.

    "Keep each order at or below CHF 120 including delivery."

    AU0005 (CHF 70.00, 17:20) and AU0006 (CHF 65.00, 17:26) are both inside that cap and
    together are CHF 135.00. Before this check the engine approved both, and the cap was worth
    nothing to anyone willing to press the button twice.

    `check_duplicate_order` deliberately stays out of this — identical carts only — and
    deferred it to the rolling limit. That deferral under-covers: only SCEN0001 states a
    period rule, so in three of the five scenarios nothing watched the total at all. Even in
    SCEN0001 the period limit caught the consequence three orders later without ever naming
    the cause, and declined AU0008, an ordinary grocery order, on the CHF 65.00 that should
    never have entered the ledger.

    A concern, not a violation: each order is compliant on its own facts and the *pattern* is
    the finding, the same test applied by `merchant_lookalike` and `unrequested_addon`.

    Spec: specs/check-split-order.md
    """
    found = _threshold(policy.get("hard_rules", []), "purchase")
    if found is None:
        return CheckResult("split_order", Verdict.NOT_APPLICABLE)

    recent = ev.enrichment.same_merchant_recent
    if not recent:
        # No reason code: an ordinary single order must not lengthen its own approval.
        return CheckResult("split_order", Verdict.PASS)

    # A duplicate is already a step-up with a better message — "you approved this same order
    # 25 minutes ago". Two codes for one event would read as a splitting accusation about an
    # order the customer simply placed twice. Same precedent as check_unrequested_addon
    # standing down when item_matches_request has already made the finding.
    if ev.enrichment.is_duplicate_of is not None:
        return CheckResult("split_order", Verdict.NOT_APPLICABLE)

    limit, rule = found
    total = ev.billing_amount_chf
    for _, amount in recent:
        total = total + amount

    strict = rule.get("operator") == "<"
    over = total >= limit if strict else total > limit
    whose = attribution(rule)
    evidence = (
        Evidence("split_order_ids", ", ".join(auth_id for auth_id, _ in recent)),
        Evidence("split_order_count", str(len(recent) + 1)),
        Evidence("split_order_total_chf", str(total)),
        Evidence("per_order_limit_chf", str(limit)),
        Evidence("per_order_limit_source", source_of(rule)),
    )
    if not over:
        # Boundary: equality passes, matching check_per_order_limit — two orders that land
        # exactly on the cap have not exceeded anything.
        return CheckResult("split_order", Verdict.PASS, None, evidence)

    orders = len(recent) + 1
    return CheckResult(
        "split_order",
        Verdict.CONCERN,
        "split_order_suspected",
        evidence,
        f"{orders} orders at {safe_display(ev.merchant_name)} within a few minutes come to "
        f"CHF {total}, against the CHF {limit} per-order limit{whose}",
        # The reassurance, not a remedy: nothing was breached, and the customer may simply
        # have ordered twice. specs/customer-message.md §4.
        "Each order is within your limit on its own.",
    )
