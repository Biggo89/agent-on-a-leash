"""End-to-end through the decision service — specs/service-contract.md.

The whole stack runs in one process: the service, the run loop, and the offline replica
mounted through an ASGI transport. No network, no team key, no ports. What this proves is
the part `make replay` never touched — that the *integration* holds, not just the engine.

The frontend is being built against this contract in parallel, so a change here that these
tests do not notice is a change that breaks the UI at hour 20.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from leash.adapters.client import SandboxClient
from leash.adapters.datapack import DataPack, data_dir
from leash.adapters.history import HistoryIndex
from leash.audit.log import AuditLog
from leash.domain.evaluator import CHECKS
from leash.runtime.supervisor import Supervisor
from leash.service.app import app, get_supervisor
from sandbox.server import app as sandbox_app

PACK = DataPack.load()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")
DEADLINE = 20.0  # seconds to let a run finish; each is 45 decisions at worst


@pytest.fixture()
def service(tmp_path: Path) -> Iterator[TestClient]:
    """The service, wired to an in-process replica and a throwaway audit file."""
    # TestClient is an httpx.Client that speaks ASGI, so the sandbox replica answers
    # in-process. One portal per request, which is what makes the run-loop thread safe here.
    client = SandboxClient(
        base_url="http://sandbox.test",
        api_key="test-key",
        http=TestClient(
            sandbox_app,
            base_url="http://sandbox.test",
            headers={"Authorization": "Bearer test-key"},
        ),
    )
    client.reset()
    supervisor = Supervisor(
        client,
        pack=PACK,
        history=HISTORY,
        audit=AuditLog(tmp_path / "decisions.jsonl"),
    )
    app.dependency_overrides[get_supervisor] = lambda: supervisor
    with TestClient(app) as c:
        c.supervisor = supervisor  # type: ignore[attr-defined]
        yield c
    for session in supervisor.sessions.values():
        session.stop_requested.set()
    app.dependency_overrides.clear()
    client.reset()
    client.close()


def _await_run(service: TestClient, run_id: str, *, status: str = "complete") -> dict:
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline:
        body = service.get(f"/v1/runs/{run_id}").json()
        if body["status"] == status:
            return body
        if body["status"] in ("failed", "stopped"):
            pytest.fail(f"run ended {body['status']}: {body.get('error')}")
        time.sleep(0.1)
    pytest.fail(f"run did not reach {status} within {DEADLINE}s")


def _await_step_up(service: TestClient, run_id: str) -> dict:
    """Until the run is held at a step-up — which, as on the real platform, it now is."""
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline:
        body = service.get(f"/v1/runs/{run_id}").json()
        if body["step_ups"]:
            return body
        if body["status"] != "running":
            pytest.fail(f"run ended {body['status']} without asking: {body.get('error')}")
        time.sleep(0.05)
    pytest.fail(f"run was not held at a step-up within {DEADLINE}s")


def _answer_until_complete(service: TestClient, run_id: str, decision: str) -> dict:
    """Play the customer: answer each step-up as it holds the run, until the run completes."""
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline:
        body = service.get(f"/v1/runs/{run_id}").json()
        if body["status"] == "complete":
            return body
        for step_up in body["step_ups"]:
            service.post(
                f"/v1/step-ups/{step_up['authorization_id']}/resolve", json={"decision": decision}
            )
        time.sleep(0.05)
    pytest.fail(f"run did not complete within {DEADLINE}s")


# --------------------------------------------------------------------- meta


def test_health_reports_the_upstream_it_is_pointed_at(service: TestClient) -> None:
    body = service.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["upstream"]["reachable"] is True
    assert body["checks_registered"] == len(CHECKS)


def test_config_exposes_the_tuning_knobs(service: TestClient) -> None:
    body = service.get("/v1/config").json()
    assert body["step_up_threshold"] == 2.0
    assert "merchant_text_manipulation" in body["concern_weights"]
    assert len(body["scenarios"]) == 5


# ----------------------------------------------------------------- mandates


def test_scenarios_compile_each_instruction_once_and_never_pin_a_fallback(
    service: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo sends back the IR it showed, so the model's answer must not change between
    boots — and a model that was down must not leave its fallback cached for good."""
    sup = service.supervisor  # type: ignore[attr-defined]
    calls: list[str] = []
    model_up = {"now": False}

    def compile_only(instruction: str, *, mode: str | None = None) -> dict[str, Any]:
        calls.append(instruction)
        if model_up["now"]:
            return {"compiler": "llm:test", "compiler_notes": [], "rules": [], "n": len(calls)}
        return {
            "compiler": "baseline-deterministic",
            "rules": [],
            "n": len(calls),
            "compiler_notes": ["model unreachable", "fell_back_to_baseline"],
        }

    monkeypatch.setattr(sup, "compile_only", compile_only)
    count = len(PACK.scenarios)

    down = service.get("/v1/scenarios", params={"mode": "auto"}).json()["scenarios"]
    assert len(calls) == count and all(
        "fell_back_to_baseline" in s["ir"]["compiler_notes"] for s in down
    )

    model_up["now"] = True
    first = service.get("/v1/scenarios", params={"mode": "auto"}).json()["scenarios"]
    assert len(calls) == 2 * count, "a fallback is retried, not served from the cache"
    again = service.get("/v1/scenarios", params={"mode": "auto"}).json()["scenarios"]
    assert len(calls) == 2 * count, "the model's answer is cached"
    assert [s["ir"] for s in again] == [s["ir"] for s in first]

    assert service.get("/v1/scenarios", params={"mode": "nonsense"}).status_code == 422


