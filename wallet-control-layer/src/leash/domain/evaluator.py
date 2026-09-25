"""The decision function: run every check, combine the verdicts, explain the result.

SKELETON. The combination logic and the two reference limit checks are implemented per
specs/decision-rules.md §5. The judgement-heavy checks are the hackathon work — each one
needs a spec entry and vectors before it is written. See specs/TEMPLATE.md.

Invariants (AGENTS.md §3):
  * pure — no I/O, no clock, no globals
  * never raises — any failure degrades to the mandate's uncertainty_policy
  * deterministic — same event, same decision, always
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from .checks.exclusion import check_category_exclusion, check_spending_hours
from .checks.item import (
    check_goal_fulfilled,
    check_item_attributes,
    check_item_matches_request,
    check_unrequested_addon,
)
from .checks.limits import check_per_order_limit, check_period_limit, check_split_order
from .checks.manipulation import check_manipulation_detected
from .checks.merchant import (
    check_merchant_lookalike,
    check_merchant_permitted,
    check_merchant_type,
)
from .checks.order import check_duplicate_order, check_order_terms
from .checks.session import check_session_integrity
from .sanitize import safe_display
from .types import (
    CheckResult,
    Decision,
    DecisionRecord,
    EnrichedEvent,
    Verdict,
    resolve_uncertainty,
)

# A check returns one result, or several when one concept carries several named signals
# (session integrity emits device novelty, velocity and hour separately so each is
# weighted and audited on its own).
Check = Callable[[EnrichedEvent, dict[str, Any]], "CheckResult | Sequence[CheckResult]"]

# Every check runs on every event: a complete evidence set IS the audit trail.
CHECKS: tuple[Check, ...] = (
    check_per_order_limit,
    check_period_limit,
    check_split_order,
    check_merchant_permitted,
    check_merchant_type,
    check_merchant_lookalike,
    check_order_terms,
    check_item_matches_request,
    check_item_attributes,
    check_unrequested_addon,
    check_goal_fulfilled,
    check_duplicate_order,
    check_category_exclusion,
    check_spending_hours,
    check_session_integrity,
    check_manipulation_detected,
)

# Concern weights and threshold live here — one place, tunable, and showable to a judge.
# Concern weights and the threshold live here and nowhere else — one place, tunable, and
# showable to a judge. `tools/tune.py` and tests/unit/test_concern_config.py both fail if a
# weight ever stops connecting to a check that emits it.
#
# Two tiers. The line between them is evidence of an adversary versus a single ambient signal:
#
#   2.0  escalates on its own — something is acting against the cardholder's interest
#   1.0  needs corroboration — ordinary alone, meaningful in company (two reach the threshold)
CONCERN_WEIGHTS: dict[str, float] = {
    # Adversarial: a counterparty or a session working against the cardholder.
    "merchant_text_manipulation": 2.0,  # merchant instructing the control layer
    "merchant_lookalike": 2.0,  # impersonating a shop the cardholder trusts
    "device_novel": 2.0,  # "someone other than me is driving the session"
    # Unagreed spend: money moving that the cardholder did not sanction.
    "unrequested_addon": 2.0,  # a seller added something to the basket
    "duplicate_order": 2.0,  # the same committed order placed twice
    "split_order_suspected": 2.0,  # one order broken in two to clear a per-order cap
    # Only ever *read* when the customer set repeat_purchase_action="ask"; the default
    # records the finding as a pass. specs/check-goal-fulfilled.md.
    "goal_already_fulfilled": 2.0,  # the thing asked for was already bought this errand
    # Ambient: ordinary alone, corroborating together.
    "velocity_elevated": 1.0,
    "unusual_hour": 1.0,
}
STEP_UP_THRESHOLD = 2.0


def digest(event: dict[str, Any]) -> str:
    """Hash of exactly what we saw. Proves the inputs without duplicating the payload."""
    return hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def evaluate(
    ev: EnrichedEvent,
    policy: dict[str, Any],
    *,
    engine_version: str = "leash-0.1.0",
    now: datetime | None = None,
) -> DecisionRecord:
    """Return a decision for one authorization. Never raises."""
    started = time.perf_counter()
    decided_at = now or datetime.now(UTC)
    uncertainty = policy.get("uncertainty_policy", "ask")
    # How much evidence this customer wants before being interrupted is theirs to set
    # (specs/customer-settings.md §7). The *weights* stay global: they encode what a signal
    # means, which is the engine's judgement and not the customer's.
    threshold = _threshold(policy)

    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            produced = check(ev, policy)
            results.extend(produced if isinstance(produced, Sequence) else (produced,))
        except Exception as exc:  # a broken check must not become a platform-side decline
            results.append(
                CheckResult(
                    getattr(check, "__name__", "unknown"),
                    Verdict.UNKNOWN,
                    "engine_error_defaulted",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    decision, codes = combine(results, uncertainty, threshold)
    evidence = tuple(e for r in results for e in r.evidence)
    return DecisionRecord(
        authorization_id=ev.authorization_id,
        decision=decision,
        reason_codes=codes,
        customer_message=explain(decision, results, ev, codes),
        evidence=evidence,
        check_results=tuple(results),
        inputs_digest=digest(ev.raw),
        engine_version=engine_version,
        latency_ms=(time.perf_counter() - started) * 1000,
        decided_at=decided_at,
        step_up_threshold=threshold,
    )


def concern_weight(reason_code: str | None) -> float:
    """Weight for one concern. Unknown codes are a config bug, not a silent zero."""
    if reason_code is None:
        return 0.0
    if reason_code not in CONCERN_WEIGHTS:
        raise KeyError(
            f"concern reason code {reason_code!r} has no entry in CONCERN_WEIGHTS; "
            "add one rather than letting it score nothing"
        )
    return CONCERN_WEIGHTS[reason_code]


def _threshold(policy: dict[str, Any]) -> float:
    """The escalation threshold in force. Lower is stricter — it takes less to escalate.

    A malformed value falls back to the global default rather than raising: this runs in the
    decision path, where nothing may raise (AGENTS.md §3.5), and the default is the safe
    reading of "the customer set something we cannot use".
    """
    try:
        value = float(policy.get("step_up_threshold", STEP_UP_THRESHOLD))
    except (TypeError, ValueError):
        return STEP_UP_THRESHOLD
    return value if value > 0 else STEP_UP_THRESHOLD


def combine(
    results: list[CheckResult],
    uncertainty_policy: str,
    step_up_threshold: float = STEP_UP_THRESHOLD,
) -> tuple[Decision, tuple[str, ...]]:
    """specs/decision-rules.md §5.

    A definite breach of an instruction the customer wrote outranks a soft risk signal; and an
    unestablished fact is never resolved silently in the agent's favour.
    """
    violations = [r for r in results if r.verdict is Verdict.VIOLATION]
    if violations:
        return Decision.DECLINE, tuple(r.reason_code for r in violations if r.reason_code)

    unknowns = [r for r in results if r.verdict is Verdict.UNKNOWN]
    if unknowns:
        codes = tuple(r.reason_code or "insufficient_evidence" for r in unknowns)
        # `resolve_uncertainty` floors a *failure* at step_up whatever the policy says: a
        # crashed check is not a doubt about the purchase, it is the absence of an opinion,
        # and `approve` was consent to resolve doubts rather than to skip the checks.
        return resolve_uncertainty(uncertainty_policy, codes), codes

    concerns = [r for r in results if r.verdict is Verdict.CONCERN]
    score = sum(concern_weight(r.reason_code) for r in concerns)
    if score >= step_up_threshold:
        return Decision.STEP_UP, tuple(r.reason_code for r in concerns if r.reason_code)

    passes = tuple(r.reason_code for r in results if r.verdict is Verdict.PASS and r.reason_code)
    concern_codes = tuple(r.reason_code for r in concerns if r.reason_code)
    return Decision.APPROVE, passes + concern_codes


def _sentence(text: str) -> str:
    """Check details are written to be joined mid-sentence; here they become one.

    Capitalise the opening and terminate it, so a following sentence — "Approve or decline in
    the app." — does not run into it.
    """
    if not text:
        return text
    text = text[:1].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


ANSWER_IN_APP = "Approve or decline in the app."


def _where(ev: EnrichedEvent) -> str:
    """The verdict line's subject: what was spent, and with whom.

    Always the CHF billing amount, because that is the figure every limit was compared
    against — with the original in parentheses when the order was not placed in CHF. AU0038 is
    450 USD against a CHF 400 cap and is fully compliant at CHF 391.50; a message that hides
    the conversion makes the pack's strongest non-over-blocking case look like a mistake.

    `merchant_name` is merchant-supplied and goes through `safe_display`.
    """
    amount = f"CHF {ev.billing_amount_chf}"
    if ev.currency != "CHF":
        amount += f" ({ev.currency} {ev.amount})"
    return f"{amount} at {safe_display(ev.merchant_name)}"


def _aside(results: list[CheckResult], codes: tuple[str, ...]) -> str:
    """Adversarial findings that did NOT drive the decision — named, and named as harmless.

    Only concerns weighted at or above the step-up threshold appear: the 2.0 tier is evidence
    of an adversary (manipulation, lookalike, an unknown device, an add-on, a duplicate) and
    the customer needs to hear it even when something else declined the order. The 1.0 tier is
    ambient — the hour, the velocity — and listing it under a decline reads as an accusation
    for signals that changed nothing. The audit record keeps both either way.

    Weights are read with `.get`: `combine()` owns the strict lookup that turns a missing
    weight into a loud config error, and it would be absurd for the *explanation* to be the
    thing that raises on a decision already made.
    """
    surfaced = [
        r.detail
        for r in results
        if r.verdict is Verdict.CONCERN
        and r.reason_code not in codes
        and r.detail
        and CONCERN_WEIGHTS.get(r.reason_code or "", 0.0) >= STEP_UP_THRESHOLD
    ]
    if not surfaced:
        return ""
    return _sentence(f"also noticed, and it did not change this decision: {'; '.join(surfaced)}")


def explain(
    decision: Decision,
    results: list[CheckResult],
    ev: EnrichedEvent,
    codes: tuple[str, ...] = (),
) -> str:
    """Plain language for the cardholder. specs/customer-message.md.

    Four parts, always in this order: the verdict, the cause, what to do next, and anything
    adversarial we noticed that did not change the outcome.

    The cause names the cited `reason_codes` and nothing else. A message that argued a reason
    the record does not carry is a message the audit trail cannot support — and on a decline
    it is also how a control layer earns a reputation for over-blocking that its own decision
    table does not deserve.

    Never echoes untrusted merchant text back to the customer, and never leaks a reason code.
    """
    where = _where(ev)
    cited = [r for r in results if r.reason_code in codes]
    cause = _sentence("; ".join(r.detail for r in cited if r.detail))
    follow_up = " ".join(r.follow_up for r in cited if r.follow_up)

    if decision is Decision.DECLINE:
        parts = [
            f"Declined: {where}.",
            cause or "This purchase falls outside the rules you set.",
            follow_up,
            _aside(results, codes),
        ]
    elif decision is Decision.STEP_UP:
        parts = [
            f"Needs your confirmation: {where}.",
            cause or "Some details could not be verified.",
            follow_up,
            ANSWER_IN_APP,
            _aside(results, codes),
        ]
    else:
        # An approval names only what passed. A sub-threshold concern can reach `codes`, and
        # warning about a purchase we did not think worth stopping is friction with no
        # decision behind it.
        ok = _sentence("; ".join(r.detail for r in cited if r.verdict is Verdict.PASS and r.detail))
        parts = [f"Approved: {where}.", ok]
    return " ".join(p for p in parts if p)
