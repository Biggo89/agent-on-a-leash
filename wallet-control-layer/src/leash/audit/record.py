"""DecisionRecord -> the audit JSON shape. See specs/audit-record.md.

Everything a judge needs to say what we permitted, what evidence we used, and why — and
nothing more. The raw event is deliberately not stored: it carries untrusted merchant text,
and ``inputs_digest`` already proves what we saw.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain.evaluator import CONCERN_WEIGHTS
from ..domain.score import score as trust_score
from ..domain.types import CheckResult, DecisionRecord, EnrichedEvent, Verdict

SCHEMA = "leash.audit.v1"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _check(result: CheckResult) -> dict[str, Any]:
    # The weight is written next to the check that earned it, so the record shows the
    # arithmetic behind concern_score rather than only its total.
    weight = (
        CONCERN_WEIGHTS.get(result.reason_code or "") if result.verdict is Verdict.CONCERN else None
    )
    return {
        "check_id": result.check_id,
        "verdict": str(result.verdict),
        "reason_code": result.reason_code,
        "detail": result.detail,
        "evidence": [e.as_dict() for e in result.evidence],
        "concern_weight": weight,
    }


def concern_score(results: tuple[CheckResult, ...]) -> float:
    return sum(
        CONCERN_WEIGHTS.get(r.reason_code or "", 0.0)
        for r in results
        if r.verdict is Verdict.CONCERN
    )


def decision_summary(record: DecisionRecord, ev: EnrichedEvent | None = None) -> dict[str, Any]:
    """The decision object the service returns and the UI renders. Contract §4."""
    # Defensive: a serializer on the audit path must never be the thing that raises.
    raw = ev.raw.get("authorization", {}) if ev is not None else {}
    return {
        "authorization_id": record.authorization_id,
        "source_authorization_id": raw.get("source_authorization_id"),
        # What the decision was *about*. A verdict with no subject cannot be rendered, and a
        # consumer reading /v1/decisions could not previously tell what was bought or from
        # whom. Merchant-supplied strings travel verbatim and are escaped by whoever
        # displays them, exactly as they are in the audit record itself.
        "authorization": _subject(ev),
        "decision": str(record.decision),
        "reason_codes": list(record.reason_codes),
        "customer_message": record.customer_message,
        "evidence": [e.as_dict() for e in record.evidence],
        "checks": [_check(r) for r in record.check_results],
        "score": {
            "concern_score": concern_score(record.check_results),
            # The same evidence as one number, for the glance before the sentence is read.
            # Derived, never a second opinion: every deduction points at a check above.
            # specs/trust-score.md.
            **trust_score(record),
            # The bar this decision was actually combined against, not the global default:
            # a customer may set their own (specs/customer-settings.md §7), and a score
            # shown against a bar nobody applied is a misleading audit line.
            "step_up_threshold": record.step_up_threshold,
        },
        "timing": {"latency_ms": round(record.latency_ms, 3)},
        "engine_version": record.engine_version,
        "decided_at": _iso(record.decided_at),
    }


def _subject(ev: EnrichedEvent | None) -> dict[str, Any]:
    """The facts a reader needs to know which purchase this verdict belongs to."""
    if ev is None:
        return {}
    return {
        "timestamp": _iso(ev.timestamp),
        "amount": str(ev.amount),
        "currency": ev.currency,
        "billing_amount_chf": str(ev.billing_amount_chf),
        "merchant_id": ev.merchant_id,
        "merchant_name": ev.merchant_name,
        "merchant_category": ev.merchant_category,
        "merchant_country": ev.merchant_country,
        "items": [
            {
                "name": i.get("item_name") or i.get("name"),
                "category": i.get("item_category"),
                "quantity": i.get("quantity"),
                "unit_price": i.get("unit_price"),
                "currency": i.get("currency"),
                "details": i.get("item_details"),
            }
            for i in ev.items
        ],
    }


def decision_context(ev: EnrichedEvent) -> dict[str, Any]:
    """What a UI shows beside a live decision: the facts the engine *derived*, not just the
    ones it was sent — prior orders at this shop, a lookalike, a re-quote, hostile spans.

    The leash demo's replayed decisions carry these from ``playground/tools/generate_data.py``;
    a live one has only what this returns, so the two keep the same shape. Served on
    ``/v1/decisions`` and never written to the trail: the audit line keeps its own, narrower
    enrichment, which deliberately does not store the hostile excerpts a second time.
    """
    enrichment = ev.enrichment
    primary = enrichment.primary_window()
    authorization = ev.raw.get("authorization", {})
    return {
        "purchase_description": authorization.get("purchase_description"),
        "device_id": ev.customer_device_id,
        "window_before_chf": str(primary[1]) if primary else "0.00",
        "enrichment": {
            "merchant_prior_approvals": enrichment.merchant_prior_approvals,
            "merchant_prior_approvals_customer": enrichment.merchant_prior_approvals_customer,
            "device_prior_approvals": enrichment.device_prior_approvals,
            # "run" when the card has no history and the counts are this run's approvals
            # (specs/check-merchant-permitted.md §"No history at all").
            "familiarity_basis": enrichment.familiarity_basis,
            "run_approvals": enrichment.run_approvals,
            "merchant_lookalike_of": enrichment.merchant_lookalike_of,
            "merchant_lookalike_name": enrichment.merchant_lookalike_name,
            "is_duplicate_of": enrichment.is_duplicate_of,
            "is_requote_of": enrichment.is_requote_of,
            "same_merchant_recent": [
                {"id": auth_id, "chf": str(amount)}
                for auth_id, amount in enrichment.same_merchant_recent
            ],
            "night_hours": enrichment.night_hours,
            "manipulations": [
                {"label": m.label, "field": m.source_field, "excerpt": m.excerpt}
                for m in enrichment.manipulations
            ],
        },
    }


def decision_record(
    record: DecisionRecord,
    ev: EnrichedEvent,
    policy: dict[str, Any],
    *,
    run_id: str | None = None,
    scenario_id: str | None = None,
    deadline_at: datetime | None = None,
    budget_ms: float | None = None,
    guard_tripped: bool = False,
    upstream: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The persisted ``decision`` line."""
    summary = decision_summary(record, ev)
    enrichment = ev.enrichment
    return {
        "schema": SCHEMA,
        "type": "decision",
        **summary,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "policy": {
            "uncertainty_policy": policy.get("uncertainty_policy", "ask"),
            "hard_rules": policy.get("hard_rules", []),
            "intent_facets": policy.get("intent_facets", []),
        },
        "enrichment": {
            "merchant_id": ev.merchant_id,
            "merchant_prior_approvals": enrichment.merchant_prior_approvals,
            "device_prior_approvals": enrichment.device_prior_approvals,
            "familiarity_basis": enrichment.familiarity_basis,
            "merchant_lookalike_of": enrichment.merchant_lookalike_of,
            # The shortest window keeps the long-standing key: it is the one a
            # single-window mandate has, so the meaning of an existing trail is unchanged.
            # Every window the policy named travels beside it.
            "approved_spend_window_chf": str(primary[1])
            if (primary := enrichment.primary_window())
            else "0.00",
            "approved_spend_windows": {
                str(days): str(spend)
                for days, spend in sorted(enrichment.approved_spend_windows.items())
            },
            "is_duplicate_of": enrichment.is_duplicate_of,
            "is_requote_of": enrichment.is_requote_of,
            "night_hours": enrichment.night_hours,
            # Label and field only. The sanitized excerpt is already on the manipulation
            # check's own evidence; repeating the hostile text here would store it twice.
            "manipulations": [
                {"label": m.label, "source_field": m.source_field} for m in enrichment.manipulations
            ],
            "billing_amount_chf": str(ev.billing_amount_chf),
        },
        "timing": {
            "latency_ms": round(record.latency_ms, 3),
            "deadline_at": _iso(deadline_at) if deadline_at else None,
            "budget_ms": round(budget_ms, 1) if budget_ms is not None else None,
            "guard_tripped": guard_tripped,
        },
        "inputs_digest": record.inputs_digest,
        "upstream": upstream
        or {"submitted": False, "http_status": None, "platform_status": None, "error": None},
    }


