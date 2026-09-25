"""The decision service the UI talks to. Implements specs/service-contract.md.

Thin by design (AGENTS.md §2): every route validates, delegates to ``runtime.Supervisor``,
and renders. No decision logic lives here — ``domain.evaluator`` is still the only place a
verdict is produced.

The UI never holds the team API key and never calls the organizers' sandbox. This process
owns the key, the Policy IR, the rolling-window ledger and the audit trail, so run state
exists in exactly one place.

    make serve        → :8000 against the offline replica
    make serve-live   → :8000 against the organizers' sandbox (needs TEAM_API_KEY)
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from ..connector.http import router as connector_router
from ..domain.deadline import RESERVE_MS
from ..domain.evaluator import CHECKS, CONCERN_WEIGHTS, STEP_UP_THRESHOLD
from ..domain.settings import catalogue
from ..runtime.supervisor import ENGINE_VERSION, Supervisor, SupervisorError
from .deps import supervisor
from .schemas import (
    AmendRequest,
    CompileRequest,
    ConfirmRequest,
    DecideRequest,
    MandateRequest,
    PatchMandateRequest,
    PreferencesRequest,
    ResolveRequest,
    RunRequest,
)

log = logging.getLogger("leash.service")

app = FastAPI(
    title="Wallet Control Layer — decision service",
    version=ENGINE_VERSION,
    description=(
        "The control layer that decides whether an AI shopping agent may spend a customer's "
        "money. Contract: specs/service-contract.md"
    ),
)

# The UI runs on another port in dev; without this every call is a browser CORS error at the
# worst possible moment. Nothing here is reachable outside the laptop.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_supervisor() -> Supervisor:
    """The one composition point. Tests replace it through ``app.dependency_overrides``."""
    return supervisor()


def sv() -> Supervisor:
    override = app.dependency_overrides.get(get_supervisor)
    return (override or get_supervisor)()


# ------------------------------------------------------------------ errors


@app.exception_handler(SupervisorError)
async def supervisor_error(_: Request, exc: SupervisorError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    detail: Any = exc.detail
    if not (isinstance(detail, dict) and "error" in detail):
        detail = {"error": {"code": f"http_{exc.status_code}", "message": str(exc.detail)}}
    return JSONResponse(status_code=exc.status_code, content=detail)


# ------------------------------------------------------------------ §1 meta


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, Any]:
    """Connection badge for the UI: are we up, and which sandbox are we pointed at?"""
    upstream = sv().health()
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "upstream": upstream,
        "checks_registered": len(CHECKS),
        "audit_records": len(sv().audit),
    }


@app.get("/v1/config", tags=["meta"])
def config() -> dict[str, Any]:
    """The decision configuration, for a 'how does it work' panel."""
    return {
        "engine_version": ENGINE_VERSION,
        "checks": [getattr(c, "__name__", "?").removeprefix("check_") for c in CHECKS],
        "concern_weights": CONCERN_WEIGHTS,
        "step_up_threshold": STEP_UP_THRESHOLD,
        "deadline_reserve_ms": RESERVE_MS,
        "audit_path": str(sv().audit.path),
        # The editable-settings catalogue. Every entry names the check that reads it, so the
        # UI cannot offer a control that enforces nothing — leash-demo's `check.mjs` asserts
        # its own table against this one. specs/customer-settings.md §7.
        "settings": catalogue(),
        "scenarios": [
            {
                "scenario_id": s["scenario_id"],
                "scenario_name": s["scenario_name"],
                "event_count": int(s["event_count"]),
                "cardholder_instruction": s["cardholder_instruction"],
            }
            for s in sv().pack.scenarios.values()
        ],
    }


@app.get("/v1/scenarios", tags=["meta"])
def scenarios(mode: Literal["auto", "baseline", "llm"] = "baseline") -> dict[str, Any]:
    """The scenario list the UI boots from, in the shape its panels render.

    Separate from `/v1/config` because the UI needs it on every boot and `/v1/config` carries
    the whole decision configuration beside it. Includes the **compiled IR** for each
    scenario's own instruction: the opening beat of the demo is the cardholder's sentence with
    every span that became a rule highlighted, and without `ir` there is nothing to highlight.

    `mode` is the compiler, as on `/v1/mandates/compile`. The default stays `baseline`: it is
    instant and deterministic. The leash demo asks for `auto`, because the live API's
    instructions carry wording the baseline was never written for — "CHF 250 in any 7-day
    window" became a per-order cap. Each instruction is compiled once per mode and cached
    (`Supervisor.scenario_irs`), so the IR a customer reviewed is the IR enforced.
    """
    sup = sv()
    irs = sup.scenario_irs(mode)
    out = []
    for s in sup.pack.scenarios.values():
        instruction = s["cardholder_instruction"]
        ir = irs[s["scenario_id"]]
        period = next(
            (r for r in ir.get("rules", []) if r.get("scope") == "period"),
            None,
        )
        out.append(
            {
                "id": s["scenario_id"],
                "name": s["scenario_name"],
                "instruction": instruction,
                "control_question": s.get("control_question", ""),
                "theme": s.get("control_theme", ""),
                "period_days": int(period["period_days"]) if period else None,
                "event_count": int(s["event_count"]),
                "ir": ir,
            }
        )
    return {"scenarios": out}


@app.get("/v1/bootstrap", tags=["meta"])
def bootstrap(refresh: bool = False) -> dict[str, Any]:
    """The platform's own limits and features, proxied. Never hard-code these."""
    return sv().bootstrap(refresh=refresh)


