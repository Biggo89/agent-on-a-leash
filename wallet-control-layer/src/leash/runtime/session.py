"""State for one scenario run: the policy it is enforcing and everything decided so far.

The platform leaves period tracking to us (``spend_in_period_before_chf`` is null on every
fixture), so the ledger lives here and is the single place a run's approved spend exists.

Mutated by the run loop thread and read by the service's request threads, so every access
goes through ``lock``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..domain.ledger import Ledger
from ..domain.money import Money
from ..domain.policy import period_caps
from ..domain.types import EnrichedEvent


@dataclass(slots=True)
class StepUp:
    """A question waiting on a human. Not an outcome — the resolution is the outcome."""

    authorization_id: str
    source_authorization_id: str
    run_id: str
    asked_at: datetime
    expires_at: datetime | None
    amount_chf: str
    merchant_name: str
    customer_message: str
    reason_codes: list[str]
    evidence: list[dict[str, str]]
    resolved: str | None = None

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        remaining = max(0.0, (self.expires_at - now).total_seconds()) if self.expires_at else None
        return {
            "authorization_id": self.authorization_id,
            "source_authorization_id": self.source_authorization_id,
            "run_id": self.run_id,
            "asked_at": _iso(self.asked_at),
            "expires_at": _iso(self.expires_at) if self.expires_at else None,
            "seconds_remaining": round(remaining, 1) if remaining is not None else None,
            "expired": remaining == 0.0 if remaining is not None else False,
            "amount_chf": self.amount_chf,
            "merchant_name": self.merchant_name,
            "customer_message": self.customer_message,
            "reason_codes": list(self.reason_codes),
            "evidence": list(self.evidence),
            "resolved": self.resolved,
        }


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class RunSession:
    run_id: str
    scenario_id: str
    mandate_id: str
    policy: dict[str, Any]  # hard_rules + uncertainty_policy + intent_facets
    period_windows: tuple[int, ...]
    total: int
    ir: dict[str, Any] = field(default_factory=dict)
    auto_resolve: str | None = None
    # The card the platform named at run start. Kept so a mid-run recompose (a narrowing
    # applied from the phone) rebuilds the same account layer instead of dropping it.
    card_id: str | None = None

    ledger: Ledger = field(default_factory=Ledger)
    seen: list[EnrichedEvent] = field(default_factory=list)
    decided: dict[str, dict[str, Any]] = field(default_factory=dict)  # auth_id -> submitted body
    decisions: list[dict[str, Any]] = field(default_factory=list)  # summaries, oldest first
    step_ups: dict[str, StepUp] = field(default_factory=dict)
    in_flight: set[str] = field(default_factory=set)  # claimed, not yet decided

    status: str = "running"  # running | complete | stopped | failed
    error: str | None = None
    delivered: int = 0
    replays: int = 0
    cursor: int = 0  # /v1/events cursor, for resume
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Reentrant: as_dict() holds it while calling counters() and window(), which take it
    # themselves so the service can also call them directly.
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    stop_requested: threading.Event = field(default_factory=threading.Event, repr=False)

    # ------------------------------------------------------------------ views

    def counters(self) -> dict[str, Any]:
        """What the engine decided, and — separately — how it ended up.

        The two are not the same thing once a human answers, and the cardholder's own screen
        needs the second. `approve`/`decline`/`step_up` count the **engine's** verdicts and
        never move, which is what an audit reader wants. `final` counts **outcomes**, where
        the three states are mutually exclusive: a step-up somebody answered is no longer
        waiting on them. Without it the phone kept reporting "2 needs you" after both had
        been answered — found rehearsing live mode on 2026-09-21.
        """
        counts = {"approve": 0, "decline": 0, "step_up": 0}
        with self.lock:
            for summary in list(self.decisions):
                counts[str(summary["decision"])] += 1
            resolutions = [s.resolved for s in self.step_ups.values()]
        waiting = sum(1 for r in resolutions if r is None)
        return {
            "total": self.total,
            "delivered": self.delivered,
            "decided": len(self.decided),
            "replays": self.replays,
            **counts,
            "final": {
                "approved": counts["approve"] + sum(1 for r in resolutions if r == "approve"),
                # An expired step-up is one the platform declined when nobody answered.
                "declined": counts["decline"]
                + sum(1 for r in resolutions if r in ("decline", "expired")),
                "waiting": waiting,
            },
        }

    def window(self, now: datetime | None = None) -> dict[str, Any]:
        """The rolling-window figure the platform does not compute for us."""
        with self.lock:
            pending = Money.zero()
            for entry in list(self.ledger.pending.values()):
                pending = pending + entry.amount
            latest = self.seen[-1].timestamp if self.seen else None
            rules = list(self.policy.get("hard_rules", []))
        # The cap each window is measured against — the tightest per window, from the composed
        # policy, so a mid-run tightening moves it. Without it a reader has "CHF 223.50 used"
        # and nothing to hold it against; the leash demo's ring vanished in live mode.
        limits = {
            days: str(Money.from_value(rule["value"]))
            for days, rule in period_caps(rules)
            if days is not None
        }
        # One entry per window the mandate states (specs/decision-rules.md §9). The
        # shortest stays on the top-level keys, so a single-window mandate — every one in
        # the pack — reports exactly what it always did and the UI's ring is unchanged.
        windows = [
            {
                "period_days": days,
                "approved_spend_chf": str(self.ledger.spend_in_window(latest, days)),
                "approvals_in_window": len(self.ledger.entries_in_window(latest, days)),
                "limit_chf": limits.get(days),
            }
            for days in self.period_windows
            if latest
        ]
        primary = windows[0] if windows else None
        return {
            "period_days": primary["period_days"]
            if primary
            else (self.period_windows[0] if self.period_windows else None),
            "approved_spend_chf": primary["approved_spend_chf"] if primary else None,
            "approvals_in_window": primary["approvals_in_window"] if primary else None,
            "limit_chf": limits.get(self.period_windows[0]) if self.period_windows else None,
            "windows": windows,
            "pending_step_up_chf": str(pending),
            "as_of": _iso(latest) if latest else None,
        }

    def as_dict(self, *, include_decisions: bool = True) -> dict[str, Any]:
        with self.lock:
            body: dict[str, Any] = {
                "run_id": self.run_id,
                "scenario_id": self.scenario_id,
                "mandate_id": self.mandate_id,
                "status": self.status,
                "started_at": _iso(self.started_at),
                "counters": self.counters(),
                "window": self.window(),
                "step_ups": [s.as_dict() for s in self.step_ups.values() if s.resolved is None],
                "error": self.error,
            }
            if include_decisions:
                body["decisions"] = list(self.decisions)
            return body

    # ------------------------------------------------------------------ mutation

    def expiry(self, response: dict[str, Any], window_seconds: int) -> datetime | None:
        """When a step-up stops being answerable.

        The replica returns ``step_up_expires_at``; the real API does not (verified
        2026-09-24), so there it is bootstrap's step-up timeout from now.
        """
        raw = response.get("step_up_expires_at")
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(UTC) + timedelta(seconds=window_seconds)
