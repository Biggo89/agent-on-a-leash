"""Core value types shared by every check. Pure data — no I/O, no framework."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .money import Money
from .sanitize import ItemFacts, Manipulation


class Verdict(StrEnum):
    PASS = "pass"
    CONCERN = "concern"
    VIOLATION = "violation"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class Decision(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"
    STEP_UP = "step_up"


#: What an `unknown` becomes under each `uncertainty_policy`. One table, read by
#: `evaluator.combine` and by the deadline guard, because they are answering the same question.
UNCERTAINTY_OUTCOME: dict[str, Decision] = {
    "ask": Decision.STEP_UP,
    "decline": Decision.DECLINE,
    "approve": Decision.APPROVE,
}

#: Reason codes that mean **we did not evaluate**, as opposed to *we evaluated and a fact could
#: not be established*. The difference is the whole of `resolve_uncertainty` below.
NOT_EVALUATED: frozenset[str] = frozenset({"engine_error_defaulted", "deadline_risk"})


def resolve_uncertainty(uncertainty_policy: str, reason_codes: Sequence[str] = ()) -> Decision:
    """An `unknown` outcome under the customer's policy — floored when nothing was evaluated.

    `uncertainty_policy: approve` is the customer saying *"resolve a doubt about the purchase
    in the agent's favour."* It is **not** consent to pay for a purchase nobody looked at.

    A missing return window is a doubt: the check ran, read the seller's text, and found no
    figure. A crashed check or a spent deadline is not a doubt about anything — it is the
    absence of an opinion, and reporting it as an approval tells the customer their rules were
    applied when they were not. So those two codes floor the outcome at `step_up`: still the
    customer's decision, which is what they always had, rather than ours by default.

    `decline` is left alone. A customer who asked for refusals when unsure is not made safer
    by being asked instead, and overriding the stricter choice would be the same error in the
    other direction.
    """
    outcome = UNCERTAINTY_OUTCOME.get(uncertainty_policy, Decision.STEP_UP)
    if outcome is Decision.APPROVE and any(code in NOT_EVALUATED for code in reason_codes):
        return Decision.STEP_UP
    return outcome


@dataclass(frozen=True, slots=True)
class Evidence:
    field: str
    value: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "value": self.value}


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's opinion. A check never decides the outcome — the evaluator combines them."""

    check_id: str
    verdict: Verdict
    reason_code: str | None = None
    evidence: tuple[Evidence, ...] = ()
    detail: str = ""
    #: One closing sentence for the customer, rendered after `detail` when this result is
    #: among the cited reason codes. For a violation it is the **remedy** — what would have
    #: satisfied the rule that was broken, never a promise of approval, because the other
    #: checks also ran. For a concern it is the **reassurance** — what the finding does not
    #: mean. Empty when the check has nothing useful to add. specs/customer-message.md §4.
    follow_up: str = ""


