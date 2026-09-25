"""Offline replica of the organizers' sandbox API.

The real sandbox is private and only reachable with the day-of team key. This replica speaks
the same protocol against the same fixtures, so the whole integration — mandate lifecycle,
long-polling, deadlines, step-up resolution, at-least-once delivery, the event feed — can be
built and rehearsed before the event, and demoed with no network at all.

Where the documented behaviour leaves a detail open, the choice is marked REPLICA-ASSUMPTION.
Those are the places to re-verify against the real API on the day.

**A step-up holds the queue**, as it does on the real platform (measured 2026-09-24,
specs/service-contract.md §5): while a step-up waits on the customer, nothing more is generated
for its run, every poll hands that same request back at once with ``status:
"pending_step_up"``, and the whole team queue waits behind it — other runs included. The
window is ``STEP_UP_WINDOW_SECONDS``; when it closes, the next request that touches the queue
settles the step-up as a decline (``decision_source: "timeout"``, ``step_up_expired``) and the
run moves on. Before this, the replica went straight on to the next order, so every offline
rehearsal ran past step-ups that freeze a live run.

Run:  make sandbox      →  http://127.0.0.1:8099
"""

from __future__ import annotations

import asyncio
import csv
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .fixtures import DataPack, build_event, data_dir

API_VERSION = "0.1.0-replica"
PACK_VERSION = "saw26"
REFERENCE_TABLES = (
    "customers",
    "accounts",
    "cards",
    "merchants",
    "items",
    "fx_rates",
    "scenario_catalogue",
)
DEFAULT_DEADLINE_SECONDS = int(os.environ.get("LEASH_SANDBOX_DEADLINE", "8"))
STEP_UP_WINDOW_SECONDS = int(os.environ.get("LEASH_SANDBOX_STEPUP_WINDOW", "120"))
# Set to 1 to deliver every event twice and prove the engine's idempotency handling.
DUPLICATE_DELIVERY = os.environ.get("LEASH_SANDBOX_DUPLICATE", "0") == "1"
REPLICA_KEY = os.environ.get("LEASH_SANDBOX_KEY", "dev-key")

Decision = Literal["approve", "decline", "step_up"]
UNCERTAINTY_POLICIES = ("ask", "decline", "approve")
_TIGHTENING_ORDER = {"approve": 0, "ask": 1, "decline": 2}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


@dataclass
class Mandate:
    mandate_id: str
    instruction: str
    hard_rules: list[dict[str, Any]]
    uncertainty_policy: str
    guidance: list[str]
    open_questions: list[str]
    status: str = "draft"
    customer_id: str = ""
    card_id: str = ""
    profile_id: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "mandate_id": self.mandate_id,
            "status": self.status,
            "customer_id": self.customer_id,
            "card_id": self.card_id,
            "instruction": self.instruction,
            "hard_rules": [dict(r) for r in self.hard_rules],
            "uncertainty_policy": self.uncertainty_policy,
            "profile_id": self.profile_id,
        }

    def resource(self) -> dict[str, Any]:
        return {**self.snapshot(), "guidance": self.guidance, "open_questions": self.open_questions}


@dataclass
class Authorization:
    authorization_id: str
    source_authorization_id: str
    run_id: str
    timestamp: datetime
    billing_amount_chf: float
    merchant_id: str
    status: str = "awaiting_decision"  # → approved | declined | pending_step_up
    decision: str | None = None  # the team's decision; stays "step_up" once one is resolved
    reason_codes: list[str] = field(default_factory=list)
    customer_message: str = ""
    deadline_at: datetime | None = None
    resolved_at: datetime | None = None
    step_up_expires_at: datetime | None = None
    decision_source: str = "team"  # "timeout" when the platform settled it itself


@dataclass
class Run:
    run_id: str
    scenario_id: str
    mandate: dict[str, Any]  # snapshot taken at run start
    queue: list[dict[str, str]]
    cursor: int = 0
    delivered: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_redelivery: list[str] = field(default_factory=list)
    rejected: int = 0  # orders never queued because the mandate was revoked