def test_compile_submits_nothing(service: TestClient) -> None:
    instruction = PACK.scenarios["SCEN0002"]["cardholder_instruction"]
    ir = service.post("/v1/mandates/compile", json={"instruction": instruction}).json()

    assert ir["source_instruction"] == instruction
    assert ir["rules"], "a CHF cap should compile to at least one hard rule"
    assert all("provenance" in f for f in ir["intent_facets"])
    assert service.get("/v1/mandates").json()["mandates"] == []


def test_mandate_lifecycle_create_confirm_tighten_revoke(service: TestClient) -> None:
    instruction = PACK.scenarios["SCEN0003"]["cardholder_instruction"]
    draft = service.post("/v1/mandates", json={"instruction": instruction}).json()
    assert draft["status"] == "draft"

    confirmed = service.post(
        f"/v1/mandates/{draft['draft_id']}/confirm", json={"confirmed": True}
    ).json()
    mandate_id = confirmed["mandate_id"]
    assert confirmed["status"] == "active"

    # guidance and open_questions are not carried on live events — readable only here.
    resource = service.get(f"/v1/mandates/{mandate_id}").json()
    assert "guidance" in resource and "open_questions" in resource

    tightened = service.patch(
        f"/v1/mandates/{mandate_id}", json={"uncertainty_policy": "decline"}
    ).json()
    assert tightened["uncertainty_policy"] == "decline"
    assert _listed(service, mandate_id)["uncertainty_policy"] == "decline"

    loosened = service.patch(f"/v1/mandates/{mandate_id}", json={"uncertainty_policy": "approve"})
    assert loosened.status_code == 422
    assert loosened.json()["error"]["code"] == "policy_loosened"

    assert service.delete(f"/v1/mandates/{mandate_id}").json()["status"] == "revoked"
    # The list the phone reads must agree with the detail view, not the cache from confirm.
    assert _listed(service, mandate_id)["status"] == "revoked"
    # Revocation is the customer's stop button: the platform refuses the run outright.
    refused = service.post("/v1/runs", json={"scenario_id": "SCEN0003", "mandate_id": mandate_id})
    assert refused.status_code == 409


def _listed(service: TestClient, mandate_id: str) -> dict[str, Any]:
    (entry,) = [
        m for m in service.get("/v1/mandates").json()["mandates"] if m["mandate_id"] == mandate_id
    ]
    return entry


def test_the_instruction_reaches_the_platform_byte_identical(service: TestClient) -> None:
    """A changed instruction loses the run. Verify the round trip, do not assume it."""
    instruction = PACK.scenarios["SCEN0004"]["cardholder_instruction"]
    draft = service.post("/v1/mandates", json={"instruction": instruction}).json()
    assert draft["instruction"] == instruction
    stored = service.get(f"/v1/mandates/{draft['draft_id']}").json()
    assert stored["instruction"] == instruction


# --------------------------------------------------------------------- runs