@dataclass(frozen=True, slots=True)
class Enrichment:
    """Signals computed before checks run. See specs/decision-rules.md §4."""

    merchant_prior_approvals: int = 0
    #: The same count across **every card the cardholder holds**. "Shops I have used before"
    #: is about the person, and a card-only count reads a two-card customer as a stranger to
    #: their own shop (specs/check-merchant-permitted.md §"Card or person").
    merchant_prior_approvals_customer: int = 0
    device_prior_approvals: int = 0
    #: Where the three counts above come from. ``"history"`` is the pack's history file, the
    #: normal case. ``"run"`` is this run's own approvals, for a card with **no approved history
    #: row at all**. The live API's cardholders are all such cards, and reading their empty
    #: file as "never bought there, never seen this phone" declined or stepped up every order
    #: (specs/check-merchant-permitted.md §"No history at all").
    familiarity_basis: str = "history"
    #: Orders approved earlier in this run, by the engine or by the customer on a step-up. Read
    #: only on the ``"run"`` basis, where zero means the run has established nothing yet.
    run_approvals: int = 0
    merchant_lookalike_of: str | None = None
    merchant_lookalike_name: str | None = None
    #: One approved-spend figure per distinct window the policy names (§2.1, §9). A mandate
    #: may carry several period rules and every one is enforced, so this is a map rather than
    #: a number — `CHF 300 / 7 days` and `CHF 1000 / 30 days` are two constraints over the
    #: same ledger and a purchase must satisfy both.
    approved_spend_windows: Mapping[int, Money] = field(default_factory=dict)
    is_duplicate_of: str | None = None
    is_requote_of: str | None = None
    #: Committed orders at **this merchant** inside the split window, as
    #: ``(authorization_id, billing_amount_chf)`` (specs/check-split-order.md). Empty means
    #: none, never "unknown" — the run's own memory is always available, so `check_split_order`
    #: has no uncertainty branch and never escalates on missing data.
    same_merchant_recent: tuple[tuple[str, Money], ...] = ()
    #: The carts of orders **approved** earlier in this run, as ``(authorization_id, items)``
    #: (specs/check-goal-fulfilled.md). Approved only — a stepped-up order awaiting the
    #: customer has bought nothing yet, so it cannot have fulfilled anything.
    approved_carts: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    night_hours: bool = False
    item_facts: tuple[ItemFacts, ...] = ()
    manipulations: tuple[Manipulation, ...] = ()

    def spend_in(self, period_days: int) -> Money | None:
        """Approved spend inside one window, or **None** when it was not computed.

        None is the whole point: a missing figure that reads as "nothing spent yet" approves
        everything, so `check_period_limit` turns it into `unknown` rather than zero
        (specs/decision-rules.md §9).
        """
        return self.approved_spend_windows.get(period_days)

    def primary_window(self) -> tuple[int, Money] | None:
        """The shortest window and its figure — for display and the audit trail only.

        Never for a decision: the check reads the window belonging to the rule it is
        evaluating. The shortest is simply the one a single-window mandate has, which keeps
        the audit record's long-standing `approved_spend_window_chf` key meaning what it
        always meant.
        """
        if not self.approved_spend_windows:
            return None
        days = min(self.approved_spend_windows)
        return days, self.approved_spend_windows[days]


@dataclass(frozen=True, slots=True)
class EnrichedEvent:
    """A parsed authorization.request plus computed signals. The input to every check."""

    raw: dict[str, Any]
    authorization_id: str
    timestamp: datetime
    billing_amount_chf: Money
    amount: Money
    currency: str
    items_subtotal: Money
    delivery_fee: Money
    merchant_id: str
    merchant_name: str
    merchant_category: str
    merchant_mcc: str
    merchant_country: str
    customer_device_id: str
    recent_attempt_count_10m: int
    order_returnable: str
    order_cancellable: str
    fulfillment_method: str
    related_authorization_id: str | None
    related_authorization_status: str | None
    items: tuple[dict[str, Any], ...]
    enrichment: Enrichment

    @property
    def item_categories(self) -> set[str]:
        return {str(i["item_category"]) for i in self.items}


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """The audit record. Everything needed to defend a decision — and nothing more."""

    authorization_id: str
    decision: Decision
    reason_codes: tuple[str, ...]
    customer_message: str
    evidence: tuple[Evidence, ...]
    check_results: tuple[CheckResult, ...]
    inputs_digest: str
    engine_version: str
    latency_ms: float
    decided_at: datetime
    #: The escalation threshold this decision was combined against. A customer may set their
    #: own (specs/customer-settings.md §7), so reporting the global default would show a
    #: score against a bar that was never applied.
    step_up_threshold: float = 2.0

    def to_payload(self) -> dict[str, Any]:
        """The body for POST /v1/authorizations/{id}/decision."""
        return {
            "authorization_id": self.authorization_id,
            "decision": str(self.decision),
            "reason_codes": list(self.reason_codes),
            "customer_message": self.customer_message,
            "evidence": [e.as_dict() for e in self.evidence],
            "engine_version": self.engine_version,
        }
