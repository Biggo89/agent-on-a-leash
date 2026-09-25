"""End-to-end protocol tests against the offline replica.

These exercise the parts that are easy to get wrong on the day and impossible to rehearse
against the real sandbox before we have a key: mandate lifecycle, tighten-only PATCH,
instruction integrity, event-schema validity, idempotent redelivery, and step-up resolution.
"""

from __future__ import annotations

import jsonschema
import pytest
from fastapi.testclient import TestClient

from leash.compile.baseline import compile_instruction, to_mandate_payload
from sandbox.fixtures import DataPack
from sandbox.server import app

PACK = DataPack.load()
AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/v1/team/reset", headers=AUTH)
        yield c
        c.post("/v1/team/reset", headers=AUTH)


def _active_mandate(client: TestClient, scenario_id: str) -> dict:
    instruction = PACK.scenarios[scenario_id]["cardholder_instruction"]
    payload = to_mandate_payload(compile_instruction(instruction))
    draft = client.post("/v1/mandates", json=payload, headers=AUTH).json()
    return client.post(
        f"/v1/mandates/{draft['draft_id']}/confirm", json={"confirmed": True}, headers=AUTH
    ).json()


def test_healthz_is_unauthenticated(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"


def test_authenticated_endpoints_require_bearer(client: TestClient) -> None:
    assert client.get("/v1/bootstrap").status_code == 401


def test_mandate_must_be_confirmed_before_a_run(client: TestClient) -> None:
    instruction = PACK.scenarios["SCEN0000"]["cardholder_instruction"]
    draft = client.post(
        "/v1/mandates", json=to_mandate_payload(compile_instruction(instruction)), headers=AUTH
    ).json()
    r = client.post(
        "/v1/scenario-runs",
        json={"scenario_id": "SCEN0000", "mandate_id": draft["draft_id"]},
        headers=AUTH,
    )
    assert r.status_code == 409


def test_changed_instruction_is_rejected(client: TestClient) -> None:
    """The sandbox rejects a changed instruction: intent may be structured, not replaced."""
    mandate = _active_mandate(client, "SCEN0000")
    client.patch(f"/v1/mandates/{mandate['mandate_id']}", json={"guidance": ["x"]}, headers=AUTH)
    tampered = client.post(
        "/v1/mandates",
        json={
            "instruction": "Buy me anything you like.",
            "hard_rules": [],
            "uncertainty_policy": "ask",
        },
        headers=AUTH,
    ).json()
    client.post(
        f"/v1/mandates/{tampered['draft_id']}/confirm", json={"confirmed": True}, headers=AUTH
    )
    r = client.post(
        "/v1/scenario-runs",
        json={"scenario_id": "SCEN0000", "mandate_id": tampered["draft_id"]},
        headers=AUTH,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "instruction_mismatch"


def test_patch_is_tighten_only(client: TestClient) -> None:
    mandate = _active_mandate(client, "SCEN0001")
    mid = mandate["mandate_id"]

    # Adding a rule is allowed.
    added = mandate["hard_rules"] + [
        {"field": "billing_amount_chf", "operator": "<=", "value": 50, "scope": "purchase"}
    ]
    assert (
        client.patch(f"/v1/mandates/{mid}", json={"hard_rules": added}, headers=AUTH).status_code
        == 200
    )

    # Removing one is not.
    assert (
        client.patch(f"/v1/mandates/{mid}", json={"hard_rules": []}, headers=AUTH).status_code
        == 422
    )

    # uncertainty_policy may only move toward decline.
    assert (
        client.patch(
            f"/v1/mandates/{mid}", json={"uncertainty_policy": "decline"}, headers=AUTH
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/v1/mandates/{mid}", json={"uncertainty_policy": "approve"}, headers=AUTH
        ).status_code
        == 422
    )


def test_revoked_mandate_cannot_start_a_run(client: TestClient) -> None:
    mandate = _active_mandate(client, "SCEN0000")
    client.delete(f"/v1/mandates/{mandate['mandate_id']}", headers=AUTH)
    r = client.post(
        "/v1/scenario-runs",
        json={"scenario_id": "SCEN0000", "mandate_id": mandate["mandate_id"]},
        headers=AUTH,
    )
    assert r.status_code == 409


@pytest.mark.parametrize("scenario_id", sorted(PACK.scenarios))
def test_every_delivered_event_is_schema_valid(client: TestClient, scenario_id: str) -> None:
    """Validate the envelope's `data` against authorization_event.schema.json — the envelope
    itself is not an authorization.request."""
    schema = PACK.event_schema()
    mandate = _active_mandate(client, scenario_id)
    client.post(
        "/v1/scenario-runs",
        json={"scenario_id": scenario_id, "mandate_id": mandate["mandate_id"]},
        headers=AUTH,
    )

    seen = 0
    while True:
        r = client.get("/v1/decision-requests/next", params={"wait": 0}, headers=AUTH)
        if r.status_code == 204:
            break
        envelope = r.json()
        jsonschema.validate(envelope["data"], schema)
        seen += 1
        client.post(
            f"/v1/authorizations/{envelope['authorization_id']}/decision",
            json={"authorization_id": envelope["authorization_id"], "decision": "decline"},
            headers=AUTH,
        )
    assert seen == int(PACK.scenarios[scenario_id]["event_count"])


def test_an_idle_long_poll_returns_a_bodyless_204(client: TestClient) -> None:
    """RFC 9110 §15.3.5: a 204 carries no body.

    `JSONResponse(content=None)` writes the four bytes `null`, which uvicorn rejects with
    "Response content longer than Content-Length" — once per idle poll, so a healthy replay
    buries its own log in tracebacks while every decision still lands. The TestClient does not
    enforce that framing, so assert the body directly or the bug comes straight back.
    """
    r = client.get("/v1/decision-requests/next", params={"wait": 0}, headers=AUTH)

    assert r.status_code == 204
    assert r.content == b"", f"a 204 must be empty, got {r.content!r}"
    assert "content-type" not in r.headers
    assert r.headers.get("content-length", "0") == "0"


def test_step_up_then_resolve(client: TestClient) -> None:
    mandate = _active_mandate(client, "SCEN0000")
    client.post(
        "/v1/scenario-runs",
        json={"scenario_id": "SCEN0000", "mandate_id": mandate["mandate_id"]},
        headers=AUTH,
    )
    envelope = client.get("/v1/decision-requests/next", params={"wait": 0}, headers=AUTH).json()
    auth_id = envelope["authorization_id"]

    r = client.post(
        f"/v1/authorizations/{auth_id}/decision", json={"decision": "step_up"}, headers=AUTH
    )
    assert r.json()["status"] == "pending_step_up"

    # After step_up the decision endpoint refuses a second automated decision — the same one
    # included — with the real API's code.
    for again in ("approve", "step_up"):
        refused = client.post(
            f"/v1/authorizations/{auth_id}/decision", json={"decision": again}, headers=AUTH
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "step_up_resolution_required"

    # Only /resolve applies, and only with approve or decline.
    assert (
        client.post(
            f"/v1/authorizations/{auth_id}/resolve", json={"decision": "step_up"}, headers=AUTH
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/authorizations/{auth_id}/resolve", json={"decision": "approve"}, headers=AUTH
        ).json()["status"]
        == "approved"
    )


def _start(client: TestClient, scenario_id: str) -> str:
    mandate = _active_mandate(client, scenario_id)
    run = client.post(
        "/v1/scenario-runs",
        json={"scenario_id": scenario_id, "mandate_id": mandate["mandate_id"]},
        headers=AUTH,
    ).json()
    return str(run["run_id"])


def _next(client: TestClient) -> dict | None:
    r = client.get("/v1/decision-requests/next", params={"wait": 0}, headers=AUTH)
    return None if r.status_code == 204 else r.json()


def _decide(client: TestClient, auth_id: str, decision: str) -> None:
    client.post(f"/v1/authorizations/{auth_id}/decision", json={"decision": decision}, headers=AUTH)


def test_a_step_up_holds_the_whole_team_queue(client: TestClient) -> None:
    """As measured on the real platform, 2026-09-24 (service-contract.md §5).

    While a step-up waits on the customer, every poll hands that same request back, marked
    `pending_step_up` — and nothing else is delivered, another run's orders included.
    """
    _start(client, "SCEN0000")
    first = _next(client)
    assert first is not None
    _decide(client, first["authorization_id"], "step_up")

    _start(client, "SCEN0001")
    for _ in range(3):
        held = _next(client)
        assert held is not None
        assert held["authorization_id"] == first["authorization_id"], "the same request, again"
        assert held["status"] == "pending_step_up"

    client.post(
        f"/v1/authorizations/{first['authorization_id']}/resolve",
        json={"decision": "decline"},
        headers=AUTH,
    )
    released = _next(client)
    assert released is not None and released["status"] == "awaiting_decision"
    assert released["data"]["authorization"]["source_authorization_id"] == "AU0002"


def test_an_unanswered_step_up_expires_into_a_platform_decline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window closes lazily, on the next request, and the platform declines it itself."""
    from sandbox import server

    monkeypatch.setattr(server, "STEP_UP_WINDOW_SECONDS", 0)
    run_id = _start(client, "SCEN0000")
    first = _next(client)
    assert first is not None
    auth_id = first["authorization_id"]
    _decide(client, auth_id, "step_up")

    assert _next(client) is None, "settled, and SCEN0000 has nothing after it"
    row = next(
        a
        for a in client.get("/v1/authorizations", headers=AUTH).json()["authorizations"]
        if a["authorization_id"] == auth_id
    )
    assert row["status"] == "declined"
    assert row["reason_codes"] == ["step_up_expired"]
    late = client.post(
        f"/v1/authorizations/{auth_id}/resolve", json={"decision": "approve"}, headers=AUTH
    )
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "authorization_not_pending"
    run = client.get(f"/v1/scenario-runs/{run_id}", headers=AUTH).json()
    assert run["status"] == "completed"


def test_a_run_is_not_complete_while_its_step_up_waits(client: TestClient) -> None:
    run_id = _start(client, "SCEN0000")
    first = _next(client)
    assert first is not None
    _decide(client, first["authorization_id"], "step_up")

    run = client.get(f"/v1/scenario-runs/{run_id}", headers=AUTH).json()
    assert run["status"] == "running" and run["complete"] is False

    client.post(
        f"/v1/authorizations/{first['authorization_id']}/resolve",
        json={"decision": "approve"},
        headers=AUTH,
    )
    assert client.get(f"/v1/scenario-runs/{run_id}", headers=AUTH).json()["status"] == "completed"


def test_revoking_rejects_the_orders_not_yet_queued(client: TestClient) -> None:
    mandate = _active_mandate(client, "SCEN0001")
    run_id = client.post(
        "/v1/scenario-runs",
        json={"scenario_id": "SCEN0001", "mandate_id": mandate["mandate_id"]},
        headers=AUTH,
    ).json()["run_id"]
    first = _next(client)
    assert first is not None
    _decide(client, first["authorization_id"], "approve")

    client.delete(f"/v1/mandates/{mandate['mandate_id']}", headers=AUTH)

    assert _next(client) is None, "nothing further reaches the control layer"
    run = client.get(f"/v1/scenario-runs/{run_id}", headers=AUTH).json()
    assert run["status"] == "completed"
    assert run["counters"]["rejected"] == 9 and run["counters"]["delivered"] == 1


def test_repeat_decision_is_idempotent(client: TestClient) -> None:
    """Delivery is at least once — a repeated identical decision must be safe."""
    mandate = _active_mandate(client, "SCEN0000")
    client.post(
        "/v1/scenario-runs",
        json={"scenario_id": "SCEN0000", "mandate_id": mandate["mandate_id"]},
        headers=AUTH,
    )
    auth_id = client.get("/v1/decision-requests/next", params={"wait": 0}, headers=AUTH).json()[
        "authorization_id"
    ]

    first = client.post(
        f"/v1/authorizations/{auth_id}/decision", json={"decision": "approve"}, headers=AUTH
    )
    repeat = client.post(
        f"/v1/authorizations/{auth_id}/decision", json={"decision": "approve"}, headers=AUTH
    )
    assert first.status_code == 200 and repeat.status_code == 200
    assert repeat.json()["idempotent"] is True

    conflicting = client.post(
        f"/v1/authorizations/{auth_id}/decision", json={"decision": "decline"}, headers=AUTH
    )
    assert conflicting.status_code == 409


def test_event_feed_cursor_advances(client: TestClient) -> None:
    mandate = _active_mandate(client, "SCEN0000")
    client.post(
        "/v1/scenario-runs",
        json={"scenario_id": "SCEN0000", "mandate_id": mandate["mandate_id"]},
        headers=AUTH,
    )
    first = client.get("/v1/events", params={"since": 0}, headers=AUTH).json()
    assert first["events"] and first["next_cursor"] > 0
    second = client.get("/v1/events", params={"since": first["next_cursor"]}, headers=AUTH).json()
    assert second["events"] == []