@dataclass
class TeamState:
    mandates: dict[str, Mandate] = field(default_factory=dict)
    runs: dict[str, Run] = field(default_factory=dict)
    authorizations: dict[str, Authorization] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0

    def emit(self, type_: str, **payload: Any) -> dict[str, Any]:
        self.seq += 1
        evt = {"event_id": self.seq, "type": type_, "occurred_at": _iso(_now()), **payload}
        self.events.append(evt)
        return evt


PACK = DataPack.load()
STATE = TeamState()
router = APIRouter()


def _auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise _error(401, "unauthorized", "Send the team key as 'Authorization: Bearer <key>'.")


def _period_days(hard_rules: list[dict[str, Any]]) -> int | None:
    """REPLICA-ASSUMPTION: the platform's period counter uses the first period-scoped rule.

    Deliberately NOT `domain.policy.period_window_days`, which takes the tightest rule
    (decision-rules.md §9). This models the platform, and the engine reads its own ledger
    rather than the `approved_spend_in_period_chf` computed here — so a divergence shows up
    as a diff to investigate on the day instead of being papered over.
    """
    for rule in hard_rules:
        if rule.get("scope") == "period" and rule.get("period_days"):
            return int(rule["period_days"])
    return None


def _approved_spend(run: Run, upto: datetime) -> float | None:
    """Recompute from decisions actually finalized in this run (context_basis).

    Final approvals, whoever gave them — a step-up the customer approved counts from the
    moment they did, as the platform's own guidance says it must.
    """
    days = _period_days(run.mandate.get("hard_rules", []))
    if days is None:
        return None
    start = upto - timedelta(days=days)
    total = 0.0
    for auth in STATE.authorizations.values():
        if auth.run_id != run.run_id or auth.status != "approved":
            continue
        if start <= auth.timestamp < upto:
            total += auth.billing_amount_chf
    return round(total, 2)


def _recent(run: Run, upto: datetime, minutes: int = 10) -> list[dict[str, Any]]:
    start = upto - timedelta(minutes=minutes)
    out = []
    for auth in STATE.authorizations.values():
        if auth.run_id != run.run_id or not (start <= auth.timestamp < upto):
            continue
        # The outcome, not the team's decision: a step-up is pending until it is resolved.
        status = {"approved": "approved", "declined": "declined"}.get(auth.status, "pending")
        out.append(
            {
                "authorization_id": auth.authorization_id,
                "timestamp": _iso(auth.timestamp),
                "merchant_id": auth.merchant_id,
                "billing_amount_chf": auth.billing_amount_chf,
                "status": status,
            }
        )
    return sorted(out, key=lambda r: r["timestamp"])


PENDING_STEP_UP = "pending_step_up"
SETTLED = ("approved", "declined")


def _settle_expired_step_ups(now: datetime) -> None:
    """Close every step-up whose window has run out, the way the platform does: lazily.

    It is settled by the next request that touches the queue, and recorded as the platform's
    own decline — measured live, the step-up raised at 11:13:55 was settled at 11:16:48, by
    the first poll after its 120 seconds.
    """
    for auth in STATE.authorizations.values():
        if (
            auth.status == PENDING_STEP_UP
            and auth.step_up_expires_at is not None
            and now >= auth.step_up_expires_at
        ):
            auth.status = "declined"
            auth.decision_source = "timeout"
            auth.reason_codes = ["step_up_expired"]
            auth.customer_message = "The confirmation window expired."
            auth.resolved_at = now
            STATE.emit(
                "authorization.decision",
                run_id=auth.run_id,
                authorization_id=auth.authorization_id,
                status="declined",
                decision="decline",
                reason_codes=["step_up_expired"],
                decision_source="timeout",
            )


