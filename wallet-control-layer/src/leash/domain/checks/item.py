"""Item checks: is the customer's actual request in this cart?

The first of three item checks, and the boundaries matter or they double-fire:

    item_matches_request  is what was asked for present AT ALL?   (helmet instead of shoes)
    item_attributes       is the matching item the right variant? (size 42 instead of 43)
    unrequested_addon     is there anything ELSE in the cart too? (shoes plus a service plan)

All three agree on which line is "the requested item" by sharing `matching_lines`.

So this check passes as soon as one line matches. AU0018 (shoes + protection plan) and AU0007
(groceries + a cosmetics gift) both contain what the customer asked for; their extra lines
belong to `unrequested_addon`.

Spec: specs/check-item-matches-request.md
"""

from __future__ import annotations

import re
from typing import Any

from ..money import Money, to_chf
from ..policy import facet, requirement
from ..sanitize import safe_display
from ..types import CheckResult, EnrichedEvent, Evidence, Verdict

_TOKENS = re.compile(r"[a-z0-9]+")


def name_tokens(name: str) -> set[str]:
    """Normalise a cart line's name to comparable tokens: lowercase, punctuation dropped.

    Public because the compile guard tokenises keywords with it (specs/llm-compiler.md, rail
    5a): a keyword is only ever compared against these tokens, so it must be cut the same way.
    """
    return set(_TOKENS.findall((name or "").lower()))


def matching_lines(ev: EnrichedEvent, policy: dict[str, Any]) -> tuple[list[int], list[int]]:
    """Indices of cart lines that match the identity requirement, and those merely in category.

    Shared by `item_matches_request` and `item_attributes` so the two cannot drift apart: a
    line the identity check considered "the requested item" is exactly the line whose
    attributes the attribute check inspects.

    Returns ``(matching, in_category)``. With no identity facet, every line matches.
    """
    required = facet(policy, "item_identity")
    if required is None:
        every = list(range(len(ev.items)))
        return every, every

    return lines_matching(ev.items, required)


def lines_matching(items: tuple[Any, ...], required: dict[str, Any]) -> tuple[list[int], list[int]]:
    """The same predicate over a bare cart, so a *prior* order can be tested against it.

    `check_goal_fulfilled` asks whether an order already approved in this run contained the
    requested item, and it must reach that answer the same way `item_matches_request` reached
    its own — one predicate, or the two drift and the engine starts disagreeing with itself
    about what "the thing you asked for" is.
    """
    allowed = [str(c) for c in requirement(required, "item_category_in", []) or []]
    keywords = [str(k).lower() for k in requirement(required, "item_keywords_all", []) or []]

    matching: list[int] = []
    in_category: list[int] = []
    for index, line in enumerate(items):
        category_ok = not allowed or str(line.get("item_category", "")) in allowed
        if category_ok:
            in_category.append(index)
            if all(k in name_tokens(str(line.get("item_name", ""))) for k in keywords):
                matching.append(index)
    return matching, in_category


