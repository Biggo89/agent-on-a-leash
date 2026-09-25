"""Merchant checks: is this seller permitted, the right kind of shop, or impersonating one?

Both read `merchant_id` and history, never `merchant_name`, for the identity decision.
The name is used only to detect that it *resembles* a trusted one — a fact about the text,
never a basis for trusting it.

Specs: specs/check-merchant-permitted.md, specs/check-merchant-type.md,
       specs/check-merchant-lookalike.md
"""

from __future__ import annotations

from typing import Any

from ..policy import facet, requirement
from ..sanitize import safe_display
from ..types import CheckResult, EnrichedEvent, Evidence, Verdict

# Weight lives in evaluator.CONCERN_WEIGHTS['merchant_lookalike'].


def check_merchant_permitted(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """ "…from shops I have used before."

    Familiarity is counted from approved history for this (card_id, merchant_id) — declines
    are attempts, not a relationship. The count is not on the event; the engine indexes the
    historical file itself.

    Applies ONLY when the instruction states a familiarity requirement. SCEN0002 demands a
    *specialist sports retailer*, not a *familiar* one, so this check stands down there and
    fixture AU0023 (unfamiliar, fully compliant) is not blocked by it.

    Three outcomes, not two. The card is the unit of *counting*; the person is the unit the
    customer's sentence is *about*. When those disagree the honest answer is that we do not
    know, not a refusal — see the comment on the unknown branch and AU0044.
    """
    required = facet(policy, "merchant_familiarity")
    if required is None:
        return CheckResult("merchant_permitted", Verdict.NOT_APPLICABLE)

    minimum = int(requirement(required, "prior_approvals_min", 1))
    seen = ev.enrichment.merchant_prior_approvals
    evidence = (
        Evidence("merchant_id", ev.merchant_id),
        Evidence("merchant_prior_approvals", str(seen)),
        Evidence("prior_approvals_required", str(minimum)),
    )

    if ev.enrichment.familiarity_basis == "run":
        return _without_history(ev, minimum, seen, evidence)

    if seen >= minimum:
        # Boundary: "used before" is satisfied by meeting the minimum, not exceeding it.
        return CheckResult(
            "merchant_permitted",
            Verdict.PASS,
            "merchant_familiar",
            evidence,
            f"{safe_display(ev.merchant_name)} is a shop you have used before "
            f"({seen} previous purchases)",
        )

    # Card or person. The cardholder wrote "shops I have used before" about *themselves*, and
    # 17 of the 20 pack customers hold more than one card. A shop this card has not visited
    # but the person has is genuinely ambiguous — and "Ask me when uncertain" is the sentence
    # they wrote for exactly this. Unknown routes through uncertainty_policy like every other
    # unestablished fact, so no new branch reaches combine().
    #
    # Never a pass: reading the person's history as permission would quietly widen the rule
    # they stated. Uncertainty widens, and widening costs a confirmation — the same asymmetry
    # as domain/amend.py and policy-ir.md rule 3. Fixture AU0044.
    across_cards = ev.enrichment.merchant_prior_approvals_customer
    if across_cards >= minimum:
        return CheckResult(
            "merchant_permitted",
            Verdict.UNKNOWN,
            "merchant_familiarity_ambiguous",
            (
                *evidence,
                Evidence("merchant_name", ev.merchant_name),
                Evidence("merchant_prior_approvals_customer", str(across_cards)),
            ),
            f"you have bought from {safe_display(ev.merchant_name)} before, but on a "
            f"different card and not on this one",
            # Says why the question exists rather than promising an outcome: the customer
            # knows they have shopped there, so the message has to name the thing they do
            # not know — that the rule was read against this card. customer-message.md §4.
            "Your instruction names shops you have used, and this card has not been there.",
        )

    return CheckResult(
        "merchant_permitted",
        Verdict.VIOLATION,
        "merchant_not_permitted",
        (*evidence, Evidence("merchant_name", ev.merchant_name)),
        f"you have not bought from {safe_display(ev.merchant_name)} before",
    )


def _without_history(
    ev: EnrichedEvent, minimum: int, seen: int, evidence: tuple[Evidence, ...]
) -> CheckResult:
    """A card with no history at all: only this run's approvals can establish anything.

    Silence in the history file is not "never bought there". The live API's cardholders have
    no row in it, and reading it that way declined every lens the customer asked for in
    SCEN0122. A shop this run has not approved yet is therefore `unknown`, never a violation,
    even after other shops were approved: a run shows some of the shops a person uses, never
    all of them. specs/check-merchant-permitted.md §"No history at all".
    """
    evidence = (*evidence, Evidence("familiarity_basis", "run"))
    name = safe_display(ev.merchant_name)
    if seen >= minimum:
        return CheckResult(
            "merchant_permitted",
            Verdict.PASS,
            "merchant_familiar",
            evidence,
            f"{name} is a shop you have already bought from in this session "
            f"({seen} approved purchase{'' if seen == 1 else 's'})",
        )
    return CheckResult(
        "merchant_permitted",
        Verdict.UNKNOWN,
        "merchant_history_unavailable",
        (*evidence, Evidence("merchant_name", ev.merchant_name)),
        f"this card has no purchase history yet, so whether you have used {name} before "
        "cannot be established",
        # Why the question exists, not a promise: the customer knows where they shop, and the
        # one thing they cannot know is that we have no record of it. customer-message.md §4.
        "Your instruction names shops you have used, and there is no record yet of where "
        "this card has shopped.",
    )


def check_merchant_lookalike(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """A seller whose name closely resembles one the card actually uses.

    ME0059 'PixelHarbour' against ME0022 'PixelHarbor' — one letter apart, different
    merchant_id, zero history at the impostor. Matching merchants by name treats it as
    familiar and waves it through, which is why the join is always on merchant_id.

    A concern rather than a violation: the customer never wrote "avoid lookalikes", so this is
    an inferred risk signal. At weight 2.0 it escalates on its own, which asks the customer the
    useful question instead of flatly refusing a shop they may have chosen deliberately.

    Never gated on a facet — impersonation is worth flagging whatever the instruction said.
    """
    impersonated = ev.enrichment.merchant_lookalike_of
    if impersonated is None:
        # Nothing to report: pass with no reason code, so an approval's codes stay meaningful.
        return CheckResult("merchant_lookalike", Verdict.PASS)

    seller = safe_display(ev.merchant_name)
    resembles = safe_display(ev.enrichment.merchant_lookalike_name or impersonated)
    return CheckResult(
        "merchant_lookalike",
        Verdict.CONCERN,
        "merchant_lookalike",
        (
            Evidence("merchant_id", ev.merchant_id),
            Evidence("merchant_name", ev.merchant_name),
            Evidence("resembles_merchant_id", impersonated),
            Evidence("resembles_merchant_name", ev.enrichment.merchant_lookalike_name or ""),
            Evidence("merchant_prior_approvals", str(ev.enrichment.merchant_prior_approvals)),
        ),
        # Only the resemblance: on a decline the cause has already said this seller is
        # unfamiliar, and repeating it wastes the one sentence that carries the finding.
        f'the seller "{seller}" has a name that closely resembles "{resembles}", '
        f"a shop you have used",
    )


def check_merchant_type(ev: EnrichedEvent, policy: dict[str, Any]) -> CheckResult:
    """ "Buy only from a specialist sports retailer."

    A requirement about the *kind* of shop, independent of whether the customer has used it
    before. Keeping the two apart is what lets AU0023 (Summit Thread — unfamiliar, but a
    sporting-goods retailer) approve while AU0022 (GreenLoop — sustainable_goods) declines.

    Matches on `merchant_category`, not `merchant_mcc`: in this pack MCC is coarser, and
    `sustainable_goods` shares 5399 with ordinary household merchants. The MCC still travels
    as evidence, because a card-issuing reader knows what 5941 means without translating our
    vocabulary.
    """
    required = facet(policy, "merchant_type")
    if required is None:
        return CheckResult("merchant_type", Verdict.NOT_APPLICABLE)

    allowed = [str(c) for c in requirement(required, "merchant_category_in", []) or []]
    evidence = (
        Evidence("merchant_category", ev.merchant_category),
        Evidence("merchant_mcc", ev.merchant_mcc),
        Evidence("merchant_category_allowed", ", ".join(allowed) if allowed else "(unspecified)"),
    )

    # An unresolved requirement must never silently approve.
    if not allowed:
        return CheckResult(
            "merchant_type",
            Verdict.UNKNOWN,
            "merchant_type_unknown",
            evidence,
            "the kind of shop your instruction allows could not be established",
        )
    if not ev.merchant_category:
        return CheckResult(
            "merchant_type",
            Verdict.UNKNOWN,
            "merchant_type_unknown",
            (Evidence("merchant_id", ev.merchant_id), *evidence),
            f"the kind of shop {safe_display(ev.merchant_name)} is could not be established",
        )

    if ev.merchant_category in allowed:
        return CheckResult(
            "merchant_type",
            Verdict.PASS,
            "merchant_meets_requirement",
            evidence,
            f"{safe_display(ev.merchant_name)} is the kind of shop you asked for "
            f"({_readable(ev.merchant_category)})",
        )

    return CheckResult(
        "merchant_type",
        Verdict.VIOLATION,
        "merchant_type_not_permitted",
        evidence,
        f"{safe_display(ev.merchant_name)} is a {_readable(ev.merchant_category)} shop, "
        f"not {_readable_list(allowed)}",
        f"{_readable_list(allowed).capitalize()} would meet your instruction.",
    )


def _readable(category: str) -> str:
    return category.replace("_", " ")


def _readable_list(categories: list[str]) -> str:
    names = [_readable(c) for c in categories]
    if len(names) == 1:
        return f"a {names[0]} shop"
    return "a " + " or ".join([", ".join(names[:-1]), names[-1]]) + " shop"