def _held_step_up() -> Authorization | None:
    """The step-up the team queue is waiting behind, if any — the oldest one asked.

    REPLICA-ASSUMPTION: oldest first. Measured live, a second run's first order stayed queued,
    undelivered, for as long as another run's step-up was pending; which of two pending
    step-ups the platform hands back first has not been observed.
    """
    pending = [
        a
        for a in STATE.authorizations.values()
        if a.status == PENDING_STEP_UP and a.step_up_expires_at is not None
    ]
    return min(pending, key=lambda a: a.step_up_expires_at or _now()) if pending else None


# --------------------------------------------------------------------------- endpoints


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "saw26-sandbox-replica",
        "api_version": API_VERSION,
        "pack_version": PACK_VERSION,
    }


@router.get("/v1/bootstrap")
async def bootstrap(authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization)
    return {
        "team": {"team_id": "TEAM_LOCAL", "name": "offline-replica"},
        "api_version": API_VERSION,
        "pack_version": PACK_VERSION,
        "scenarios": [
            {
                "scenario_id": s["scenario_id"],
                "scenario_name": s["scenario_name"],
                "event_count": int(s["event_count"]),
                "cardholder_instruction": s["cardholder_instruction"],
            }
            for s in PACK.scenarios.values()
        ],
        "limits": {
            "decision_deadline_seconds": DEFAULT_DEADLINE_SECONDS,
            "step_up_window_seconds": STEP_UP_WINDOW_SECONDS,
            "max_long_poll_seconds": 25,
        },
        "features": {"reset_enabled": True, "duplicate_delivery": DUPLICATE_DELIVERY},
    }


@router.get("/v1/reference-data")
async def reference_data(authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization)
    # The shape the real API returned on 2026-09-24: the pack's tables by name, every value a
    # string except the catalogue's `event_count`, and the history behind its own path.
    # `leash.adapters.livepack` writes exactly this back to disk, so the replica serving the
    # repo pack in this shape is what lets the tests prove that round trip.
    tables: dict[str, list[dict[str, Any]]] = {}
    for name in REFERENCE_TABLES:
        with (data_dir() / f"{name}.csv").open(newline="", encoding="utf-8") as fh:
            tables[name] = list(csv.DictReader(fh))
    for row in tables["scenario_catalogue"]:
        row["event_count"] = int(row["event_count"])
    with (data_dir() / "authorization_history.csv").open(encoding="utf-8") as fh:
        history_rows = sum(1 for _ in fh) - 1
    return {
        "type": "reference_data",
        "pack_version": PACK_VERSION,
        "classification": "SYNTHETIC TEST DATA",
        "tables": tables,
        "history": {
            "path": "/v1/reference-data/authorization-history.csv",
            "rows": history_rows,
            "format": "csv",
        },
        "runtime": {
            "scenario_ids": list(PACK.scenarios),
            "purchase_attempts": "Delivered one at a time by scenario runs.",
            "purchase_attempt_items": "Delivered inside authorization events.",
        },
    }


@router.get("/v1/reference-data/authorization-history.csv")
async def history_csv(authorization: str | None = Header(None)) -> FileResponse:
    _auth(authorization)
    return FileResponse(data_dir() / "authorization_history.csv", media_type="text/csv")


@router.post("/v1/mandates", status_code=201)
async def create_mandate(
    body: dict[str, Any], authorization: str | None = Header(None)
) -> dict[str, Any]:
    _auth(authorization)
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        raise _error(422, "invalid_instruction", "instruction must be non-empty")
    policy = body.get("uncertainty_policy", "ask")
    if policy not in UNCERTAINTY_POLICIES:
        raise _error(422, "invalid_uncertainty_policy", f"must be one of {UNCERTAINTY_POLICIES}")

    draft_id = f"TM{uuid.uuid4().hex[:12].upper()}"
    STATE.mandates[draft_id] = Mandate(
        mandate_id=draft_id,
        instruction=body["instruction"],
        hard_rules=list(body.get("hard_rules") or []),
        uncertainty_policy=policy,
        guidance=list(body.get("guidance") or []),
        open_questions=list(body.get("open_questions") or []),
    )
    STATE.emit("mandate.drafted", mandate_id=draft_id)
    return {"draft_id": draft_id, **STATE.mandates[draft_id].resource()}