def check_item_matches_request(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """ "Replace my worn road-running shoes." / "Buy the 27-inch monitor I chose."

    A line matches when its category is one the customer asked for AND every required keyword
    appears in its name. Keywords are matched against `item_name`, never `item_details`: the
    data dictionary guarantees `item_name` matches the catalogue row, while `item_details` is
    free merchant text — the field SCEN0004's injections live in. Matching identity there
    would let a merchant relabel a trail shoe as a road shoe in its own description.
    """
    required = facet(policy, "item_identity")
    if required is None:
        return CheckResult("item_matches_request", Verdict.NOT_APPLICABLE)

    allowed = [str(c) for c in requirement(required, "item_category_in", []) or []]
    keywords = [str(k).lower() for k in requirement(required, "item_keywords_all", []) or []]
    wanted = str(requirement(required, "item_description", "") or "") or "what you asked for"

    if not allowed and not keywords:
        return CheckResult(
            "item_matches_request",
            Verdict.UNKNOWN,
            "insufficient_evidence",
            (Evidence("item_identity_required", "(unspecified)"),),
            "what you asked to buy could not be established from your instruction",
        )

    matched, in_category = matching_lines(ev, policy)

    found = ", ".join(
        sorted({f'"{safe_display(str(li.get("item_name", "")))}"' for li in ev.items})
    )
    evidence = (
        Evidence("requested", wanted),
        Evidence("cart_items", found or "(empty)"),
        Evidence("item_category_allowed", ", ".join(allowed) if allowed else "(any)"),
        *((Evidence("item_keywords_required", ", ".join(keywords)),) if keywords else ()),
    )

    if matched:
        names = ", ".join(
            f'"{safe_display(str(ev.items[i].get("item_name", "")))}"' for i in matched
        )
        return CheckResult(
            "item_matches_request",
            Verdict.PASS,
            "item_matches_request",
            evidence,
            f"this order contains {names}, which is what you asked for",
        )

    # Nothing in the cart is even the right kind of thing: the cart is about something else.
    if not in_category:
        return CheckResult(
            "item_matches_request",
            Verdict.VIOLATION,
            "cart_contradicts_purpose",
            evidence,
            f"this order is for {found or 'nothing recognisable'}, not the {wanted} you asked for",
        )

    # Right kind of thing, wrong product.
    return CheckResult(
        "item_matches_request",
        Verdict.VIOLATION,
        "item_not_requested",
        evidence,
        f"this order is for {found}, not the {wanted} you asked for",
    )


def check_item_attributes(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """ "…in size 43."

    Inspects only the lines `item_matches_request` matched. AU0018 pairs a size-43 shoe with an
    "Extended protection plan" that states no size — a plan has no size and never will, so
    comparing it against the requirement would produce a spurious uncertainty on an order that
    is fine.

    The size comes from untrusted merchant text, which is legitimate typed fact extraction:
    `extract_item_facts` returns `int | None` and cannot be argued into emitting a verdict.
    """
    required = facet(policy, "item_attribute")
    if required is None:
        return CheckResult("item_attributes", Verdict.NOT_APPLICABLE)

    wanted_size = requirement(required, "size")
    if wanted_size is None:
        return CheckResult(
            "item_attributes",
            Verdict.UNKNOWN,
            "item_attributes_unknown",
            (Evidence("item_attribute_required", "(unspecified)"),),
            "the item details your instruction requires could not be established",
        )
    wanted_size = int(wanted_size)

    matched, _ = matching_lines(ev, policy)
    if facet(policy, "item_identity") is not None and not matched:
        # item_matches_request has already declined; a second reason here would be noise.
        return CheckResult("item_attributes", Verdict.NOT_APPLICABLE)

    facts = ev.enrichment.item_facts
    stated = [(i, facts[i].size) for i in matched if i < len(facts) and facts[i].size is not None]
    base = (Evidence("size_required", str(wanted_size)),)

    if not stated:
        return CheckResult(
            "item_attributes",
            Verdict.UNKNOWN,
            "item_attributes_unknown",
            base,
            f"the seller did not state a size, and you asked for size {wanted_size}",
        )

    # Strict: any mismatching line fails. A cart holding both sizes is a pending return,
    # not a fulfilled request.
    wrong = [(i, size) for i, size in stated if size != wanted_size]
    if wrong:
        index, size = wrong[0]
        name = safe_display(str(ev.items[index].get("item_name", "")))
        return CheckResult(
            "item_attributes",
            Verdict.VIOLATION,
            "item_attribute_mismatch",
            (*base, Evidence("size_offered", str(size)), Evidence("item_name", name)),
            f"this order is for size {size}, and you asked for size {wanted_size}",
            f"An order in size {wanted_size} would match.",
        )

    return CheckResult(
        "item_attributes",
        Verdict.PASS,
        "item_attributes_match",
        (*base, Evidence("size_offered", str(stated[0][1]))),
        f"this order is in size {wanted_size}, as you asked",
    )


# Weight lives in evaluator.CONCERN_WEIGHTS['unrequested_addon'].


def check_unrequested_addon(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """ "Do not add anything I did not ask for." — stated in SCEN0004, implied elsewhere.

    Only SCEN0004 states the rule outright; SCEN0001 and SCEN0002 imply the scope by naming
    what to buy. The check honours that difference: a stated rule is a violation, an inferred
    scope is a concern that escalates to the customer.

    AU0018 is why. Shoes at CHF 165 plus a CHF 29 plan is CHF 194 — under the cap, right
    retailer, right size, 30-day returns. Declining it would leave the customer without the
    shoes they asked for because a seller bundled a service onto them. Asking keeps the shoes
    reachable and the choice theirs.
    """
    if facet(policy, "item_identity") is None:
        return CheckResult("unrequested_addon", Verdict.NOT_APPLICABLE)

    matched, _ = matching_lines(ev, policy)
    if not matched:
        # Every line would technically be an addition, but that is item_matches_request's
        # finding, already made with a better message.
        return CheckResult("unrequested_addon", Verdict.NOT_APPLICABLE)

    extra = [i for i in range(len(ev.items)) if i not in matched]
    if not extra:
        # No reason code: a clean multi-line order should not clutter its own approval.
        return CheckResult("unrequested_addon", Verdict.PASS)

    names: list[str] = []
    total = Money.zero()
    for index in extra:
        line = ev.items[index]
        names.append(f'"{safe_display(str(line.get("item_name", "")))}"')
        try:
            unit = to_chf(line.get("unit_price", 0), str(line.get("currency", "CHF")))
            total = total + unit * int(line.get("quantity", 1))
        except Exception:  # a malformed line must not stop the finding being reported
            continue

    listed = ", ".join(names)
    evidence = (
        Evidence("unrequested_items", listed),
        Evidence("unrequested_total_chf", str(total)),
        Evidence("unrequested_line_count", str(len(extra))),
    )
    detail = f"this order includes {listed}, costing CHF {total}, which you did not ask for"

    if facet(policy, "no_additions") is not None:
        return CheckResult(
            "unrequested_addon", Verdict.VIOLATION, "unrequested_addon", evidence, detail
        )
    return CheckResult(
        "unrequested_addon",
        Verdict.CONCERN,
        "unrequested_addon",
        evidence,
        detail,
    )


# Weight lives in evaluator.CONCERN_WEIGHTS['goal_already_fulfilled'] — read only when the
# customer has asked to be questioned about repeats. The default records and does not escalate.

#: What to do when the thing the customer asked for has already been bought on this errand.
#: `note` records the finding in the evidence and the approval's reason codes; `ask` makes it
#: a concern that escalates. specs/check-goal-fulfilled.md, "Why the default is `note`".
REPEAT_ACTIONS = ("note", "ask")


def check_goal_fulfilled(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """ "Buy **the** 27-inch monitor I chose." — and then a second one.

    SCEN0004 approves four monitors on an instruction that names one, CHF 1,430.40 against a
    single errand; SCEN0002 approves three pairs of the same shoes. Every one of those orders
    is compliant on its own facts, which is exactly why nothing else on the board sees them:
    `duplicate_order` needs an identical cart *and* amount at the same seller, so a second
    monitor from a different shop at a different price walks straight past it.

    **The gate is `item_keywords_all`, and it is the whole design.** An instruction that names
    a *thing* compiles keywords ("road"/"running", "27"/"inch"); one that names a *kind of
    thing* — "our household groceries", "clothing for me" — compiles a category and none. Only
    the first kind can be finished. Without that gate this check would report the second
    grocery delivery of the week as a completed goal, which is the most ordinary purchase in
    the pack.

    Spec: specs/check-goal-fulfilled.md
    """
    required = facet(policy, "item_identity")
    if required is None:
        return CheckResult("goal_fulfilled", Verdict.NOT_APPLICABLE)

    # A category is not a goal: "groceries" is never finished.
    if not (requirement(required, "item_keywords_all", []) or []):
        return CheckResult("goal_fulfilled", Verdict.NOT_APPLICABLE)

    # A duplicate is already reported, with a sharper sentence: "you approved this same
    # CHF 289.00 order 25 minutes ago" beats "this is the second monitor". Same precedent as
    # check_split_order and check_unrequested_addon standing down for a better message.
    if ev.enrichment.is_duplicate_of is not None:
        return CheckResult("goal_fulfilled", Verdict.NOT_APPLICABLE)

    matching, _ = matching_lines(ev, policy)
    if not matching:
        # This order is not the requested thing at all, which is item_matches_request's
        # finding. Nothing here to say about a goal it does not advance.
        return CheckResult("goal_fulfilled", Verdict.NOT_APPLICABLE)

    earlier = [
        auth_id
        for auth_id, items in ev.enrichment.approved_carts
        if lines_matching(items, required)[0]
    ]
    if not earlier:
        # No reason code: the first order of the thing asked for must not carry a note about
        # repeats it is not.
        return CheckResult("goal_fulfilled", Verdict.PASS)

    ordinal = len(earlier) + 1
    wanted = safe_display(
        str(requirement(required, "item_description", "") or "what you asked for")
    )
    evidence = (
        Evidence("goal_fulfilled_by", ", ".join(earlier)),
        Evidence("orders_of_requested_item", str(ordinal)),
        Evidence("requested_item", wanted),
    )
    detail = f"you have already bought the {wanted} on this errand; this is order {ordinal}"

    if str(policy.get("repeat_purchase_action", "note")) == "ask":
        return CheckResult(
            "goal_fulfilled",
            Verdict.CONCERN,
            "goal_already_fulfilled",
            evidence,
            detail,
            "You asked to be checked before a repeat of something already bought.",
        )
    # Recorded, not enforced. The finding is on the record and in the approval's reason codes;
    # the decision is unchanged, because the customer stated a limit and a seller, not a count.
    return CheckResult("goal_fulfilled", Verdict.PASS, "goal_already_fulfilled", evidence, detail)