def resolution_record(
    authorization_id: str,
    outcome: str,
    *,
    resolved_at: datetime,
    customer_message: str = "",
    resolved_by: str = "customer",
    upstream: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A human answering a step-up. Appended separately — nothing is rewritten."""
    return {
        "schema": SCHEMA,
        "type": "resolution",
        "authorization_id": authorization_id,
        "resolved_at": _iso(resolved_at),
        "outcome": outcome,
        "customer_message": customer_message,
        "resolved_by": resolved_by,
        "upstream": upstream or {"submitted": False, "http_status": None, "error": None},
    }


def replay_record(
    authorization_id: str,
    decision: str,
    reason_codes: list[str],
    *,
    observed_at: datetime,
    upstream: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """At-least-once delivery of something already decided. Never a re-evaluation."""
    return {
        "schema": SCHEMA,
        "type": "replay",
        "authorization_id": authorization_id,
        "observed_at": _iso(observed_at),
        "decision": decision,
        "reason_codes": reason_codes,
        "upstream": upstream or {"submitted": False, "http_status": None, "error": None},
    }


def unowned_record(
    authorization_id: str,
    decision: str,
    *,
    run_id: str,
    observed_at: datetime,
    upstream: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An authorization for a run this process does not know, answered without judging it.

    The decision queue is team-global, so another engine on the same team key can hand us its
    work. We answer so the platform does not decline it unexplained, but the policy — the
    intent facets in particular — is not ours to apply, so the outcome is that run's own
    uncertainty policy. Recorded as its own type: it is not a decision this engine made about
    a purchase it understood.
    """
    return {
        "schema": SCHEMA,
        "type": "unowned",
        "authorization_id": authorization_id,
        "delivered_for_run_id": run_id,
        "observed_at": _iso(observed_at),
        "decision": decision,
        "reason_codes": ["insufficient_evidence"],
        "upstream": upstream or {"submitted": False, "http_status": None, "error": None},
    }