@router.post("/v1/mandates/{draft_id}/confirm")
async def confirm_mandate(
    draft_id: str, body: dict[str, Any], authorization: str | None = Header(None)
) -> dict[str, Any]:
    _auth(authorization)
    mandate = STATE.mandates.get(draft_id)
    if mandate is None:
        raise _error(404, "not_found", f"no mandate {draft_id}")
    if not body.get("confirmed"):
        raise _error(422, "not_confirmed", 'send {"confirmed": true}')
    if mandate.status != "draft":
        raise _error(409, "already_confirmed", f"mandate is {mandate.status}")
    mandate.status = "active"
    STATE.emit("mandate.confirmed", mandate_id=mandate.mandate_id)
    return {"mandate_id": mandate.mandate_id, **mandate.resource()}


@router.get("/v1/mandates/{mandate_id}")
async def get_mandate(mandate_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization)
    mandate = STATE.mandates.get(mandate_id)
    if mandate is None:
        raise _error(404, "not_found", f"no mandate {mandate_id}")
    return mandate.resource()


@router.patch("/v1/mandates/{mandate_id}")
async def patch_mandate(
    mandate_id: str, body: dict[str, Any], authorization: str | None = Header(None)
) -> dict[str, Any]:
    """Tighten-only. Rules may be added, never removed; uncertainty may only move toward decline."""
    _auth(authorization)
    mandate = STATE.mandates.get(mandate_id)
    if mandate is None:
        raise _error(404, "not_found", f"no mandate {mandate_id}")
    if mandate.status != "active":
        raise _error(409, "not_active", f"mandate is {mandate.status}")

    if "hard_rules" in body:
        incoming = list(body["hard_rules"] or [])
        for existing in mandate.hard_rules:
            if existing not in incoming:
                raise _error(
                    422, "rules_not_preserved", "hard_rules must preserve every existing rule"
                )
        mandate.hard_rules = incoming
    if "uncertainty_policy" in body:
        new = body["uncertainty_policy"]
        if new not in UNCERTAINTY_POLICIES:
            raise _error(
                422, "invalid_uncertainty_policy", f"must be one of {UNCERTAINTY_POLICIES}"
            )
        if _TIGHTENING_ORDER[new] < _TIGHTENING_ORDER[mandate.uncertainty_policy]:
            raise _error(422, "policy_loosened", "uncertainty_policy may only be tightened")
        mandate.uncertainty_policy = new
    if "guidance" in body:
        mandate.guidance = list(body["guidance"] or [])
    if "open_questions" in body:
        mandate.open_questions = list(body["open_questions"] or [])

    STATE.emit("mandate.patched", mandate_id=mandate.mandate_id)
    return mandate.resource()


@router.delete("/v1/mandates/{mandate_id}")
async def revoke_mandate(
    mandate_id: str, authorization: str | None = Header(None)
) -> dict[str, Any]:
    _auth(authorization)
    mandate = STATE.mandates.get(mandate_id)
    if mandate is None:
        raise _error(404, "not_found", f"no mandate {mandate_id}")
    mandate.status = "revoked"
    # REPLICA-ASSUMPTION: "The platform rejects revoked or expired mandates ... before queueing
    # an actionable request" (technical_details.md §8) applies to a run already under way: its
    # remaining orders are never queued. What happens to one already waiting on the customer
    # is not specified; here it stays answerable, and expires like any other.
    for run in STATE.runs.values():
        if run.mandate.get("mandate_id") == mandate_id and run.cursor < len(run.queue):
            run.rejected += len(run.queue) - run.cursor
            run.cursor = len(run.queue)
    STATE.emit("mandate.revoked", mandate_id=mandate.mandate_id)
    return mandate.resource()


