"""The deadline guard — specs/deadline-guard.md.

The platform assigns the 8-second deadline when it *queues* an attempt, not when it hands it
to us, so what actually remains after the long-poll is unknown until we look. This is the one
place where the real clock affects an outcome; everything else in the engine uses the
simulated ``authorization.timestamp``.

Pure, like the rest of ``domain/``: the caller supplies ``now``. Nothing here reads a clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .sanitize import safe_display
from .types import Decision, DecisionRecord, EnrichedEvent, Evidence, resolve_uncertainty

# Enough for one HTTPS submit on venue Wi-Fi plus the engine's own p99 (< 5 ms across all 45
# fixtures) plus margin. Almost entirely a network allowance — see specs/deadline-guard.md.
RESERVE_MS = 1500.0


def parse_deadline(event: dict[str, Any]) -> datetime | None:
    """``data.deadline_at`` as an aware UTC instant, or None when absent or unparseable.

    A malformed deadline disables the guard rather than breaking the decision: this is a
    safety net, and a safety net that throws is worse than no net.
    """
    raw = event.get("deadline_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def budget_ms(deadline_at: datetime | None, now: datetime) -> float | None:
    """Milliseconds left before the platform stops accepting a decision. None ⇒ no deadline."""
    if deadline_at is None:
        return None
    return (deadline_at - now).total_seconds() * 1000.0


def at_risk(budget: float | None, *, reserve_ms: float = RESERVE_MS) -> bool:
    """True when there is not enough budget left to evaluate *and* submit.

    Boundary is ``>=``: a budget exactly equal to the reserve is spent on the evaluation we
    were asked for, because the reserve is sized to cover the submit that follows it.
    """
    return budget is not None and budget < reserve_ms


def guard_record(
    ev: EnrichedEvent,
    uncertainty_policy: str,
    *,
    budget: float | None,
    engine_version: str,
    decided_at: datetime,
    inputs_digest: str,
) -> DecisionRecord:
    """The decision we emit instead of missing the window.

    Carries no check results — none ran, and a record that implied otherwise would tell a
    judge the evidence set was examined when it was not.
    """
    # Shares evaluator.combine()'s resolver, which floors `deadline_risk` at step_up: a
    # spent deadline means no check ran, and "approve when unsure" was never consent to pay
    # for a purchase nobody looked at. specs/deadline-guard.md, Outcome mapping.
    decision = resolve_uncertainty(uncertainty_policy, ("deadline_risk",))
    where = f"CHF {ev.billing_amount_chf} at {safe_display(ev.merchant_name)}"
    # No APPROVE branch: the resolver cannot return one here.
    message = {
        Decision.STEP_UP: (
            f"Needs your confirmation: {where}. There was not enough time to complete every "
            "check before the payment network's deadline. Approve or decline in the app."
        ),
        Decision.DECLINE: (
            f"Declined: {where}. There was not enough time to complete every check before the "
            "payment network's deadline, and you asked us to decline when we are unsure."
        ),
    }[decision]
    return DecisionRecord(
        authorization_id=ev.authorization_id,
        decision=decision,
        reason_codes=("deadline_risk",),
        customer_message=message,
        evidence=(
            Evidence("deadline_budget_ms", f"{budget:.0f}" if budget is not None else "unknown"),
            Evidence("billing_amount_chf", str(ev.billing_amount_chf)),
        ),
        check_results=(),
        inputs_digest=inputs_digest,
        engine_version=engine_version,
        latency_ms=0.0,
        decided_at=decided_at,
    )


__all__ = [
    "RESERVE_MS",
    "at_risk",
    "budget_ms",
    "guard_record",
    "parse_deadline",
]