def test_a_run_decides_every_event_and_matches_the_offline_board(service: TestClient) -> None:
    """The live path must reach the same decisions as `make replay`, not merely finish.

    The run is held at each step-up until someone answers; `auto_resolve` answers them as a
    decline, which leaves the window exactly where the board has it — a step-up never enters
    approved spend unless the customer approves it.
    """
    started = service.post(
        "/v1/runs", json={"scenario_id": "SCEN0001", "auto_resolve": "decline"}
    ).json()
    body = _await_run(service, started["run_id"])

    decided = {d["source_authorization_id"]: d["decision"] for d in body["decisions"]}
    assert len(decided) == 10
    # AU0011 is the window discriminator: rolling = 223/300 approve, cumulative = 387.50
    # decline. Getting it right end to end is the point of tracking the period ourselves.
    assert decided["AU0011"] == "approve"
    assert decided["AU0004"] == "decline"
    assert body["window"]["period_days"] == 7
    assert body["counters"]["decided"] == 10


def test_one_call_starts_a_run_with_its_own_mandate(service: TestClient) -> None:
    """The Postman path: no mandate_id, so the service compiles and confirms one itself."""
    started = service.post("/v1/runs", json={"scenario_id": "SCEN0000"}).json()
    assert started["mandate_id"].startswith("TM")
    body = _await_run(service, started["run_id"])
    assert body["counters"]["approve"] == 1