@router.post("/v1/scenario-runs", status_code=201)
async def start_run(
    body: dict[str, Any], authorization: str | None = Header(None)
) -> dict[str, Any]:
    _auth(authorization)
    scenario_id = body.get("scenario_id", "")
    mandate = STATE.mandates.get(body.get("mandate_id", ""))
    scenario = PACK.scenarios.get(scenario_id)
    if scenario is None:
        raise _error(404, "unknown_scenario", f"no scenario {scenario_id}")
    if mandate is None:
        raise _error(404, "not_found", "unknown mandate_id")
    if mandate.status != "active":
        raise _error(409, "mandate_not_active", f"mandate is {mandate.status}; confirm it first")
    # The sandbox rejects a changed or empty instruction: intent may be structured, not replaced.
    if mandate.instruction != scenario["cardholder_instruction"]:
        raise _error(
            422,
            "instruction_mismatch",
            "instruction must exactly match the scenario's cardholder_instruction",
        )

    authority = next(
        a for a in PACK.authorities.values() if a["authority_id"] == _authority_for(scenario_id)
    )
    run_id = f"RUN_{uuid.uuid4().hex[:10].upper()}"
    mandate.customer_id = authority["customer_id"]
    mandate.card_id = authority["card_id"]
    mandate.profile_id = f"PROFILE_{authority['authority_id']}"

    run = Run(
        run_id=run_id,
        scenario_id=scenario_id,
        mandate=mandate.snapshot(),  # snapshot at start; later patches affect later runs
        queue=list(PACK.scenario_attempts(scenario_id)),
    )
    STATE.runs[run_id] = run
    STATE.emit("run.started", run_id=run_id, scenario_id=scenario_id, mandate_id=mandate.mandate_id)
    return {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "mandate_id": mandate.mandate_id,
        "profile": {
            "customer_id": mandate.customer_id,
            "card_id": mandate.card_id,
            "profile_id": mandate.profile_id,
        },
        "counters": {"total": len(run.queue), "delivered": 0, "decided": 0},
    }


def _authority_for(scenario_id: str) -> str:
    mapping = {
        "SCEN0000": "AUTH0001",
        "SCEN0001": "AUTH0002",
        "SCEN0002": "AUTH0003",
        "SCEN0003": "AUTH0004",
        "SCEN0004": "AUTH0005",
    }
    return mapping[scenario_id]


@router.get("/v1/scenario-runs/{run_id}")
async def get_run(run_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization)
    run = STATE.runs.get(run_id)
    if run is None:
        raise _error(404, "not_found", f"no run {run_id}")
    _settle_expired_step_ups(_now())
    delivered = [a for a in STATE.authorizations.values() if a.run_id == run_id]
    decided = sum(1 for a in delivered if a.decision)
    # Complete once nothing is left to deliver and every delivered order is settled — a
    # step-up still waiting on the customer keeps the run open, as it does on the platform.
    complete = run.cursor >= len(run.queue) and all(a.status in SETTLED for a in delivered)
    return {
        "run_id": run_id,
        "scenario_id": run.scenario_id,
        "mandate_id": run.mandate["mandate_id"],
        "status": "completed" if complete else "running",
        "counters": {
            "total": len(run.queue),
            "delivered": len(delivered),  # not the cursor: a revoke moves that past the rest
            "decided": decided,
            "rejected": run.rejected,
        },
        "complete": complete,
    }


@router.get("/v1/decision-requests/next")
async def next_request(
    wait: int = Query(25, ge=0, le=25), authorization: str | None = Header(None)
) -> Any:
    """Long-poll: 200 with an envelope, or 204 when nothing is actionable before the wait."""
    _auth(authorization)
    deadline = _now() + timedelta(seconds=wait)
    while True:
        envelope = _dequeue()
        if envelope is not None:
            return envelope
        if _now() >= deadline:
            # A 204 carries no body at all. `JSONResponse(content=None)` writes the four
            # bytes `null` while the ASGI server has already committed to Content-Length: 0,
            # which uvicorn rejects — the client still sees the 204, so it looks like the
            # replica works while the server log fills with tracebacks on every idle poll.
            return Response(status_code=204)
        await asyncio.sleep(0.05)