# ------------------------------------------------------------------ §2 mandates


@app.post("/v1/mandates/compile", tags=["mandates"])
def compile_mandate(body: CompileRequest) -> dict[str, Any]:
    """The review screen. Compiles the instruction, submits nothing, mutates nothing."""
    return sv().compile_only(body.instruction, mode=body.mode)


@app.post("/v1/mandates", status_code=201, tags=["mandates"])
def create_mandate(body: MandateRequest) -> dict[str, Any]:
    return sv().create_mandate(
        body.instruction, ir=body.ir, uncertainty_policy=body.uncertainty_policy
    )


@app.post("/v1/mandates/{draft_id}/confirm", tags=["mandates"])
def confirm_mandate(draft_id: str, body: ConfirmRequest) -> dict[str, Any]:
    """The customer's consent. Nothing is enforceable until this call."""
    if not body.confirmed:
        raise SupervisorError(422, "not_confirmed", 'send {"confirmed": true}')
    return sv().confirm_mandate(draft_id)


@app.get("/v1/mandates", tags=["mandates"])
def list_mandates() -> dict[str, Any]:
    return {"mandates": sv().list_mandates()}


@app.get("/v1/mandates/{mandate_id}", tags=["mandates"])
def get_mandate(mandate_id: str) -> dict[str, Any]:
    """Includes `guidance` and `open_questions`, which live events do not carry."""
    return sv().get_mandate(mandate_id)


@app.patch("/v1/mandates/{mandate_id}", tags=["mandates"])
def patch_mandate(mandate_id: str, body: PatchMandateRequest) -> dict[str, Any]:
    """Tighten only: rules may be added, never removed; uncertainty may only move to decline."""
    return sv().patch_mandate(mandate_id, body.model_dump(exclude_none=True))


@app.delete("/v1/mandates/{mandate_id}", tags=["mandates"])
def revoke_mandate(mandate_id: str) -> dict[str, Any]:
    """Revoke. The platform then rejects new runs before any request reaches us."""
    return sv().revoke_mandate(mandate_id)


@app.post("/v1/mandates/{mandate_id}/amend", tags=["mandates"])
def amend_mandate(mandate_id: str, body: AmendRequest) -> dict[str, Any]:
    """The one entry point for an edit — specs/customer-settings.md §6.

    The caller does not say whether this narrows or widens; the engine decides. A narrowing
    is applied immediately and add-only. A widening returns a **draft** and applies nothing
    until it is confirmed, because widening what an agent may do is a new consent moment and
    not a patch. A contradiction is refused with both sides named.
    """
    return sv().amend_mandate(mandate_id, body.settings)


# ------------------------------------------------------------------ §2b preferences


@app.get("/v1/preferences", tags=["preferences"])
def get_preferences() -> dict[str, Any]:
    """The standing layer: the customer, rather than one errand."""
    return sv().preferences()


@app.put("/v1/preferences", tags=["preferences"])
def put_preferences(body: PreferencesRequest) -> dict[str, Any]:
    """Replace the standing layer. Refuses every setting no check reads, and says which."""
    return sv().set_preferences(body.model_dump())