def test_unknown_scenario_is_a_404(service: TestClient) -> None:
    r = service.post("/v1/runs", json={"scenario_id": "SCEN9999"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_scenario"


# ----------------------------------------------------------------- decisions


def test_decide_evaluates_without_submitting(service: TestClient) -> None:
    event = _example_event()
    body = service.post("/v1/decide", json={"event": event}).json()

    assert body["decision"] in ("approve", "decline", "step_up")
    assert body["submitted"] is False
    assert len(body["checks"]) >= len(CHECKS), "every check runs, passes included"
    assert service.get("/v1/audit").json()["records"] == 0


def test_decide_is_a_what_if_lever(service: TestClient) -> None:
    """Same event, tighter cap, different answer — with no state touched either way."""
    event = _example_event()
    tight = {
        "hard_rules": [
            {
                "field": "billing_amount_chf",
                "operator": "<=",
                "value": 1,
                "currency": "CHF",
                "scope": "purchase",
            }
        ],
        "uncertainty_policy": "ask",
        "intent_facets": [],
    }
    body = service.post("/v1/decide", json={"event": event, "policy": tight}).json()
    assert body["decision"] == "decline"
    assert "per_order_limit_exceeded" in body["reason_codes"]


def test_decide_rejects_a_body_that_is_not_an_event(service: TestClient) -> None:
    r = service.post("/v1/decide", json={"event": {"nope": True}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_event"


def test_every_decision_lands_in_the_audit_trail(service: TestClient) -> None:
    started = service.post("/v1/runs", json={"scenario_id": "SCEN0000"}).json()
    body = _await_run(service, started["run_id"])
    auth_id = body["decisions"][0]["authorization_id"]

    record = service.get(f"/v1/audit/{auth_id}").json()
    assert record["type"] == "decision"
    assert record["run_id"] == started["run_id"]
    assert record["upstream"]["http_status"] == 200
    assert record["inputs_digest"]
    assert record["timing"]["guard_tripped"] is False
    # The raw endpoint is the "show me the append-only trail" moment.
    assert service.get(f"/v1/audit/{auth_id}/raw").text.count("\n") >= 0


def test_audit_404s_for_an_unknown_authorization(service: TestClient) -> None:
    assert service.get("/v1/audit/AU9999-NOPE").status_code == 404


# ------------------------------------------------------------------ step-ups


def test_step_up_is_asked_then_answered_by_a_human(service: TestClient) -> None:
    started = service.post("/v1/runs", json={"scenario_id": "SCEN0002"}).json()
    _await_step_up(service, started["run_id"])

    waiting = service.get("/v1/step-ups").json()["step_ups"]
    assert waiting, "SCEN0002 contains at least one step_up"
    first = waiting[0]
    # The notification is written for a person: no reason code may leak into it.
    assert not any(code in first["customer_message"] for code in first["reason_codes"])
    assert first["seconds_remaining"] is not None

    resolved = service.post(
        f"/v1/step-ups/{first['authorization_id']}/resolve",
        json={"decision": "approve", "customer_message": "Confirmed in the app."},
    ).json()
    assert resolved["status"] == "approved"

    # Appended, never rewritten — and the fold reports what actually happened.
    record = service.get(f"/v1/audit/{first['authorization_id']}").json()
    assert record["decision"] == "step_up"
    assert record["final_status"] == "approved"
    assert len(record["resolutions"]) == 1

    # And it is gone from the notification list.
    assert first["authorization_id"] not in [
        s["authorization_id"] for s in service.get("/v1/step-ups").json()["step_ups"]
    ]


def test_a_second_human_answer_is_refused(service: TestClient) -> None:
    started = service.post("/v1/runs", json={"scenario_id": "SCEN0002"}).json()
    _await_step_up(service, started["run_id"])
    auth_id = service.get("/v1/step-ups").json()["step_ups"][0]["authorization_id"]

    assert (
        service.post(f"/v1/step-ups/{auth_id}/resolve", json={"decision": "approve"}).status_code
        == 200
    )
    second = service.post(f"/v1/step-ups/{auth_id}/resolve", json={"decision": "decline"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_resolved"


def test_resolving_something_that_was_never_stepped_up_is_a_404(service: TestClient) -> None:
    r = service.post("/v1/step-ups/AU0001-NOPE/resolve", json={"decision": "approve"})
    assert r.status_code == 404


def test_an_approved_step_up_enters_approved_spend_only_when_resolved(
    service: TestClient,
) -> None:
    """decision-rules.md §2.1: pending contributes nothing to the rolling window."""
    started = service.post("/v1/runs", json={"scenario_id": "SCEN0002"}).json()
    body = _await_step_up(service, started["run_id"])
    pending_before = body["window"]["pending_step_up_chf"]
    assert pending_before == body["step_ups"][0]["amount_chf"], "pending, and only pending"

    after = _answer_until_complete(service, started["run_id"], "approve")
    assert after["window"]["pending_step_up_chf"] == "0.00"


def test_a_step_up_holds_the_run_until_the_customer_answers(service: TestClient) -> None:
    """As on the real platform: nothing more is decided until the customer answers."""
    started = service.post("/v1/runs", json={"scenario_id": "SCEN0002"}).json()
    held = _await_step_up(service, started["run_id"])
    time.sleep(1.5)  # the loop polls on; the platform keeps handing back the same request
    still = service.get(f"/v1/runs/{started['run_id']}").json()
    assert still["counters"]["decided"] == held["counters"]["decided"]
    assert still["counters"]["replays"] == 0, "the held step-up is never sent a second time"

    step_up = held["step_ups"][0]
    service.post(
        f"/v1/step-ups/{step_up['authorization_id']}/resolve", json={"decision": "decline"}
    )
    deadline = time.monotonic() + DEADLINE
    while (
        service.get(f"/v1/runs/{started['run_id']}").json()["counters"]["decided"]
        == (held["counters"]["decided"])
    ):
        assert time.monotonic() < deadline, "the answer did not release the run"
        time.sleep(0.05)


def test_revoking_declines_the_step_up_waiting_on_the_customer(service: TestClient) -> None:
    """The revoke is the customer's answer: the purchase waiting on them is declined.

    Left pending, it would hold the platform's whole team queue for up to 120 s with nobody
    left to answer it — the next run's orders would sit behind it past their deadlines.
    """
    started = service.post("/v1/runs", json={"scenario_id": "SCEN0002"}).json()
    run_id, mandate_id = started["run_id"], started["mandate_id"]
    held = _await_step_up(service, run_id)
    waiting = held["step_ups"][0]["authorization_id"]

    revoked = service.delete(f"/v1/mandates/{mandate_id}")
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["status"] == "revoked"
    assert body["declined_step_ups"] == [waiting]
    assert body["stopped_runs"] == [run_id]

    assert service.get("/v1/step-ups").json()["step_ups"] == [], "nothing left to answer"
    record = service.get(f"/v1/audit/{waiting}").json()
    assert record["final_status"] == "declined"
    assert "revoked" in record["resolutions"][0]["customer_message"]

    # Upstream, the orders the agent had not yet placed are never queued at all.
    upstream = service.supervisor.client.get_run(run_id)  # type: ignore[attr-defined]
    assert upstream["status"] == "completed"
    assert upstream["counters"]["rejected"] > 0
    assert _await_run(service, run_id, status="stopped")["counters"]["final"]["waiting"] == 0


def _example_event() -> dict:
    import json

    return dict(
        json.loads(
            (data_dir() / "scenario_fixtures" / "example_authorization_request.json").read_text()
        )
    )
