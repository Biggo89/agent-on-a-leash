"""Rolling-window approved-spend ledger.

The platform leaves period tracking to us: ``spend_in_period_before_chf`` is null on every
fixture. The window semantics are normative (specs/decision-rules.md §2.1):

    C.timestamp - N days  <=  A.timestamp  <  C.timestamp

Lower bound inclusive, upper exclusive, current attempt excluded. Only *finalized* approvals
count — a stepped-up authorization is pending and contributes nothing until resolved.

Getting this wrong in the obvious way (summing every approval since the run began) declines
fixture AU0011 at 387.50/300 when the true in-window figure is 223.00/300.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .money import Money


@dataclass(frozen=True, slots=True)
class Entry:
    authorization_id: str
    timestamp: datetime  # simulated scenario time, never the real clock
    amount: Money


@dataclass(slots=True)
class Ledger:
    """Per-card spend state for one run. Pure: the caller supplies every timestamp."""

    approvals: list[Entry] = field(default_factory=list)
    pending: dict[str, Entry] = field(default_factory=dict)

    def record_approval(self, authorization_id: str, timestamp: datetime, amount: Money) -> None:
        self.pending.pop(authorization_id, None)
        if any(e.authorization_id == authorization_id for e in self.approvals):
            return  # idempotent: at-least-once delivery must not double-count
        self.approvals.append(Entry(authorization_id, timestamp, amount))

    def record_step_up(self, authorization_id: str, timestamp: datetime, amount: Money) -> None:
        """Park a stepped-up authorization. It does NOT enter approved spend yet."""
        self.pending[authorization_id] = Entry(authorization_id, timestamp, amount)

    def resolve(self, authorization_id: str, approved: bool) -> None:
        entry = self.pending.pop(authorization_id, None)
        if entry is not None and approved:
            self.record_approval(entry.authorization_id, entry.timestamp, entry.amount)

    def spend_in_window(self, now: datetime, period_days: int) -> Money:
        """Sum of final approvals inside the rolling window ending at ``now`` (exclusive)."""
        start = now - timedelta(days=period_days)
        total = Money.zero()
        for e in self.approvals:
            if start <= e.timestamp < now:
                total = total + e.amount
        return total

    def entries_in_window(self, now: datetime, period_days: int) -> list[Entry]:
        start = now - timedelta(days=period_days)
        return [e for e in self.approvals if start <= e.timestamp < now]

    def cumulative(self) -> Money:
        """Total of all approvals. Present ONLY to demonstrate the wrong model in the demo."""
        total = Money.zero()
        for e in self.approvals:
            total = total + e.amount
        return total
