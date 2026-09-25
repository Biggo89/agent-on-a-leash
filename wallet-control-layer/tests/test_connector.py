"""The connector, end to end: an agent host speaking MCP, the Payment App issuing the token,
the decision service judging the orders, the phone answering — all in one process.

The replica, the service and the app are mounted through ASGI transports, so no port opens
and no key is needed. What this proves is the *chain*: discovery → consent → token →
introspection → tool → the same Supervisor tables the phone reads.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

from leash.adapters.client import SandboxClient
from leash.adapters.datapack import DataPack, data_dir
from leash.adapters.history import HistoryIndex
from leash.audit.log import AuditLog
from leash.connector.auth import IntrospectionVerifier
from leash.connector.http import STATE
from leash.runtime.supervisor import Supervisor
from leash.service.app import app, get_supervisor
from payment_app.server import PERSONAS, STORE, WIRING
from payment_app.server import app as phone_app
from sandbox.server import app as sandbox_app

PACK = DataPack.load()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")
SECRET = "dev-introspection-secret"
REDIRECT = "http://localhost:9999/callback"
HOUSEHOLD = next(
    s["cardholder_instruction"]
    for s in PACK.scenarios.values()
    if s["scenario_name"] == "Household budget"
)
CARDHOLDER = PERSONAS[0]  # the persona with the richest history
USUAL_GROCER = max(
    (
        row
        for mid, row in PACK.merchants.items()
        if row["merchant_category"] == "groceries"
        and mid in HISTORY.known_merchants(CARDHOLDER.card_id)
    ),
    key=lambda row: HISTORY.merchant_approvals(CARDHOLDER.card_id, row["merchant_id"]),
)


@pytest.fixture()
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    replica = SandboxClient(
        base_url="http://sandbox.test",
        api_key="test-key",
        http=TestClient(
            sandbox_app,
            base_url="http://sandbox.test",
            headers={"Authorization": "Bearer test-key"},
        ),
    )
    replica.reset()
    supervisor = Supervisor(
        replica, pack=PACK, history=HISTORY, audit=AuditLog(tmp_path / "decisions.jsonl")
    )
    app.dependency_overrides[get_supervisor] = lambda: supervisor

    STORE.reset(None)
    monkeypatch.setenv("WALLET_INTROSPECTION_SECRET", SECRET)
    monkeypatch.setenv("LEASH_SERVICE_URL", "http://engine.test")
    phone = TestClient(phone_app, base_url="http://app.test")
    STATE.reset()
    STATE.verifier = IntrospectionVerifier("http://app.test", SECRET, http=phone)

    with TestClient(app, base_url="http://engine.test") as service:
        WIRING.service_http = service
        yield {"service": service, "phone": phone, "supervisor": supervisor}

    for session in supervisor.sessions.values():
        session.stop_requested.set()
    app.dependency_overrides.clear()
    WIRING.service_http = None
    STATE.reset()
    STORE.reset(None)
    replica.reset()
    replica.close()


# --------------------------------------------------------------------- helpers


def connect(
    phone: TestClient, *, agent: str = "Claude", scope: str | None = None
) -> dict[str, Any]:
    """What an MCP client does after reading the discovery documents."""
    client = phone.post(
        "/register",
        json={
            "client_name": agent,
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
        },
    ).json()
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT,
        "state": "abc",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": "http://engine.test/mcp",
    }
    if scope:
        params["scope"] = scope
    phone.post(
        "/login",
        data={
            "subject": CARDHOLDER.customer_id,
            "pin": "0000",
            "next": "/authorize?" + urlencode(params),
        },
        follow_redirects=False,
    )
    assert "Confirm Leash" in phone.get("/authorize", params=params).text
    r = phone.post("/authorize", data={**params, "decision": "approve"}, follow_redirects=False)
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    tokens = phone.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client["client_id"],
            "code_verifier": verifier,
        },
    ).json()
    return {"token": tokens["access_token"], "client_id": client["client_id"], "agent": agent}


def rpc(
    service: TestClient, token: str | None, method: str, params: Any = None, msg_id: Any = 1
) -> Any:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        body["id"] = msg_id
    return service.post("/mcp", json=body, headers=headers)


def call(service: TestClient, token: str, tool: str, **arguments: Any) -> dict[str, Any]:
    r = rpc(service, token, "tools/call", {"name": tool, "arguments": arguments})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    result: dict[str, Any] = body["result"]
    return result


def text_of(result: dict[str, Any]) -> str:
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


def grocery_order(
    amount: float, *, merchant: str | None = None, item: str = "Fresh produce selection"
) -> dict[str, Any]:
    return {
        "merchant": merchant or USUAL_GROCER["merchant_name"],
        "items": [{"name": item, "quantity": 1, "unit_price": amount}],
        "description": "weekly groceries",
    }


def mandate_ready(stack: dict[str, Any], token: str) -> str:
    """Propose the household instruction and confirm it on the phone; returns the mandate id."""
    draft = call(stack["service"], token, "propose_mandate", instruction=HOUSEHOLD)
    assert draft["isError"] is False, text_of(draft)
    draft_id = draft["structuredContent"]["draft_id"]
    confirmed = stack["service"].post(f"/v1/mandates/{draft_id}/confirm", json={"confirmed": True})
    assert confirmed.status_code == 200, confirmed.text
    return str(confirmed.json()["mandate_id"])


# --------------------------------------------------------------------- discovery


def test_an_unauthenticated_call_is_told_where_consent_lives(stack: dict[str, Any]) -> None:
    r = rpc(stack["service"], None, "initialize")
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        'resource_metadata="http://engine.test/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert 'error="' not in challenge  # no token at all is not an invalid token (RFC 6750)

    doc = stack["service"].get("/.well-known/oauth-protected-resource/mcp").json()
    assert doc["resource"] == "http://engine.test/mcp"
    assert doc["authorization_servers"] == ["http://127.0.0.1:8081"]
    assert set(doc["scopes_supported"]) == {"mandate:propose", "payment:request", "activity:read"}
    assert doc["resource_name"] == "Leash Wallet"  # what a client may show the cardholder

    bad = rpc(stack["service"], "not-a-token", "initialize")
    assert bad.status_code == 401 and 'error="invalid_token"' in bad.headers["www-authenticate"]


def test_initialize_lists_five_tools_and_the_leash_rules(stack: dict[str, Any]) -> None:
    token = connect(stack["phone"])["token"]
    init = rpc(
        stack["service"],
        token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    ).json()["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert (init["serverInfo"]["name"], init["serverInfo"]["title"]) == (
        "leash-wallet",
        "Leash Wallet",
    )
    assert "never approve your own requests" in init["instructions"]
    assert "tools" in init["capabilities"] and "resources" in init["capabilities"]
    # An unknown version gets our default rather than an echo of something we do not speak.
    assert (
        rpc(stack["service"], token, "initialize", {"protocolVersion": "1999-01-01"}).json()[
            "result"
        ]["protocolVersion"]
        == "2025-06-18"
    )

    assert rpc(stack["service"], token, "notifications/initialized", msg_id=None).status_code == 202
    assert rpc(stack["service"], token, "ping").json()["result"] == {}

    tools = rpc(stack["service"], token, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "whats_allowed",
        "propose_mandate",
        "request_payment",
        "payment_status",
        "recent_activity",
    ]
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object" and len(tool["description"]) > 40
    assert not any(
        verb in t["name"] for t in tools for verb in ("confirm", "resolve", "amend", "revoke")
    )

    resources = rpc(stack["service"], token, "resources/list").json()["result"]["resources"]
    assert {r["uri"] for r in resources} == {"wallet://mandate/current", "wallet://ledger/window"}


def test_protocol_errors_are_json_rpc_errors(stack: dict[str, Any]) -> None:
    token = connect(stack["phone"])["token"]
    service = stack["service"]
    r = service.post(
        "/mcp",
        content=b"{not json",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    assert rpc(service, token, "prompts/list").json()["error"]["code"] == -32601
    assert (
        rpc(service, token, "tools/call", {"name": "confirm_mandate"}).json()["error"]["code"]
        == -32602
    )
    assert (
        rpc(service, token, "resources/read", {"uri": "wallet://nope"}).json()["error"]["code"]
        == -32002
    )
    assert service.get("/mcp").status_code == 405
    evil = service.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
    )
    assert evil.status_code == 403
    batch = service.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "id": "a", "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert batch.status_code == 200 and [a["id"] for a in batch.json()] == ["a"]


# --------------------------------------------------------------------- the errand


def test_without_a_mandate_the_agent_is_told_to_ask_the_cardholder(stack: dict[str, Any]) -> None:
    token = connect(stack["phone"])["token"]
    allowed = call(stack["service"], token, "whats_allowed")
    assert allowed["structuredContent"]["mandate"] is None
    assert "propose_mandate" in text_of(allowed)
    assert allowed["structuredContent"]["known_shops"], "the card's history should be offered"
    refused = call(stack["service"], token, "request_payment", **grocery_order(50))
    assert refused["isError"] is True and "No active mandate" in text_of(refused)


def test_a_proposal_waits_on_the_phone_and_nothing_can_be_bought_meanwhile(
    stack: dict[str, Any],
) -> None:
    service, phone = stack["service"], stack["phone"]
    token = connect(phone, agent="Claude")["token"]
    draft = call(service, token, "propose_mandate", instruction=HOUSEHOLD)
    body = draft["structuredContent"]
    assert body["status"] == "draft" and any("CHF 120.00 per order" in s for s in body["rules"])
    assert "waiting for the cardholder" in text_of(draft)

    proposals = service.get("/connector/proposals").json()["proposals"]
    assert proposals[0]["draft_id"] == body["draft_id"] and proposals[0]["agent_name"] == "Claude"
    assert proposals[0]["status"] == "draft"
    listed = service.get("/v1/mandates").json()["mandates"]
    assert any(m["mandate_id"] == body["draft_id"] and m["status"] == "draft" for m in listed)

    refused = call(service, token, "request_payment", **grocery_order(50))
    assert refused["isError"] is True and body["draft_id"] in text_of(refused)
    # The instruction reached the platform byte-identical (create_mandate verifies the hash).
    assert service.get(f"/v1/mandates/{body['draft_id']}").json()["instruction"] == HOUSEHOLD


def test_an_ordinary_order_is_approved_and_lands_everywhere_the_phone_looks(
    stack: dict[str, Any],
) -> None:
    service, phone = stack["service"], stack["phone"]
    token = connect(phone)["token"]
    mandate_id = mandate_ready(stack, token)

    allowed = call(service, token, "whats_allowed")["structuredContent"]
    assert allowed["mandate"]["mandate_id"] == mandate_id
    assert any("across any 7 days" in s for s in allowed["mandate"]["rules"])

    result = call(service, token, "request_payment", **grocery_order(45), delivery_fee=5)
    assert result["isError"] is False, text_of(result)
    body = result["structuredContent"]
    assert body["decision"] == "approve" and text_of(result).startswith("APPROVED")
    assert body["authorization"]["billing_amount_chf"] == "50.00"
    assert body["authorization"]["merchant_id"] == USUAL_GROCER["merchant_id"]
    assert (
        body["window"]["approved_spend_chf"] == "50.00" and body["window"]["limit_chf"] == "300.00"
    )
    assert not any(code in body["customer_message"] for code in body["reason_codes"])

    # The same tables the phone and the demo read.
    decisions = service.get("/v1/decisions").json()["decisions"]
    assert decisions[0]["authorization_id"] == body["authorization_id"]
    runs = service.get("/v1/runs").json()["runs"]
    assert runs[0]["mandate_id"] == mandate_id and runs[0]["status"] == "running"
    agents = service.get("/connector/agents").json()["agents"]
    assert agents[0]["orders"] == 1 and agents[0]["window"]["approved_spend_chf"] == "50.00"
    record = service.get(f"/v1/audit/{body['authorization_id']}").json()
    assert record["decision"] == "approve" and record["upstream"]["channel"] == "connector"
    assert record["final_status"] == "approved"

    status = call(service, token, "payment_status", authorization_id=body["authorization_id"])
    assert status["structuredContent"]["status"] == "approved"
    activity = call(service, token, "recent_activity", limit=5)["structuredContent"]["orders"]
    assert [o["authorization_id"] for o in activity] == [body["authorization_id"]]


def test_an_order_over_the_cap_is_declined_in_the_cardholders_words(stack: dict[str, Any]) -> None:
    service, phone = stack["service"], stack["phone"]
    token = connect(phone)["token"]
    mandate_ready(stack, token)
    result = call(
        service, token, "request_payment", **grocery_order(130, item="Weekly grocery basket")
    )
    body = result["structuredContent"]
    assert body["decision"] == "decline" and text_of(result).startswith("DECLINED")
    assert "CHF 120.00" in body["customer_message"]
    assert body["window"]["approved_spend_chf"] == "0.00"  # a decline never enters the ledger
    assert (
        call(service, token, "payment_status", authorization_id=body["authorization_id"])[
            "structuredContent"
        ]["status"]
        == "declined"
    )


def test_a_split_order_is_stepped_up_and_the_phone_answers(stack: dict[str, Any]) -> None:
    """Two orders at the same shop, each under the cap, together above it: the engine asks
    (specs/check-split-order.md), the step-up shows on the phone with its countdown, and the
    cardholder's approval enters the window only when given."""
    service, phone = stack["service"], stack["phone"]
    token = connect(phone)["token"]
    mandate_ready(stack, token)
    first = call(service, token, "request_payment", **grocery_order(70))["structuredContent"]
    assert first["decision"] == "approve"
    second = call(service, token, "request_payment", **grocery_order(65, item="Pantry staples"))
    body = second["structuredContent"]
    assert body["decision"] == "step_up", text_of(second)
    assert "split_order_suspected" in body["reason_codes"]
    assert text_of(second).startswith("PENDING") and body["authorization_id"] in text_of(second)
    assert body["seconds_remaining"] is not None and body["seconds_remaining"] > 0

    waiting = service.get("/v1/step-ups").json()["step_ups"]
    assert [s["authorization_id"] for s in waiting] == [body["authorization_id"]]
    assert (
        waiting[0]["seconds_remaining"] > 0
        and waiting[0]["merchant_name"] == USUAL_GROCER["merchant_name"]
    )
    pending = call(service, token, "payment_status", authorization_id=body["authorization_id"])[
        "structuredContent"
    ]
    assert pending["status"] == "pending" and pending["window"]["pending_step_up_chf"] == "65.00"
    assert pending["window"]["approved_spend_chf"] == "70.00"

    answered = service.post(
        f"/v1/step-ups/{body['authorization_id']}/resolve",
        json={"decision": "approve", "customer_message": "Yes, that is me."},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == "approved"
    after = call(service, token, "payment_status", authorization_id=body["authorization_id"])[
        "structuredContent"
    ]
    assert after["status"] == "approved" and after["window"]["pending_step_up_chf"] == "0.00"
    assert after["window"]["approved_spend_chf"] == "135.00"  # entered the window when answered
    # The message follows the answer; the question it asked stays readable beside it.
    assert (
        after["customer_message"] == "The cardholder approved it in the app; the order is through."
    )
    assert after["asked_message"] == body["customer_message"]
    assert service.get("/v1/step-ups").json()["step_ups"] == []
    record = service.get(f"/v1/audit/{body['authorization_id']}").json()
    assert record["final_status"] == "approved" and len(record["resolutions"]) == 1
    again = service.post(
        f"/v1/step-ups/{body['authorization_id']}/resolve", json={"decision": "decline"}
    )
    assert again.status_code == 409


def test_an_unanswered_step_up_expires_and_charges_nothing(
    stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, phone = stack["service"], stack["phone"]
    token = connect(phone)["token"]
    mandate_ready(stack, token)
    monkeypatch.setattr(stack["supervisor"], "step_up_window_seconds", lambda: 0)
    call(service, token, "request_payment", **grocery_order(70))
    asked = call(service, token, "request_payment", **grocery_order(65, item="Pantry staples"))[
        "structuredContent"
    ]
    assert asked["decision"] == "step_up"
    status = call(service, token, "payment_status", authorization_id=asked["authorization_id"])[
        "structuredContent"
    ]
    assert status["status"] == "expired" and status["window"]["pending_step_up_chf"] == "0.00"
    assert status["window"]["approved_spend_chf"] == "70.00"
    late = service.post(
        f"/v1/step-ups/{asked['authorization_id']}/resolve", json={"decision": "approve"}
    )
    assert late.status_code == 409
    record = service.get(f"/v1/audit/{asked['authorization_id']}").json()
    assert (
        record["final_status"] == "declined"
        and record["resolutions"][0]["resolved_by"] == "timeout"
    )


def test_an_unknown_shop_is_judged_on_the_facts_and_flagged_as_unknown(
    stack: dict[str, Any],
) -> None:
    service, phone = stack["service"], stack["phone"]
    token = connect(phone)["token"]
    mandate_ready(stack, token)
    result = call(
        service, token, "request_payment", **grocery_order(40, merchant="Corner Shop Zürich")
    )
    body = result["structuredContent"]
    assert body["authorization"]["merchant_id"].startswith("ME-AGENT-")
    assert body["authorization"]["merchant_category"] == "unknown"
    assert "not in the card's merchant records" in text_of(result)
    assert body["decision"] in ("approve", "decline", "step_up")


def test_a_bad_order_is_a_tool_error_not_a_crash(stack: dict[str, Any]) -> None:
    service, phone = stack["service"], stack["phone"]
    token = connect(phone)["token"]
    mandate_ready(stack, token)
    for order, expected in (
        ({"merchant": USUAL_GROCER["merchant_name"], "items": []}, "items must be"),
        ({"merchant": "", "items": [{"name": "x", "unit_price": 1}]}, "merchant is required"),
        ({"merchant": "Shop", "items": [{"name": "x", "unit_price": -1}]}, "unit_price"),
        (
            {"merchant": "Shop", "items": [{"name": "x", "unit_price": 1}], "currency": "JPY"},
            "currency must be",
        ),
    ):
        result = call(service, token, "request_payment", **order)
        assert result["isError"] is True and expected in text_of(result), (order, text_of(result))
    assert service.get("/v1/decisions").json()["decisions"] == []


def test_scopes_bind_the_tools(stack: dict[str, Any]) -> None:
    service, phone = stack["service"], stack["phone"]
    token = connect(phone, scope="activity:read")["token"]
    allowed = call(service, token, "whats_allowed")
    assert allowed["isError"] is False and allowed["structuredContent"]["scopes"] == [
        "activity:read"
    ]
    refused = call(service, token, "request_payment", **grocery_order(10))
    assert refused["isError"] is True and "payment:request" in text_of(refused)
    refused = call(service, token, "propose_mandate", instruction=HOUSEHOLD)
    assert refused["isError"] is True and "mandate:propose" in text_of(refused)


def test_the_resources_mirror_the_tools(stack: dict[str, Any]) -> None:
    import json

    service, phone = stack["service"], stack["phone"]
    token = connect(phone)["token"]
    mandate_id = mandate_ready(stack, token)
    call(service, token, "request_payment", **grocery_order(30))
    mandate = rpc(service, token, "resources/read", {"uri": "wallet://mandate/current"}).json()[
        "result"
    ]["contents"][0]
    assert mandate["mimeType"] == "application/json"
    assert json.loads(mandate["text"])["mandate"]["mandate_id"] == mandate_id
    window = rpc(service, token, "resources/read", {"uri": "wallet://ledger/window"}).json()[
        "result"
    ]["contents"][0]
    assert json.loads(window["text"])["approved_spend_chf"] == "30.00"


def test_revoking_on_the_phone_cuts_the_token_the_errand_and_the_mandate(
    stack: dict[str, Any],
) -> None:
    service, phone = stack["service"], stack["phone"]
    agent = connect(phone, agent="Claude")
    token = agent["token"]
    mandate_id = mandate_ready(stack, token)
    call(service, token, "request_payment", **grocery_order(30))

    r = phone.post(f"/api/agents/{agent['client_id']}/revoke")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tokens_revoked"] == 1
    assert body["connector"]["mandates_revoked"] == [mandate_id]
    assert len(body["connector"]["errands_stopped"]) == 1

    # The agent's next call: a clean 401, not a crash and not a decision.
    STATE.verifier.forget()  # type: ignore[union-attr]  # the positive cache would otherwise answer for 3 s
    refused = rpc(service, token, "tools/call", {"name": "whats_allowed", "arguments": {}})
    assert (
        refused.status_code == 401
        and 'error="invalid_token"' in refused.headers["www-authenticate"]
    )
    assert service.get(f"/v1/mandates/{mandate_id}").json()["status"] == "revoked"
    # …and the list the cockpit reads says so too, not the "active" cached at confirm.
    listed = {m["mandate_id"]: m["status"] for m in service.get("/v1/mandates").json()["mandates"]}
    assert listed[mandate_id] == "revoked"
    assert service.get("/v1/runs").json()["runs"][0]["status"] == "stopped"
    assert service.get("/connector/agents").json()["agents"][0]["stopped_reason"]


def test_a_restart_forgets_nothing_the_agent_needs(
    stack: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proposals outlive the process (spec §5): after a restart the mandate is back,
    active, with its compiled rules, before the agent's next order — and the phone lists it."""
    service, phone, supervisor = stack["service"], stack["phone"], stack["supervisor"]

    def restart() -> None:
        STATE.reset()
        STATE.verifier = IntrospectionVerifier("http://app.test", SECRET, http=phone)

    monkeypatch.setenv("LEASH_CONNECTOR_STATE", str(tmp_path / "connector.json"))
    restart()
    token = connect(phone)["token"]
    mandate_id = mandate_ready(stack, token)
    assert (
        call(service, token, "request_payment", **grocery_order(30))["structuredContent"][
            "decision"
        ]
        == "approve"
    )

    restart()  # a new Connector on the same file …
    supervisor.mandates.clear()  # … and a Supervisor that remembers nothing
    assert service.get("/v1/mandates").json()["mandates"] == []
    allowed = call(service, token, "whats_allowed")["structuredContent"]
    assert allowed["mandate"]["mandate_id"] == mandate_id
    assert allowed["mandate"]["rules"]  # the compiled rules came back with the proposal
    listed = service.get("/v1/mandates").json()["mandates"]
    assert [(m["mandate_id"], m["status"]) for m in listed] == [(mandate_id, "active")]
    again = call(service, token, "request_payment", **grocery_order(25))["structuredContent"]
    assert again["decision"] == "approve" and again["window"]["approved_spend_chf"] == "25.00"


def test_a_newer_confirmation_supersedes_the_running_errand(stack: dict[str, Any]) -> None:
    service, phone = stack["service"], stack["phone"]
    token = connect(phone)["token"]
    first = mandate_ready(stack, token)
    call(service, token, "request_payment", **grocery_order(30))
    second = mandate_ready(stack, token)
    assert second != first
    result = call(service, token, "request_payment", **grocery_order(30))["structuredContent"]
    assert result["window"]["approved_spend_chf"] == "30.00"  # a fresh ledger under the new mandate
    runs = service.get("/v1/runs").json()["runs"]
    assert sorted(r["status"] for r in runs) == ["running", "stopped"]