@app.get("/v1/preferences/candidates", tags=["preferences"])
def preference_candidates(
    scenario_id: str = Query(...),
    mode: str | None = Query(
        "baseline",
        description=(
            "Which compiler reads the profile prose. `baseline` (default) is free and "
            "offline; `auto` lets the model read it. The UI asks on every boot, so the "
            "default is deliberately the cheap one."
        ),
    ),
) -> dict[str, Any]:
    """What the cardholder's profile proposes — proposals a human accepts, never rules."""
    return sv().preference_candidates(scenario_id, mode=mode)


# ------------------------------------------------------------------ §3 runs


@app.post("/v1/runs", status_code=201, tags=["runs"])
def start_run(body: RunRequest) -> dict[str, Any]:
    """Start a scenario and the loop that decides it. Returns immediately."""
    session = sv().start_run(
        body.scenario_id, mandate_id=body.mandate_id, auto_resolve=body.auto_resolve
    )
    return session.as_dict(include_decisions=False)


@app.get("/v1/runs", tags=["runs"])
def list_runs() -> dict[str, Any]:
    return {"runs": sv().list_runs()}


@app.get("/v1/runs/{run_id}", tags=["runs"])
def get_run(run_id: str) -> dict[str, Any]:
    """The UI's main polling endpoint. 1 s is plenty."""
    return sv().session(run_id).as_dict()


@app.post("/v1/runs/{run_id}/stop", tags=["runs"])
def stop_run(run_id: str) -> dict[str, Any]:
    return sv().stop_run(run_id).as_dict(include_decisions=False)


# ------------------------------------------------------------------ §4 decisions


@app.post("/v1/decide", tags=["decisions"])
def decide(body: DecideRequest) -> dict[str, Any]:
    """Evaluate one authorization.request and submit nothing — the what-if endpoint."""
    return sv().decide(body.event, policy=body.policy, run_id=body.run_id)


@app.get("/v1/decisions", tags=["decisions"])
def decisions(run_id: str | None = None, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"decisions": sv().decisions(run_id, limit)}


@app.get("/v1/audit", tags=["decisions"])
def audit_tail(limit: int = Query(50, ge=1, le=500), type: str | None = None) -> dict[str, Any]:
    """The tail of the append-only trail, newest first."""
    return {
        "path": str(sv().audit.path),
        "records": len(sv().audit),
        "malformed_lines": sv().audit.malformed,
        "audit": sv().audit.tail(limit, type_=type),
    }


@app.get("/v1/audit/{authorization_id}", tags=["decisions"])
def audit_record(authorization_id: str) -> dict[str, Any]:
    """The decision, with any later resolution and replay lines folded in."""
    record = sv().audit.get(authorization_id)
    if record is None:
        raise SupervisorError(404, "not_found", f"no audit record for {authorization_id}")
    return record


@app.get("/v1/audit/{authorization_id}/raw", response_class=PlainTextResponse, tags=["decisions"])
def audit_raw(authorization_id: str) -> str:
    """The untouched JSONL lines — the 'show me the append-only trail' moment."""
    return sv().audit.raw_lines(authorization_id)


# ------------------------------------------------------------------ §5 step-ups


@app.get("/v1/step-ups", tags=["step-up"])
def step_ups(include_resolved: bool = False) -> dict[str, Any]:
    """Everything waiting on the customer — the notification list."""
    return {"step_ups": sv().step_ups(include_resolved=include_resolved)}


@app.post("/v1/step-ups/{authorization_id}/resolve", tags=["step-up"])
def resolve_step_up(authorization_id: str, body: ResolveRequest) -> dict[str, Any]:
    """The cardholder's answer. An approved step-up enters approved spend now, not earlier."""
    return sv().resolve_step_up(authorization_id, body.decision, body.customer_message)


# ------------------------------------------------------------------ §6 the connector
#
# The agent-facing surface: MCP over HTTP at /mcp, authenticated with the tokens the Payment
# App issues when a cardholder connects an agent. Everything it does goes through the same
# Supervisor as the routes above, so an agent errand's step-ups are listed by /v1/step-ups and
# answered by /v1/step-ups/{id}/resolve — the phone needs no second API. specs/connector.md.
app.include_router(connector_router(sv))
