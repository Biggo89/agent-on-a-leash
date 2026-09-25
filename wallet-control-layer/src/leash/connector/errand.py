"""The agent's errand: a run fed by tool calls instead of a platform queue.

Everything a scenario run has — a mandate snapshot, a rolling-window ledger, the decided set,
step-ups waiting on the customer — an agent errand has too, in the same ``RunSession``. So the
phone lists its step-ups from ``/v1/step-ups`` and answers them through
``/v1/step-ups/{id}/resolve``, the demo reads its decisions from ``/v1/decisions``, and a
tightening from the app lands on its next order through the Supervisor's own
``_refresh_sessions``. What differs is only where the events come from: ``request_payment``
instead of a long-poll. Nothing here decides; ``domain.evaluator`` still does.

An errand registers itself in the Supervisor's ``sessions`` and ``runners`` tables, where a
``ScenarioRunner`` normally lives. The Supervisor calls exactly one method on a runner for a
step-up — ``resolve()`` — and this class answers to it.

ASSUMPTION: the runtime modules are mid-change in the working tree, so the attach point is
those two tables rather than a new ``Supervisor.attach_run()``; promote it once they settle.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..adapters.client import UpstreamResult
from ..adapters.parse import parse_event
from ..audit.record import decision_record, decision_summary, resolution_record
from ..domain import deadline as guard
from ..domain.evaluator import evaluate
from ..domain.money import Money
from ..domain.policy import period_caps, period_windows
from ..domain.types import Decision
from ..runtime.session import RunSession, StepUp
from ..runtime.supervisor import Supervisor, SupervisorError
from . import render
from .auth import Principal
from .orders import build_event

log = logging.getLogger("leash.connector")

#: How long a mandate's upstream status is trusted before it is read again. A revocation from
#: the app reaches the errand through `Connector.revoke_agent` at once; this catches one made
#: anywhere else (the leash demo, Postman) within a few seconds and one upstream call.
MANDATE_RECHECK_SECONDS = 5.0

#: The `upstream` block of an errand's records. Nothing is submitted to a platform — the
#: connector is the platform for these orders — and the trail says so rather than leaving the
#: field to be misread as a failed submission.
CONNECTOR_UPSTREAM: dict[str, Any] = {
    "submitted": False,
    "http_status": None,
    "platform_status": None,
    "idempotent": False,
    "error": None,
    "channel": "connector",
}

FINAL = {"approve": "approved", "decline": "declined", "expired": "expired"}

#: What `payment_status` says once the cardholder has answered a step-up. The decision's own
#: message asked for the confirmation; repeating it after the answer told the agent the order
#: was still waiting — Claude Code read it that way on 2026-09-24 and said so.
ANSWERED = {
    "approve": "The cardholder approved it in the app; the order is through.",
    "decline": "The cardholder declined it in the app; nothing was charged.",
    "expired": "The cardholder did not answer in time; nothing was charged.",
}


class ErrandError(Exception):
    """Something the agent can act on, in words written for it."""


@dataclass(slots=True)
class Proposal:
    """A mandate an agent drafted. Its status lives with the Supervisor's copy."""

    draft_id: str
    subject: str
    card_id: str
    client_id: str
    agent_name: str
    instruction: str
    proposed_at: datetime
    revoked: bool = False
    ir: dict[str, Any] = field(default_factory=dict)  # the compiled rules, as proposed

    def as_record(self) -> dict[str, Any]:
        """What survives a restart (`LEASH_CONNECTOR_STATE`)."""
        return {
            "draft_id": self.draft_id,
            "subject": self.subject,
            "card_id": self.card_id,
            "client_id": self.client_id,
            "agent_name": self.agent_name,
            "instruction": self.instruction,
            "proposed_at": _iso(self.proposed_at),
            "revoked": self.revoked,
            "ir": self.ir,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Proposal:
        return cls(
            draft_id=str(record["draft_id"]),
            subject=str(record.get("subject", "")),
            card_id=str(record.get("card_id", "")),
            client_id=str(record.get("client_id", "")),
            agent_name=str(record.get("agent_name", "")),
            instruction=str(record.get("instruction", "")),
            proposed_at=datetime.fromisoformat(
                str(record.get("proposed_at", "")).replace("Z", "+00:00")
            ),
            revoked=bool(record.get("revoked", False)),
            ir=dict(record.get("ir") or {}),
        )

    def as_dict(self, status: str, mandate_id: str) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "mandate_id": mandate_id,
            "status": status,
            "subject": self.subject,
            "card_id": self.card_id,
            "client_id": self.client_id,
            "agent_name": self.agent_name,
            "instruction": self.instruction,
            "proposed_at": _iso(self.proposed_at),
        }


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class Errand:
    """One agent, one cardholder, one confirmed mandate, any number of orders."""

    def __init__(
        self, supervisor: Supervisor, session: RunSession, principal: Principal, mandate_id: str
    ) -> None:
        self.supervisor = supervisor
        self.session = session
        self.subject = principal.subject
        self.card_id = principal.card_id
        self.client_id = principal.client_id
        self.agent_name = principal.agent_name
        self.device_id = principal.device_id
        self.mandate_id = mandate_id
        self.started_at = datetime.now(UTC)
        self.stopped_reason: str | None = None
        self._mandate_status = "active"
        self._mandate_checked_at = time.monotonic()

    @property
    def run_id(self) -> str:
        return self.session.run_id

    @property
    def running(self) -> bool:
        return self.session.status == "running"

    # ------------------------------------------------------------------ mandate

    def mandate_resource(self) -> dict[str, Any]:
        # A dict read without the Supervisor's lock: the entry is replaced whole on confirm,
        # never mutated field by field, and holding a non-reentrant lock from outside the
        # class that owns it is how deadlocks start.
        entry = self.supervisor.mandates.get(self.mandate_id) or {}
        resource: dict[str, Any] = dict(entry.get("resource") or {})
        return resource

    def mandate_active(self) -> bool:
        if not self.running:
            return False
        if time.monotonic() - self._mandate_checked_at < MANDATE_RECHECK_SECONDS:
            return self._mandate_status == "active"
        try:
            status = str(self.supervisor.get_mandate(self.mandate_id).get("status", "active"))
        except SupervisorError as exc:
            # Unreachable is not revoked: keep the last answer rather than stop an errand
            # because the replica blinked.
            log.warning("could not re-read mandate %s: %s", self.mandate_id, exc.message)
            status = self._mandate_status
        self._mandate_status = status
        self._mandate_checked_at = time.monotonic()
        if status != "active":
            self.stop(f"the mandate is {status}")
        return status == "active"

    def stop(self, reason: str) -> None:
        with self.session.lock:
            if self.session.status == "running":
                self.session.status = "stopped"
                self.session.error = reason
        self.session.stop_requested.set()
        self.stopped_reason = self.stopped_reason or reason

    # ------------------------------------------------------------------ the window

    def window(self, now: datetime | None = None) -> dict[str, Any]:
        """``RunSession.window()`` with the clock at *now* rather than at the latest attempt.

        A scenario run reports the window as the latest attempt saw it, which by the rolling
        rule (upper bound exclusive) leaves that attempt out. An agent reads the figure *after*
        its order, so the order it just placed has to be in it — the same ledger, one instant
        later.
        """
        now = now or datetime.now(UTC)
        # The rolling rule's upper bound is exclusive, so the instant itself is nudged past:
        # an order stamped *now* is in the figure that answers it.
        edge = now + timedelta(microseconds=1)
        session = self.session
        with session.lock:
            pending = Money.zero()
            for entry in list(session.ledger.pending.values()):
                pending = pending + entry.amount
            rules = list(session.policy.get("hard_rules", []))
            limits = {
                days: str(Money.from_value(rule["value"]))
                for days, rule in period_caps(rules)
                if days is not None
            }
            windows = [
                {
                    "period_days": days,
                    "approved_spend_chf": str(session.ledger.spend_in_window(edge, days)),
                    "approvals_in_window": len(session.ledger.entries_in_window(edge, days)),
                    "limit_chf": limits.get(days),
                }
                for days in session.period_windows
            ]
        primary = windows[0] if windows else None
        return {
            "period_days": primary["period_days"] if primary else None,
            "approved_spend_chf": primary["approved_spend_chf"] if primary else None,
            "approvals_in_window": primary["approvals_in_window"] if primary else None,
            "limit_chf": primary["limit_chf"] if primary else None,
            "windows": windows,
            "pending_step_up_chf": str(pending),
            "as_of": _iso(now),
        }

    # ------------------------------------------------------------------ orders

    def request(self, order: dict[str, Any]) -> dict[str, Any]:
        """Decide one order. The same steps as ``ScenarioRunner._decide`` minus the submit."""
        now = datetime.now(UTC)
        self.expire(now)
        if not self.mandate_active():
            raise ErrandError(
                f"This errand is over: {self.stopped_reason or 'the mandate is no longer active'}. "
                "Nothing was charged. Ask the cardholder whether they want to set up a new one."
            )
        sv = self.supervisor
        session = self.session
        window_seconds = sv.step_up_window_seconds()
        # Evaluation is sub-millisecond, so the whole step holds the session lock: the ledger
        # `parse_event` reads must be the ledger `_apply` writes, with no resolution between.
        with session.lock:
            decided = {k: str(v.get("decision", "")) for k, v in session.decided.items()}
            event = build_event(
                order,
                card_id=self.card_id,
                customer_id=self.subject,
                device_id=self.device_id,
                mandate=self.mandate_resource(),
                merchants=sv.pack.merchants,
                fx=sv.pack.fx,
                seen=session.seen,
                decided=decided,
                sequence=len(session.seen) + 1,
                now=now,
            )
            deadline_at = guard.parse_deadline(event)
            budget = guard.budget_ms(deadline_at, now)
            ev = parse_event(
                event,
                history=sv.history,
                ledger=session.ledger,
                merchants=sv.pack.merchants,
                period_windows=session.period_windows,
                seen_in_run=session.seen,
            )
            policy = dict(session.policy)
            record = evaluate(ev, policy, engine_version=sv.engine_version, now=now)
            payload = record.to_payload()

            session.decided[ev.authorization_id] = payload
            session.seen.append(ev)
            session.delivered += 1
            if record.decision is Decision.APPROVE:
                session.ledger.record_approval(
                    ev.authorization_id, ev.timestamp, ev.billing_amount_chf
                )
            elif record.decision is Decision.STEP_UP:
                session.ledger.record_step_up(
                    ev.authorization_id, ev.timestamp, ev.billing_amount_chf
                )
                session.step_ups[ev.authorization_id] = StepUp(
                    authorization_id=ev.authorization_id,
                    source_authorization_id=ev.authorization_id,
                    run_id=session.run_id,
                    asked_at=record.decided_at,
                    expires_at=now + timedelta(seconds=window_seconds),
                    amount_chf=str(ev.billing_amount_chf),
                    merchant_name=ev.merchant_name,
                    customer_message=record.customer_message,
                    reason_codes=list(record.reason_codes),
                    evidence=[e.as_dict() for e in record.evidence],
                )
            summary = decision_summary(record, ev)
            summary["upstream"] = dict(CONNECTOR_UPSTREAM)
            summary["merchant_known"] = bool(event["runtime"].get("merchant_known"))
            summary["timing"]["budget_ms"] = round(budget, 1) if budget is not None else None
            summary["window"] = self.window(now)
            session.decisions.append(summary)
        sv.audit.append(
            decision_record(
                record,
                ev,
                policy,
                run_id=session.run_id,
                scenario_id=session.scenario_id,
                deadline_at=deadline_at,
                budget_ms=budget,
                guard_tripped=False,
                upstream=dict(CONNECTOR_UPSTREAM),
            )
        )
        return summary

    def status(self, authorization_id: str) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        self.expire(now)
        session = self.session
        with session.lock:
            payload = session.decided.get(authorization_id)
            if payload is None:
                return None
            summary = next(
                (d for d in session.decisions if d.get("authorization_id") == authorization_id),
                {},
            )
            step = session.step_ups.get(authorization_id)
            step_view = step.as_dict(now) if step is not None else None
        window = self.window(now)
        decision = str(payload.get("decision", ""))
        remaining: float | None = None
        if decision == "step_up":
            if step is None or step.resolved is None:
                state = "pending"
                remaining = step_view["seconds_remaining"] if step_view else None
            else:
                state = FINAL.get(step.resolved, step.resolved)
        else:
            state = "approved" if decision == "approve" else "declined"
        subject = summary.get("authorization", {})
        asked = str(payload.get("customer_message", ""))
        message = asked
        if step is not None and step.resolved is not None:
            message = ANSWERED.get(step.resolved, asked)
        return {
            "authorization_id": authorization_id,
            "decision": decision,
            "status": state,
            "amount_chf": subject.get("billing_amount_chf")
            or (step_view["amount_chf"] if step_view else None),
            "merchant_name": subject.get("merchant_name")
            or (step_view["merchant_name"] if step_view else None),
            "customer_message": message,
            "asked_message": asked if decision == "step_up" else None,
            "reason_codes": list(payload.get("reason_codes") or []),
            "seconds_remaining": remaining,
            "decided_at": summary.get("decided_at"),
            "window": window,
        }

    def activity(self, limit: int) -> list[dict[str, Any]]:
        with self.session.lock:
            ids = [str(d.get("authorization_id")) for d in reversed(self.session.decisions)]
        rows = [self.status(auth_id) for auth_id in ids[: max(1, limit)]]
        return [r for r in rows if r is not None]

    # ------------------------------------------------------------------ step-ups

    def resolve(self, authorization_id: str, outcome: str, message: str = "") -> UpstreamResult:
        """The cardholder's answer, from the phone through ``Supervisor.resolve_step_up``.

        Returns the shape a ``ScenarioRunner`` returns, so the Supervisor needs no second
        path: an approved step-up enters approved spend now, not when it was asked.
        """
        now = datetime.now(UTC)
        session = self.session
        with session.lock:
            step = session.step_ups.get(authorization_id)
            if step is None:
                return UpstreamResult(
                    404, error_code="not_found", error_message="no such step-up on this errand"
                )
            if step.resolved is not None:
                return UpstreamResult(
                    409, error_code="already_resolved", error_message="already answered"
                )
            if step.expires_at is not None and now > step.expires_at:
                self._expire_one(step, now)
                expired = [authorization_id]
            else:
                expired = []
                session.ledger.resolve(authorization_id, outcome == "approve")
                step.resolved = outcome
        if expired:
            self._record_expiry(expired, now)
            return UpstreamResult(
                409,
                error_code="step_up_expired",
                error_message="the window closed before the answer arrived; nothing was charged",
            )
        self.supervisor.audit.append(
            resolution_record(
                authorization_id,
                outcome,
                resolved_at=now,
                customer_message=message,
                upstream={"submitted": False, "http_status": None, "error": None},
            )
        )
        return UpstreamResult(
            200, body={"status": "approved" if outcome == "approve" else "declined"}
        )

    def expire(self, now: datetime) -> list[str]:
        """Close every step-up whose window passed unanswered. Nothing was charged.

        A platform run has the platform for this; an errand is its own platform, so it keeps
        its own clock — lazily, on the next call that looks, which is all a queue needs.
        """
        expired: list[str] = []
        with self.session.lock:
            for step in self.session.step_ups.values():
                if step.resolved is None and step.expires_at is not None and now > step.expires_at:
                    self._expire_one(step, now)
                    expired.append(step.authorization_id)
        self._record_expiry(expired, now)
        return expired

    def _expire_one(self, step: StepUp, now: datetime) -> None:
        step.resolved = "expired"
        self.session.ledger.resolve(step.authorization_id, False)

    def _record_expiry(self, ids: list[str], now: datetime) -> None:
        for auth_id in ids:
            self.supervisor.audit.append(
                resolution_record(
                    auth_id,
                    "expired",
                    resolved_at=now,
                    customer_message="No answer within the window; nothing was charged.",
                    resolved_by="timeout",
                    upstream={"submitted": False, "http_status": None, "error": None},
                )
            )

    # ------------------------------------------------------------------ views

    def snapshot(self) -> dict[str, Any]:
        self.expire(datetime.now(UTC))
        with self.session.lock:
            waiting = [s.as_dict() for s in self.session.step_ups.values() if s.resolved is None]
            body = {
                "client_id": self.client_id,
                "agent_name": self.agent_name,
                "subject": self.subject,
                "card_id": self.card_id,
                "device_id": self.device_id,
                "mandate_id": self.mandate_id,
                "run_id": self.session.run_id,
                "status": self.session.status,
                "stopped_reason": self.stopped_reason,
                "started_at": _iso(self.started_at),
                "orders": len(self.session.decided),
                "counters": self.session.counters(),
                "window": self.window(),
                "waiting": waiting,
            }
        return body


