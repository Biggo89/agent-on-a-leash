"""The live run loop: long-poll → decide → submit, with the failures the protocol promises.

    "Requests may be delivered at least once. Treat the authorization ID as the idempotency
     key and make repeated responses safe."  — technical_details.md

Four things this has to survive, none of which the offline replay path ever exercised:

* **redelivery** — the same authorization arrives twice. Resubmit the stored decision with
  ``idempotent_replay``; never re-evaluate, because our own state has moved since.
* **409** — the platform already has a decision, or the authorization is stepped up and only
  ``/resolve`` applies. Record it and move on; do not fight it.
* **a step-up that holds the run** — the real platform generates nothing more for a run while
  one of its step-ups waits on the customer, and hands that request back on every poll with
  ``status: "pending_step_up"``. That is not a redelivery to answer: wait for the human.
* **a stopped run** — once its mandate is revoked, or it is stopped, nothing more is decided
  under it, even when another run's loop is handed one of its requests.
* **408** — our decision arrived after ``deadline_at`` and the platform declined it. The
  record stands and says so; nothing enters approved spend.
* **restart** — the process died mid-run. Rebuild the decided set and the ledger from
  ``/v1/authorizations``, then resume the event feed from the stored cursor.

Runs on its own thread, one per run. Everything shared with the service goes through
``RunSession.lock``.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from ..adapters.client import (
    PENDING_STEP_UP_STATUSES,
    SandboxClient,
    UpstreamResult,
    authorization_decision,
    run_complete,
    run_total,
)
from ..adapters.history import HistoryIndex
from ..adapters.parse import parse_event
from ..audit.log import AuditLog
from ..audit.record import (
    decision_context,
    decision_record,
    decision_summary,
    replay_record,
    resolution_record,
    unowned_record,
)
from ..domain import deadline as guard
from ..domain.evaluator import evaluate
from ..domain.sanitize import safe_display
from ..domain.settings import UNCERTAINTY_ORDER
from ..domain.types import Decision, DecisionRecord, EnrichedEvent
from .session import RunSession, StepUp

log = logging.getLogger("leash.runner")

# Long-poll length. The platform allows 25 s; shorter keeps a stop request responsive and
# costs nothing but an extra round trip on an idle queue.
POLL_SECONDS = 5
# Empty polls before a run that the platform never calls complete is abandoned, so a hung
# run does not spin for the rest of the event.
MAX_IDLE_POLLS = 12
# Bounded backoff for a transport failure. Beyond this the run is marked failed rather than
# hammering an API that is not answering.
RETRY_BACKOFF = (0.25, 0.5, 1.0)
# How long to wait before polling again when the platform keeps handing back a request nobody
# here may answer: a step-up waiting on the customer, which it returns at once rather than
# long-polling, or an order of a run that was stopped. Without it the loop spins.
STEP_UP_WAIT_SECONDS = 1.0


class ScenarioRunner:
    """Drives one scenario run to completion. Owns no decision logic."""

    def __init__(
        self,
        client: SandboxClient,
        session: RunSession,
        *,
        history: HistoryIndex,
        merchants: dict[str, dict[str, str]],
        audit: AuditLog,
        engine_version: str = "leash-0.1.0",
        step_up_window_seconds: int = 120,
        reserve_ms: float = guard.RESERVE_MS,
        on_decision: Any = None,  # callable(session, summary) -> None, for the UI feed
        dispatch: Any = None,  # callable(run_id) -> ScenarioRunner | None
    ) -> None:
        self.client = client
        self.session = session
        self.history = history
        self.merchants = merchants
        self.audit = audit
        self.engine_version = engine_version
        self.step_up_window_seconds = step_up_window_seconds
        self.reserve_ms = reserve_ms
        self.on_decision = on_decision
        self.dispatch = dispatch

    # ------------------------------------------------------------------ loop

    def run(self) -> None:
        """Poll until the run is complete, stopped, or the API stops answering."""
        session = self.session
        try:
            self.resume()
            idle = 0
            while not session.stop_requested.is_set():
                envelope = self._poll()
                if envelope is None:
                    if self._complete():
                        session.status = "complete"
                        return
                    idle += 1
                    if idle >= MAX_IDLE_POLLS:
                        session.status = "stopped"
                        session.error = "the platform stopped delivering before the run completed"
                        log.warning("run %s abandoned after %d idle polls", session.run_id, idle)
                        return
                    continue
                idle = 0
                self.handle(envelope)
            session.status = "stopped"
        except Exception as exc:  # a dead loop must say why, not vanish
            log.exception("run %s failed", session.run_id)
            session.status = "failed"
            session.error = f"{type(exc).__name__}: {exc}"

    def _poll(self) -> dict[str, Any] | None:
        for delay in (*RETRY_BACKOFF, None):
            try:
                return self.client.next_request(wait=POLL_SECONDS)
            except Exception as exc:
                if delay is None:
                    raise
                log.warning("poll failed (%s); retrying in %.2fs", exc, delay)
                time.sleep(delay)
        return None

    def _complete(self) -> bool:
        """Ask the platform, and fall back to our own counters if it will not say."""
        try:
            progress = self.client.get_run(self.session.run_id)
        except Exception:
            return len(self.session.decided) >= self.session.total
        total = run_total(progress)
        if total is not None:
            with self.session.lock:
                self.session.total = total
        return run_complete(progress)

    # ------------------------------------------------------------------ resume

    def resume(self) -> None:
        """Rebuild state after a restart, so a redelivered event is recognised as one.

        Verified against the real API on 2026-09-24: its rows nest the submitted decision
        object under ``decision`` and the purchase under ``authorization``, where the replica
        has flat fields. Both shapes are read.
        """
        session = self.session
        try:
            listed = self.client.authorizations()
        except Exception as exc:
            log.warning("resume: could not read /v1/authorizations (%s)", exc)
            return
        restored = 0
        for auth in listed:
            decision = authorization_decision(auth)
            if auth.get("run_id") != session.run_id or not decision:
                continue
            auth_id = str(auth["authorization_id"])
            if auth_id in session.decided:
                continue
            submitted = auth["decision"] if isinstance(auth.get("decision"), dict) else {}
            session.decided[auth_id] = {
                "authorization_id": auth_id,
                "decision": decision,
                "reason_codes": list(auth.get("reason_codes") or []),
                "customer_message": auth.get("customer_message")
                or submitted.get("customer_message", ""),
                "engine_version": self.engine_version,
            }
            restored += 1
            timestamp, amount = _ledger_inputs(auth)
            if timestamp is None or amount is None:
                continue
            if auth.get("status") == "approved":
                session.ledger.record_approval(auth_id, timestamp, amount)
            elif decision == "step_up" and auth.get("status") in PENDING_STEP_UP_STATUSES:
                session.ledger.record_step_up(auth_id, timestamp, amount)
        if restored:
            log.info(
                "resumed %s: %d decision(s) restored from the platform", session.run_id, restored
            )
        self._advance_cursor()

    def _advance_cursor(self) -> None:
        """Drain the append-only feed so the stored cursor survives a restart."""
        try:
            feed = self.client.events(since=self.session.cursor)
        except Exception as exc:
            log.warning("resume: could not read the event feed (%s)", exc)
            return
        self.session.cursor = int(feed.get("next_cursor", self.session.cursor))

    # ------------------------------------------------------------------ one event

    def handle(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        """Decide and submit one delivered authorization. Returns the decision summary."""
        if envelope.get("status") in PENDING_STEP_UP_STATUSES:
            # Not work. The platform holds a run at an unanswered step-up and returns that
            # same request on every poll until the customer answers or the window closes. A
            # second automated decision is refused (409 `step_up_resolution_required`) and
            # the organizers' guide forbids sending one — so wait for the human, briefly,
            # rather than resubmitting or spinning on the endpoint.
            self.session.stop_requested.wait(STEP_UP_WAIT_SECONDS)
            return None

        # The decision queue is team-global: with two runs in flight, either loop can be
        # handed the other's authorization. Route it to the run that owns it, so it is judged
        # against that run's facets and counted in that run's window.
        delivered_run = str(envelope.get("run_id") or "")
        if delivered_run and delivered_run != self.session.run_id:
            owner = self.dispatch(delivered_run) if self.dispatch is not None else None
            if owner is not None:
                if owner.session.stop_requested.is_set():
                    # Its run was stopped — its mandate revoked, or `/stop`. Answered here, it
                    # could be approved after the customer withdrew the permission it needed;
                    # left alone, the platform declines it at its deadline.
                    self.session.stop_requested.wait(STEP_UP_WAIT_SECONDS)
                    return None
                handled: dict[str, Any] | None = owner.handle(envelope)
                return handled
            # Unknown run: answer it, but not with this run's policy. See _answer_unowned.
            log.warning("delivered an authorization for unknown run %s", delivered_run)
            return self._answer_unowned(envelope)

        session = self.session
        event = envelope["data"]
        auth_id = event["authorization"]["authorization_id"]
        session.cursor = max(session.cursor, int(envelope.get("event_id", 0)))

        with session.lock:
            already = session.decided.get(auth_id)
            # Claimed in the same critical section as the check. Two loops can hold one
            # request at once — the queue is team-global and the real platform hands a request
            # to every concurrent poller — and a check-then-act here let both evaluate, submit
            # and record: measured 2026-09-24, AU0009–AU0011 each decided twice ~20 ms apart,
            # and every counter of the run double-counted.
            racing = already is None and auth_id in session.in_flight
            if already is None and not racing:
                session.in_flight.add(auth_id)
            session.delivered += 1
        if already is not None:
            self._replay(auth_id, already)
            return None
        if racing:
            return None  # the thread deciding it submits once, for both deliveries
        try:
            return self._decide(event, auth_id)
        finally:
            with session.lock:
                session.in_flight.discard(auth_id)

    def _decide(self, event: dict[str, Any], auth_id: str) -> dict[str, Any]:
        """Evaluate, submit and record one authorization this run has claimed."""
        session = self.session
        now = datetime.now(UTC)
        deadline_at = guard.parse_deadline(event)
        budget = guard.budget_ms(deadline_at, now)
        policy = self._policy_for(event)

        ev = parse_event(
            event,
            history=self.history,
            ledger=session.ledger,
            merchants=self.merchants,
            period_windows=session.period_windows,
            seen_in_run=session.seen,
        )

        tripped = guard.at_risk(budget, reserve_ms=self.reserve_ms)
        if tripped:
            log.warning("deadline guard tripped for %s (%.0f ms left)", auth_id, budget or 0.0)
            record = guard.guard_record(
                ev,
                policy.get("uncertainty_policy", "ask"),
                budget=budget,
                engine_version=self.engine_version,
                decided_at=now,
                inputs_digest=_digest(event),
            )
        else:
            record = evaluate(ev, policy, engine_version=self.engine_version, now=now)

        payload = record.to_payload()
        result = self.client.try_submit_decision(auth_id, payload)
        self._apply(ev, record, payload, result)

        summary = decision_summary(record, ev)
        summary["upstream"] = result.as_dict()
        summary["context"] = decision_context(ev)
        self.audit.append(
            decision_record(
                record,
                ev,
                policy,
                run_id=session.run_id,
                scenario_id=session.scenario_id,
                deadline_at=deadline_at,
                budget_ms=budget,
                guard_tripped=tripped,
                upstream=result.as_dict(),
            )
        )
        with session.lock:
            session.decisions.append(summary)
        if self.on_decision is not None:
            self.on_decision(session, summary)
        return summary

    def _answer_unowned(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        """Answer an authorization whose run this process does not know about.

        The decision queue is team-global, so a second engine started against the same team
        key — two people both running `make serve` — can be handed another run's
        authorization. We must still answer, because a missing decision becomes a platform
        decline nobody ever explained.

        What we must NOT do is judge it against *this* run's policy. ``intent_facets`` are
        per-mandate and are not carried on the wire, so borrowing ours applies the wrong rules
        with full confidence: AU0035, a compliant monitor purchase, judged with a shoe run's
        facets declines as ``cart_contradicts_purpose``. Evaluating with no facets is no
        better — the facet-gated checks stand down and it would approve things the real policy
        declines, which is the dangerous direction.

        We cannot establish which policy applies, so this routes to the event's own
        ``uncertainty_policy`` — the same statement the engine makes anywhere else a fact
        cannot be established. Nothing touches this run's ledger, window or audit counters.
        """
        event = envelope["data"]
        authorization = event.get("authorization", {})
        auth_id = str(authorization.get("authorization_id", ""))

        with self.session.lock:
            already = self.session.decided.get(auth_id)
        if already is not None:
            self._replay(auth_id, already)
            return None

        policy = str(event.get("mandate", {}).get("uncertainty_policy") or "ask")
        decision = {"ask": "step_up", "decline": "decline", "approve": "approve"}.get(
            policy, "step_up"
        )
        amount = authorization.get("billing_amount_chf")
        merchant = safe_display(str(authorization.get("merchant", {}).get("merchant_name", "")))
        payload = {
            "authorization_id": auth_id,
            "decision": decision,
            "reason_codes": ["insufficient_evidence"],
            "customer_message": (
                f"Needs your confirmation: CHF {amount} at {merchant}. This purchase belongs to "
                "a different agent session, so the rules you set for it could not be read here."
            )
            if decision == "step_up"
            else (
                f"Declined: CHF {amount} at {merchant}. This purchase belongs to a different "
                "agent session, so the rules you set for it could not be read here."
            ),
            "engine_version": self.engine_version,
        }
        result = self.client.try_submit_decision(auth_id, payload)
        with self.session.lock:
            self.session.decided[auth_id] = payload
        self.audit.append(
            unowned_record(
                auth_id,
                decision,
                run_id=str(envelope.get("run_id") or ""),
                observed_at=datetime.now(UTC),
                upstream=result.as_dict(),
            )
        )
        return None

    def _policy_for(self, event: dict[str, Any]) -> dict[str, Any]:
        """The run's composed policy, **union** the mandate snapshot the event carries.

        Two sources, and both matter:

        * The session policy is the composition of every layer — account ceiling, standing
          preferences, the mandate (specs/customer-settings.md §3). Only the mandate's own
          rules ever reach the platform, so a policy taken from the event alone silently
          drops the layers and enforces less than the customer set. That was a real defect:
          a standing `CHF 1000 / 30 days` ceiling was composed at `start_run`, carried on
          the session, and then never consulted.
        * The event's `mandate.hard_rules` is what the **platform** holds, which can have
          moved since the run began — a `PATCH` from another client, for instance. Dropping
          it would ignore a tightening we were told about.

        Union is safe in both directions because every selection downstream is "tightest
        wins": `binding_cap` per purchase scope, `period_caps` per window. Adding a rule can
        only narrow, never widen, so neither source can loosen the other.

        ``intent_facets`` are not on the wire — the event schema restricts the mandate object
        to the fields the API can represent — so they come from the session alone.
        See specs/policy-ir.md.
        """
        mandate = event.get("mandate", {})
        composed = list(self.session.policy.get("hard_rules", []))
        comparable = {_rule_key(r) for r in composed}
        for rule in mandate.get("hard_rules", []) or []:
            if _rule_key(rule) not in comparable:
                composed.append(rule)

        # The stricter of the two, for the same reason: a platform that hardened the mandate
        # under us is telling us something, and our own layers can only harden it further.
        stated = [
            p
            for p in (
                mandate.get("uncertainty_policy"),
                self.session.policy.get("uncertainty_policy"),
            )
            if p in UNCERTAINTY_ORDER
        ]
        policy: dict[str, Any] = {
            "hard_rules": composed,
            "uncertainty_policy": (
                max(stated, key=lambda p: UNCERTAINTY_ORDER[p]) if stated else "ask"
            ),
            "intent_facets": self.session.policy.get("intent_facets", []),
        }
        # Carried explicitly, and this is the second time a field has been lost here by being
        # forgotten rather than by being wrong: the mandate snapshot on the event cannot
        # express it, so anything the composition added that is not `hard_rules` has to be
        # named. A dropped threshold silently restores the global default.
        if "step_up_threshold" in self.session.policy:
            policy["step_up_threshold"] = self.session.policy["step_up_threshold"]
        return policy

    def _apply(
        self,
        ev: EnrichedEvent,
        record: DecisionRecord,
        payload: dict[str, Any],
        result: UpstreamResult,
    ) -> None:
        """Move our own state only as far as the platform actually accepted.

        A decision the platform refused (408 late, 409 already decided elsewhere) must not
        enter the ledger: the window has to mirror what was really approved, not what we
        intended to approve.
        """
        session = self.session
        with session.lock:
            session.decided[ev.authorization_id] = payload
            session.seen.append(ev)

            if not result.ok:
                log.warning(
                    "%s not accepted: HTTP %s %s",
                    ev.authorization_id,
                    result.status,
                    result.error_code,
                )
                return

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
                    source_authorization_id=str(
                        ev.raw.get("authorization", {}).get("source_authorization_id", "")
                    ),
                    run_id=session.run_id,
                    asked_at=record.decided_at,
                    expires_at=session.expiry(result.body, self.step_up_window_seconds),
                    amount_chf=str(ev.billing_amount_chf),
                    merchant_name=ev.merchant_name,
                    customer_message=record.customer_message,
                    reason_codes=list(record.reason_codes),
                    evidence=[e.as_dict() for e in record.evidence],
                )
        if record.decision is Decision.STEP_UP and session.auto_resolve:
            # Demo aid only. A real step-up waits for the human surface in service/. The record
            # says plainly that no person answered it — it reaches the platform as a resolution.
            self.resolve(
                ev.authorization_id,
                session.auto_resolve,
                "Answered automatically (auto_resolve), not by a person.",
            )

    def _replay(self, auth_id: str, stored: dict[str, Any]) -> None:
        """At-least-once redelivery: resubmit the identical decision, never re-evaluate."""
        session = self.session
        if stored.get("decision") == "step_up":
            # Only `/resolve` settles a step-up. Resubmitting it is refused (409) and is the
            # "second automated decision" the organizers' guide forbids.
            with session.lock:
                session.replays += 1
            log.info("redelivery of stepped-up %s; waiting for the customer", auth_id)
            return
        payload = dict(stored)
        codes = list(payload.get("reason_codes") or [])
        if "idempotent_replay" not in codes:
            codes.append("idempotent_replay")
        payload["reason_codes"] = codes
        result = self.client.try_submit_decision(auth_id, payload)
        with session.lock:
            session.replays += 1
        self.audit.append(
            replay_record(
                auth_id,
                str(payload.get("decision", "")),
                codes,
                observed_at=datetime.now(UTC),
                upstream=result.as_dict(),
            )
        )
        log.info("idempotent replay of %s (HTTP %s)", auth_id, result.status)

    # ------------------------------------------------------------------ step-up

    def resolve(self, authorization_id: str, outcome: str, message: str = "") -> UpstreamResult:
        """Record the cardholder's human answer to a step-up.

        An approved step-up enters approved spend **now**, not when it was asked: while
        pending it contributed nothing to the rolling window (decision-rules.md §2.1).
        """
        result = self.client.try_resolve(authorization_id, outcome, message)
        session = self.session
        with session.lock:
            if result.ok:
                session.ledger.resolve(authorization_id, outcome == "approve")
                step_up = session.step_ups.get(authorization_id)
                if step_up is not None:
                    step_up.resolved = outcome
        self.audit.append(
            resolution_record(
                authorization_id,
                outcome,
                resolved_at=datetime.now(UTC),
                customer_message=message,
                upstream={
                    "submitted": result.status is not None,
                    "http_status": result.status,
                    "error": result.error_code,
                },
            )
        )
        return result


def _rule_key(rule: dict[str, Any]) -> tuple[Any, ...]:
    """A rule's identity for de-duplication: the fields that decide, and nothing else.

    Provenance and confidence are ours and never travel on the wire, so the same rule arrives
    back from the platform looking different. Keying on the decision-bearing fields is what
    stops the composed list growing a copy of itself on every event.
    """
    return tuple(
        rule.get(k) for k in ("field", "operator", "value", "currency", "scope", "period_days")
    )


def _digest(event: dict[str, Any]) -> str:
    from ..domain.evaluator import digest

    return digest(event)


def _ledger_inputs(auth: dict[str, Any]) -> tuple[datetime | None, Any]:
    from ..domain.money import Money

    # Flat on the replica; nested under `authorization` on the real API.
    nested = auth.get("authorization")
    subject: dict[str, Any] = nested if isinstance(nested, dict) else {}
    raw_ts = auth.get("timestamp", subject.get("timestamp"))
    amount = auth.get("billing_amount_chf", subject.get("billing_amount_chf"))
    if not isinstance(raw_ts, str) or amount is None:
        return None, None
    try:
        return datetime.fromisoformat(raw_ts.replace("Z", "+00:00")), Money.from_value(amount)
    except (ValueError, TypeError):
        return None, None