def _dequeue() -> dict[str, Any] | None:
    _settle_expired_step_ups(_now())
    held = _held_step_up()
    if held is not None:
        # The whole team queue waits behind a step-up, and the platform says so by handing the
        # same request back, marked, instead of long-polling — so no new order is generated,
        # for this run or any other, until the customer answers or the window closes.
        return {
            **STATE.runs[held.run_id].delivered[held.authorization_id],
            "status": PENDING_STEP_UP,
        }
    for run in STATE.runs.values():
        # At-least-once delivery: replay an already-delivered event to exercise idempotency.
        if run.pending_redelivery:
            auth_id = run.pending_redelivery.pop(0)
            return run.delivered[auth_id]
        # Fixture order, one outstanding decision at a time.
        outstanding = [
            a
            for a in STATE.authorizations.values()
            if a.run_id == run.run_id and a.decision is None
        ]
        if outstanding or run.cursor >= len(run.queue):
            continue

        attempt = run.queue[run.cursor]
        run.cursor += 1
        ts = datetime.fromisoformat(attempt["timestamp"].replace("Z", "+00:00"))
        event = build_event(
            PACK,
            attempt,
            run_id=run.run_id,
            mandate=run.mandate,
            approved_spend_in_period_chf=_approved_spend(run, ts),
            recent_authorizations=_recent(run, ts),
            deadline_seconds=DEFAULT_DEADLINE_SECONDS,
            request_seq=run.cursor,
        )
        auth_id = event["authorization"]["authorization_id"]
        STATE.authorizations[auth_id] = Authorization(
            authorization_id=auth_id,
            source_authorization_id=attempt["authorization_id"],
            run_id=run.run_id,
            timestamp=ts,
            billing_amount_chf=float(attempt["billing_amount_chf"]),
            merchant_id=attempt["merchant_id"],
            deadline_at=datetime.fromisoformat(event["deadline_at"].replace("Z", "+00:00")),
        )
        envelope = {
            "event_id": STATE.seq + 1,
            "type": "authorization.request",
            "run_id": run.run_id,
            "authorization_id": auth_id,
            "status": "awaiting_decision",
            "occurred_at": _iso(_now()),
            "data": event,
        }
        STATE.emit("authorization.request", run_id=run.run_id, authorization_id=auth_id)
        run.delivered[auth_id] = envelope
        if DUPLICATE_DELIVERY:
            run.pending_redelivery.append(auth_id)
        return envelope
    return None


@router.post("/v1/authorizations/{authorization_id}/decision")
async def submit_decision(
    authorization_id: str, body: dict[str, Any], authorization: str | None = Header(None)
) -> dict[str, Any]:
    _auth(authorization)
    auth = STATE.authorizations.get(authorization_id)
    if auth is None:
        raise _error(404, "not_found", f"no authorization {authorization_id}")
    decision = body.get("decision")
    if decision not in ("approve", "decline", "step_up"):
        raise _error(422, "invalid_decision", "decision must be approve, decline or step_up")
    _settle_expired_step_ups(_now())
    if auth.status == PENDING_STEP_UP:
        # The real API's code and wording, measured 2026-09-24.
        raise _error(
            409,
            "step_up_resolution_required",
            "Resolve the pending step-up through the /resolve endpoint",
        )
    if auth.decision is not None:
        # At-least-once delivery: a repeat of the same decision is accepted idempotently —
        # except a step-up, which only /resolve ever settled.
        if auth.decision == decision and decision != "step_up":
            return {
                "authorization_id": authorization_id,
                "status": auth.status,
                "decision": auth.decision,
                "idempotent": True,
            }
        raise _error(409, "authorization_finalized", "Authorization is already finalized")

    # A missing, invalid, or late decision does not become an approval.
    if auth.deadline_at and _now() > auth.deadline_at:
        auth.decision, auth.status = "decline", "declined"
        auth.reason_codes = ["platform_deadline_exceeded"]
        STATE.emit(
            "authorization.declined", authorization_id=authorization_id, reason="deadline_exceeded"
        )
        raise _error(
            408, "deadline_exceeded", "decision arrived after deadline_at; declined by the platform"
        )

    auth.decision = decision
    auth.reason_codes = list(body.get("reason_codes") or [])
    auth.customer_message = body.get("customer_message", "")
    auth.status = {"approve": "approved", "decline": "declined", "step_up": PENDING_STEP_UP}[
        decision
    ]
    if decision == "step_up":
        auth.step_up_expires_at = _now() + timedelta(seconds=STEP_UP_WINDOW_SECONDS)
    STATE.emit(f"authorization.{auth.status}", authorization_id=authorization_id, decision=decision)
    return {
        "authorization_id": authorization_id,
        "status": auth.status,
        "decision": decision,
        "step_up_expires_at": _iso(auth.step_up_expires_at) if auth.step_up_expires_at else None,
    }