class Connector:
    """Every proposal and errand this process knows, keyed by (cardholder, agent).

    The proposals — which agent drafted which mandate for whom, and the rules it compiled to —
    outlive the process when `LEASH_CONNECTOR_STATE` names a file: the Supervisor's own copy
    of a mandate is in memory, so after a restart it would answer "no active mandate" to an
    agent whose mandate is active upstream (Claude Code was told exactly that on 2026-09-24).
    The errand's ledger does not survive; the audit trail keeps what was approved.
    """

    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor
        self.proposals: list[Proposal] = []
        self.errands: dict[tuple[str, str], Errand] = {}
        self._lock = threading.Lock()
        raw = os.environ.get("LEASH_CONNECTOR_STATE", "").strip()
        self.path: Path | None = Path(raw) if raw else None
        self._load()

    # ------------------------------------------------------------------ state

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); starting without proposals", self.path, exc)
            return
        self.proposals = [Proposal.from_record(r) for r in records.get("proposals", [])]
        for proposal in self.proposals:
            if not proposal.revoked:
                self._rehydrate(proposal.draft_id)

    def _save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            records = [p.as_record() for p in self.proposals]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"proposals": records}, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _rehydrate(self, draft_id: str) -> dict[str, Any] | None:
        """After a restart the Supervisor's copy is empty. The proposal carries the compiled
        IR and upstream carries the status; put the two back where every reader looks — the
        phone's `/v1/mandates`, the errand's policy."""
        with self._lock:
            proposal = next((p for p in self.proposals if p.draft_id == draft_id), None)
        if proposal is None or not proposal.ir:
            return None
        try:
            resource = self.supervisor.get_mandate(draft_id)
        except SupervisorError as exc:
            log.warning("cannot re-read mandate %s after a restart: %s", draft_id, exc.message)
            return None
        resource.pop("ir", None)
        entry = {"ir": dict(proposal.ir), "resource": resource}
        with self.supervisor._lock:
            self.supervisor.mandates.setdefault(draft_id, entry)
            self.supervisor.mandates.setdefault(str(resource.get("mandate_id") or draft_id), entry)
        return entry

    # ------------------------------------------------------------------ mandates

    def propose(self, principal: Principal, instruction: str) -> dict[str, Any]:
        if not instruction.strip():
            raise ErrandError(
                "instruction must be the cardholder's own words — what to buy and within "
                "what limits. Ask them, then pass their sentence unchanged."
            )
        draft = self.supervisor.create_mandate(instruction)
        ir: dict[str, Any] = dict(draft.get("ir") or {})
        proposal = Proposal(
            draft_id=str(draft["draft_id"]),
            subject=principal.subject,
            card_id=principal.card_id,
            client_id=principal.client_id,
            agent_name=principal.agent_name,
            instruction=instruction,
            proposed_at=datetime.now(UTC),
            ir=ir,
        )
        with self._lock:
            self.proposals.append(proposal)
        self._save()
        return {
            "draft_id": proposal.draft_id,
            "status": "draft",
            "rules": render.mandate_sentences(ir),
            "guidance": list(ir.get("guidance") or draft.get("guidance") or []),
            "open_questions": list(ir.get("open_questions") or draft.get("open_questions") or []),
            "compiler": ir.get("compiler"),
            "compiler_notes": list(ir.get("compiler_notes") or []),
            "next": "waiting for the cardholder to confirm it in their app",
        }

    def _entry(self, draft_id: str) -> dict[str, Any]:
        entry = self.supervisor.mandates.get(draft_id)
        if entry is None:
            entry = self._rehydrate(draft_id)
        return dict(entry or {})

    def proposal_state(self, proposal: Proposal) -> tuple[str, str]:
        """(status, mandate_id) as the Supervisor's copy reports them now."""
        resource = self._entry(proposal.draft_id).get("resource") or {}
        status = "revoked" if proposal.revoked else str(resource.get("status") or "draft")
        return status, str(resource.get("mandate_id") or proposal.draft_id)

    def _mine(self, principal: Principal) -> list[Proposal]:
        with self._lock:
            return [
                p
                for p in reversed(self.proposals)
                if (p.subject, p.client_id) == principal.key and not p.revoked
            ]

    def active_mandate(self, principal: Principal) -> tuple[str, dict[str, Any]] | None:
        """The newest confirmed mandate this agent proposed for this cardholder."""
        for proposal in self._mine(principal):
            status, mandate_id = self.proposal_state(proposal)
            if status == "active":
                return mandate_id, self._entry(proposal.draft_id)
        return None

    def pending_drafts(self, principal: Principal) -> list[Proposal]:
        return [p for p in self._mine(principal) if self.proposal_state(p)[0] == "draft"]

    # ------------------------------------------------------------------ errands

    def errand_for(self, principal: Principal, *, create: bool) -> Errand | None:
        """The running errand for this agent, started on first use once a mandate is active.

        A newer confirmation supersedes: the errand under the old mandate stops and a fresh
        one starts, so a re-proposed instruction never runs with a stale ledger.
        """
        active = self.active_mandate(principal)
        with self._lock:
            current = self.errands.get(principal.key)
        if current is not None and current.running:
            if active is None:
                current.stop("the mandate is no longer active")
                return None
            if current.mandate_id == active[0]:
                return current
            current.stop(f"superseded by mandate {active[0]}")
        if active is None or not create:
            return None
        return self._start(principal, *active)

    def _start(self, principal: Principal, mandate_id: str, entry: dict[str, Any]) -> Errand:
        ir: dict[str, Any] = dict(entry.get("ir") or {})
        # Composed the way a scenario run is (standing preferences, then the mandate), with
        # no account layer: the composition path keyed on scenario has none for an errand
        # either, so a mid-run refresh and the start see the same layers.
        policy = self.supervisor.effective_policy(ir, None)
        session = RunSession(
            run_id=f"AG{uuid.uuid4().hex[:8].upper()}",
            scenario_id="agent",
            mandate_id=mandate_id,
            policy=policy,
            period_windows=period_windows(policy["hard_rules"]),
            total=0,
            ir=ir,
        )
        errand = Errand(self.supervisor, session, principal, mandate_id)
        # The Supervisor's own tables, so /v1/step-ups, /v1/decisions, /v1/runs and a
        # tightening from the app all reach this run without a second code path. The runner
        # table is typed for ScenarioRunner; the one method the Supervisor calls on it is
        # `resolve()`, which Errand provides with the same signature and result.
        with self.supervisor._lock:
            self.supervisor.sessions[session.run_id] = session
            self.supervisor.runners[session.run_id] = errand  # type: ignore[assignment]
        with self._lock:
            self.errands[principal.key] = errand
        log.info(
            "errand %s: %s for %s under mandate %s",
            session.run_id,
            principal.agent_name,
            principal.subject,
            mandate_id,
        )
        return errand

    def _last_errand(self, principal: Principal) -> Errand | None:
        with self._lock:
            return self.errands.get(principal.key)

    def _no_mandate(self, principal: Principal) -> str:
        drafts = self.pending_drafts(principal)
        if drafts:
            return (
                f"Draft {drafts[0].draft_id} is waiting for the cardholder to confirm it in "
                "their app. Nothing can be bought until they do. Ask them to open the app, "
                "then try again."
            )
        return (
            "No active mandate for this cardholder. Ask them what you should buy and within "
            "what limits, then call propose_mandate with their words unchanged. They confirm "
            "it in their own app; you cannot confirm it here."
        )

    # ------------------------------------------------------------------ the tools

    def allowed(self, principal: Principal) -> dict[str, Any]:
        active = self.active_mandate(principal)
        drafts = self.pending_drafts(principal)
        errand = self.errand_for(principal, create=False)
        out: dict[str, Any] = {
            "cardholder": principal.subject,
            "card_id": principal.card_id,
            "agent": principal.agent_name,
            "scopes": sorted(principal.scopes),
            "mandate": None,
            "drafts_waiting": [d.draft_id for d in drafts],
            "known_shops": self.known_shops(principal.card_id),
            "window": None,
        }
        lines: list[str] = []
        if active is None:
            lines.append(self._no_mandate(principal))
        else:
            mandate_id, entry = active
            ir: dict[str, Any] = dict(entry.get("ir") or {})
            resource = entry.get("resource") or {}
            out["mandate"] = {
                "mandate_id": mandate_id,
                "instruction": resource.get("instruction") or ir.get("source_instruction"),
                "rules": render.mandate_sentences(ir),
                "guidance": list(ir.get("guidance") or []),
                "open_questions": list(ir.get("open_questions") or []),
            }
            lines.append(f'The cardholder said: "{out["mandate"]["instruction"]}"')
            lines.append("Rules in force:")
            lines += [f"  - {s}" for s in out["mandate"]["rules"]]
            for note in out["mandate"]["guidance"]:
                lines.append(f"  Note: {note}")
            if errand is not None:
                out["window"] = errand.window()
                spent = render.window_sentence(out["window"])
                if spent:
                    lines.append(spent)
                pending = out["window"].get("pending_step_up_chf")
                if pending and pending != "0.00":
                    lines.append(f"Waiting on the cardholder right now: CHF {pending}")
        if out["known_shops"]:
            shops = ", ".join(
                f"{s['merchant_name']} ({s['merchant_category']}, {s['prior_purchases']} purchases)"
                for s in out["known_shops"][:8]
            )
            lines.append(f"Shops this card already uses: {shops}.")
        out["text"] = "\n".join(lines)
        return out

    def known_shops(self, card_id: str) -> list[dict[str, Any]]:
        history = self.supervisor.history
        rows = []
        for merchant_id in history.known_merchants(card_id):
            row = self.supervisor.pack.merchants.get(merchant_id)
            if row is None:
                continue
            rows.append(
                {
                    "merchant_id": merchant_id,
                    "merchant_name": row["merchant_name"],
                    "merchant_category": row["merchant_category"],
                    "prior_purchases": history.merchant_approvals(card_id, merchant_id),
                }
            )
        rows.sort(key=lambda r: (-int(r["prior_purchases"]), str(r["merchant_name"])))
        return rows[:12]

    def request_payment(self, principal: Principal, order: dict[str, Any]) -> dict[str, Any]:
        errand = self.errand_for(principal, create=True)
        if errand is None:
            raise ErrandError(self._no_mandate(principal))
        return errand.request(order)

    def payment_status(self, principal: Principal, authorization_id: str) -> dict[str, Any]:
        errand = self._last_errand(principal)
        status = errand.status(authorization_id) if errand is not None else None
        if status is None:
            raise ErrandError(
                f"No order {authorization_id!r} on this errand. The id is the one "
                "request_payment returned."
            )
        return status

    def recent_activity(self, principal: Principal, limit: int) -> list[dict[str, Any]]:
        errand = self._last_errand(principal)
        return errand.activity(limit) if errand is not None else []

    # ------------------------------------------------------------------ the phone

    def revoke_agent(self, client_id: str, *, subject: str | None = None) -> dict[str, Any]:
        """The cardholder pulled the leash: stop every errand and revoke every mandate this
        agent holds for them (every cardholder's when `subject` is None). The app revokes the
        token itself; together that is the whole cut."""
        stopped: list[str] = []
        revoked: list[str] = []
        errors: list[str] = []

        def theirs(owner: str) -> bool:
            return subject is None or owner == subject

        with self._lock:
            errands = [
                e for e in self.errands.values() if e.client_id == client_id and theirs(e.subject)
            ]
            proposals = [
                p
                for p in self.proposals
                if p.client_id == client_id and not p.revoked and theirs(p.subject)
            ]
        for errand in errands:
            if errand.running:
                errand.stop("revoked by the cardholder in the app")
                stopped.append(errand.run_id)
        for proposal in proposals:
            status, mandate_id = self.proposal_state(proposal)
            proposal.revoked = True
            if status in ("active", "draft"):
                try:
                    self.supervisor.revoke_mandate(mandate_id)
                    revoked.append(mandate_id)
                except SupervisorError as exc:
                    errors.append(f"{mandate_id}: {exc.code} {exc.message}")
        if proposals:
            self._save()
        return {
            "client_id": client_id,
            "errands_stopped": stopped,
            "mandates_revoked": revoked,
            "errors": errors,
        }

    def agents(self) -> list[dict[str, Any]]:
        with self._lock:
            errands = list(self.errands.values())
        return [e.snapshot() for e in errands]

    def proposals_view(self) -> list[dict[str, Any]]:
        with self._lock:
            proposals = list(self.proposals)
        return [p.as_dict(*self.proposal_state(p)) for p in proposals]