@router.post("/v1/authorizations/{authorization_id}/resolve")
async def resolve(
    authorization_id: str, body: dict[str, Any], authorization: str | None = Header(None)
) -> dict[str, Any]:
    """Record the cardholder's human confirmation. Final resolution is approve or decline only."""
    _auth(authorization)
    auth = STATE.authorizations.get(authorization_id)
    if auth is None:
        raise _error(404, "not_found", f"no authorization {authorization_id}")
    if auth.decision != "step_up":
        raise _error(409, "not_stepped_up", "only a stepped-up authorization can be resolved")
    _settle_expired_step_ups(_now())
    if auth.status != PENDING_STEP_UP:
        # Answered already, or the window closed and the platform declined it. The real
        # API's code and wording, measured 2026-09-24.
        raise _error(409, "authorization_not_pending", "Authorization is not awaiting step-up")
    outcome = body.get("decision")
    if outcome not in ("approve", "decline"):
        raise _error(422, "invalid_resolution", "resolution must be approve or decline")

    auth.status = "approved" if outcome == "approve" else "declined"
    auth.resolved_at = _now()
    auth.customer_message = body.get("customer_message", auth.customer_message)
    STATE.emit("authorization.resolved", authorization_id=authorization_id, outcome=outcome)
    return {"authorization_id": authorization_id, "status": auth.status, "resolved_by": "customer"}


@router.get("/v1/authorizations")
async def list_authorizations(authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization)
    return {
        "authorizations": [
            {
                "authorization_id": a.authorization_id,
                "source_authorization_id": a.source_authorization_id,
                "run_id": a.run_id,
                "status": a.status,
                "decision": a.decision,
                "reason_codes": a.reason_codes,
                "customer_message": a.customer_message,
                "billing_amount_chf": a.billing_amount_chf,
                "timestamp": _iso(a.timestamp),
            }
            for a in STATE.authorizations.values()
        ]
    }


@router.get("/v1/events")
async def events(
    since: int = Query(0, ge=0), authorization: str | None = Header(None)
) -> dict[str, Any]:
    _auth(authorization)
    batch = [e for e in STATE.events if e["event_id"] > since]
    return {"events": batch, "next_cursor": batch[-1]["event_id"] if batch else since}


@router.post("/v1/team/reset")
async def reset(authorization: str | None = Header(None)) -> dict[str, str]:
    """Clear local state so a run can be repeated from a clean context. Disabled during judging."""
    _auth(authorization)
    STATE.mandates.clear()
    STATE.runs.clear()
    STATE.authorizations.clear()
    STATE.events.clear()
    STATE.seq = 0
    return {"status": "reset"}


app = FastAPI(title="Agent on a Leash — offline sandbox replica", version=API_VERSION)
app.include_router(router)


@app.exception_handler(HTTPException)
async def error_envelope(_: Request, exc: HTTPException) -> JSONResponse:
    detail = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"error": {"code": "error", "message": str(exc.detail)}}
    )
    return JSONResponse(status_code=exc.status_code, content=detail)
