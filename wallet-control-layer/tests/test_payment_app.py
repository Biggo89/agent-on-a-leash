"""The mock Payment App as an OAuth 2.1 authorization server.

What a connector client relies on: discovery, dynamic registration, the PKCE code flow with
its refusals, refresh rotation, introspection and revocation — and the one rule that matters
most, that an error before the redirect URI is trusted is a page, never a redirect.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from payment_app.server import PERSONAS, STORE, WIRING
from payment_app.server import app as phone_app

REDIRECT = "http://localhost:9999/callback"
SECRET = "dev-introspection-secret"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.fixture()
def phone(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    STORE.reset(None)
    monkeypatch.setenv("WALLET_INTROSPECTION_SECRET", SECRET)
    monkeypatch.setenv("LEASH_SERVICE_URL", "http://stub")
    stub = FastAPI()

    @stub.post("/connector/agents/{client_id}/revoke")
    def revoke(client_id: str) -> dict[str, Any]:
        return {"client_id": client_id, "errands_stopped": ["AG1"], "mandates_revoked": ["TM1"]}

    WIRING.service_http = TestClient(stub, base_url="http://stub")
    with TestClient(phone_app, base_url="http://app.test") as c:
        yield c
    WIRING.service_http = None
    STORE.reset(None)


def register(phone: TestClient, *, method: str = "none", name: str = "Claude") -> dict[str, Any]:
    r = phone.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"],
        },
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def sign_in(phone: TestClient, next_url: str = "/") -> None:
    r = phone.post(
        "/login",
        data={"subject": PERSONAS[0].customer_id, "pin": "0000", "next": next_url},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert "wallet_session" in r.cookies


def authorize_params(client: dict[str, Any], challenge: str, **extra: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT,
        "state": "s-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": "http://engine.test/mcp",
        **extra,
    }


def consent(
    phone: TestClient, params: dict[str, str], decision: str = "approve"
) -> dict[str, list[str]]:
    r = phone.post("/authorize", data={**params, "decision": decision}, follow_redirects=False)
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert location.startswith(REDIRECT + "?")
    return parse_qs(urlsplit(location).query)


def connect(phone: TestClient, **kw: Any) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Register, sign in, consent, exchange. Returns (client, token body, verifier)."""
    client = register(phone, **kw)
    verifier, challenge = pkce()
    params = authorize_params(client, challenge)
    sign_in(phone, "/authorize?" + urlencode(params))
    query = consent(phone, params)
    r = phone.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client["client_id"],
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    return client, r.json(), verifier


# --------------------------------------------------------------------- discovery


def test_discovery_names_every_endpoint_under_the_issuer(phone: TestClient) -> None:
    doc = phone.get("/.well-known/oauth-authorization-server").json()
    assert doc["issuer"] == "http://app.test"
    for key in ("authorization", "token", "registration", "introspection", "revocation"):
        assert doc[f"{key}_endpoint"].startswith("http://app.test/")
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert "none" in doc["token_endpoint_auth_methods_supported"]
    assert set(doc["scopes_supported"]) == {"mandate:propose", "payment:request", "activity:read"}


def test_registration_is_one_call_and_public_clients_get_no_secret(phone: TestClient) -> None:
    public = register(phone, method="none")
    assert public["client_id"].startswith("agent_") and "client_secret" not in public
    confidential = register(phone, method="client_secret_post")
    assert confidential["client_secret"]


def test_registration_echoes_only_what_was_registered(phone: TestClient) -> None:
    """Claude Code validates the registration response as URLs-or-absent: a `client_uri`
    echoed as "" made it refuse the whole flow (2026-09-24)."""
    without = register(phone)
    assert "client_uri" not in without
    r = phone.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "client_uri": "https://claude.ai", "client_name": "C"},
    )
    assert r.status_code == 201 and r.json()["client_uri"] == "https://claude.ai"
    r = phone.post("/register", json={"redirect_uris": [REDIRECT], "client_uri": "not a url"})
    assert r.status_code == 201 and "client_uri" not in r.json()


def test_registration_refuses_a_redirect_that_is_not_local_http(phone: TestClient) -> None:
    r = phone.post("/register", json={"redirect_uris": ["http://evil.example/cb"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
    r = phone.post("/register", json={"redirect_uris": []})
    assert r.status_code == 400


# --------------------------------------------------------------------- authorize


def test_an_unknown_client_gets_a_page_never_a_redirect(phone: TestClient) -> None:
    _, challenge = pkce()
    r = phone.get(
        "/authorize",
        params=authorize_params({"client_id": "agent_nope"}, challenge),
        follow_redirects=False,
    )
    assert r.status_code == 400 and "text/html" in r.headers["content-type"]


def test_an_unregistered_redirect_uri_gets_a_page_never_a_redirect(phone: TestClient) -> None:
    client = register(phone)
    _, challenge = pkce()
    params = authorize_params(client, challenge, redirect_uri="http://localhost:9999/other")
    r = phone.get("/authorize", params=params, follow_redirects=False)
    assert r.status_code == 400


def test_pkce_is_required(phone: TestClient) -> None:
    client = register(phone)
    params = authorize_params(client, "")
    r = phone.get("/authorize", params=params, follow_redirects=False)
    assert r.status_code == 303
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["error"] == ["invalid_request"] and query["state"] == ["s-123"]


def test_only_agent_scopes_can_be_asked_for(phone: TestClient) -> None:
    client = register(phone)
    _, challenge = pkce()
    r = phone.get(
        "/authorize",
        params=authorize_params(client, challenge, scope="stepup:resolve"),
        follow_redirects=False,
    )
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["invalid_scope"]


def test_signed_out_sees_login_then_consent_then_a_code(phone: TestClient) -> None:
    client = register(phone, name="Claude")
    verifier, challenge = pkce()
    params = authorize_params(client, challenge)
    first = phone.get("/authorize", params=params)
    # Signed out, the splash — whose one tap leads to the sign-in and back here.
    assert first.status_code == 200 and "Tap to continue" in first.text
    # The way back carries the whole authorize URL, encoded, so nothing of it is lost.
    href = html.unescape(re.search(r'href="([^"]*/login\?[^"]+)"', first.text).group(1))  # type: ignore[union-attr]
    back = parse_qs(urlsplit(href).query)["next"][0]
    assert back.startswith("/authorize?")
    assert {k: v[0] for k, v in parse_qs(urlsplit(back).query).items()} == params
    assert "Sign in" in phone.get("/login").text
    sign_in(phone, "/authorize?" + urlencode(params))
    page = phone.get("/authorize", params=params)
    assert page.status_code == 200
    # The onboarding, every step on the page, the consent at the end of it.
    for step in (
        "Leash protects your payments",
        "Step 1 of 5",
        "How you usually pay",
        "This is your suggested leash",
        "What should Leash do for you?",
        "Your leash is ready",
        "Confirm Leash",
    ):
        assert step in page.text, step
    assert 'data-start="intro"' in page.text
    assert "<b>Claude</b> may search, compare and pay" in page.text
    assert "Claude can never approve its own requests" in page.text
    assert PERSONAS[0].last4 in page.text
    pattern = PERSONAS[0].pattern
    assert pattern["sentence"] in page.text
    assert f"CHF {pattern['typical_low']} to {pattern['typical_high']}" in page.text
    assert f"Allow up to CHF {pattern['suggested_cap_chf']} per purchase" in page.text
    query = consent(phone, params)
    assert query["code"] and query["state"] == ["s-123"]


def test_the_choices_stay_with_the_grant_and_the_second_agent_skips_to_the_consent(
    phone: TestClient,
) -> None:
    client = register(phone, name="Claude")
    _, challenge = pkce()
    params = authorize_params(client, challenge)
    sign_in(phone, "/authorize?" + urlencode(params))
    setup = {"used_history": True, "suggested_cap_chf": 100, "choices": {"A": "allow", "B": "ask"}}
    r = phone.post(
        "/authorize",
        data={**params, "decision": "approve", "setup": json.dumps(setup)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "code=" in r.headers["location"]
    me = phone.get("/api/me").json()
    assert me["setup"] == {
        "used_history": True,
        "suggested_cap_chf": 100,
        "choices": {"A": "allow", "B": "ask"},
    }
    assert me["onboarded_at"] and me["pattern"]["suggested_cap_chf"] == 100
    assert me["limits"]["monthly_limit_chf"] == PERSONAS[0].monthly_limit_chf
    # A second agent for the same cardholder: the onboarding is done, the page opens on
    # the consent itself.
    other = register(phone, name="ChatGPT")
    _, challenge2 = pkce()
    again = phone.get("/authorize", params=authorize_params(other, challenge2))
    assert 'data-start="ready"' in again.text and "<b>ChatGPT</b> may search" in again.text
    # Garbage in the setup field is ignored, never a failure of the consent.
    r = phone.post(
        "/authorize",
        data={**authorize_params(other, challenge2), "decision": "approve", "setup": "{not json"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "code=" in r.headers["location"]


def test_not_now_denies_without_a_code(phone: TestClient) -> None:
    client = register(phone)
    _, challenge = pkce()
    params = authorize_params(client, challenge)
    sign_in(phone)
    query = consent(phone, params, decision="deny")
    assert query["error"] == ["access_denied"] and "code" not in query
    assert phone.get("/api/agents").json()["agents"] == []


def test_login_never_redirects_off_this_app(phone: TestClient) -> None:
    r = phone.post(
        "/login",
        data={"subject": PERSONAS[0].customer_id, "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"
    r = phone.post(
        "/login",
        data={"subject": PERSONAS[0].customer_id, "next": "//evil.example/"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


# --------------------------------------------------------------------- token


def test_the_code_flow_issues_a_token_the_connector_can_introspect(phone: TestClient) -> None:
    client, tokens, _ = connect(phone)
    assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]
    assert set(tokens["scope"].split()) == {"mandate:propose", "payment:request", "activity:read"}
    claims = phone.post(
        "/introspect",
        data={"token": tokens["access_token"]},
        headers={"Authorization": f"Bearer {SECRET}"},
    ).json()
    assert claims["active"] is True
    assert claims["client_id"] == client["client_id"] and claims["client_name"] == "Claude"
    assert claims["sub"] == PERSONAS[0].customer_id
    assert claims["card_id"] == PERSONAS[0].card_id
    assert claims["device_id"] == PERSONAS[0].device_id
    assert claims["aud"] == "http://engine.test/mcp"
    assert "refresh_token" not in claims


def test_a_wrong_verifier_is_refused_and_the_code_is_spent(phone: TestClient) -> None:
    client = register(phone)
    verifier, challenge = pkce()
    params = authorize_params(client, challenge)
    sign_in(phone)
    code = consent(phone, params)["code"][0]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client["client_id"],
        "code_verifier": "not-the-verifier",
    }
    r = phone.post("/token", data=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # Single use: even the right verifier cannot rescue a spent code.
    r = phone.post("/token", data={**body, "code_verifier": verifier})
    assert r.status_code == 400


def test_a_confidential_client_must_present_its_secret(phone: TestClient) -> None:
    client = register(phone, method="client_secret_basic")
    verifier, challenge = pkce()
    params = authorize_params(client, challenge)
    sign_in(phone)
    code = consent(phone, params)["code"][0]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client["client_id"],
        "code_verifier": verifier,
    }
    assert phone.post("/token", data=body).status_code == 401
    basic = base64.b64encode(f"{client['client_id']}:{client['client_secret']}".encode()).decode()
    r = phone.post("/token", data=body, headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200, r.text


def test_refresh_rotates_the_pair(phone: TestClient) -> None:
    client, tokens, _ = connect(phone)
    r = phone.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
        },
    )
    assert r.status_code == 200
    fresh = r.json()
    assert fresh["access_token"] != tokens["access_token"]
    old = phone.post(
        "/introspect",
        data={"token": tokens["access_token"]},
        headers={"Authorization": f"Bearer {SECRET}"},
    ).json()
    assert old == {"active": False}
    again = phone.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
        },
    )
    assert again.status_code == 400  # the old refresh token died with the rotation


# --------------------------------------------------------------------- introspect · revoke


def test_introspection_needs_the_shared_secret(phone: TestClient) -> None:
    _, tokens, _ = connect(phone)
    r = phone.post("/introspect", data={"token": tokens["access_token"]})
    assert r.status_code == 401
    r = phone.post(
        "/introspect",
        data={"token": "nope"},
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert r.json() == {"active": False}


def test_revocation_endpoint_kills_the_token(phone: TestClient) -> None:
    _, tokens, _ = connect(phone)
    assert phone.post("/revoke", data={"token": tokens["access_token"]}).status_code == 200
    claims = phone.post(
        "/introspect",
        data={"token": tokens["access_token"]},
        headers={"Authorization": f"Bearer {SECRET}"},
    ).json()
    assert claims == {"active": False}
    assert phone.post("/revoke", data={"token": "unknown"}).status_code == 200


def test_the_phone_lists_connected_agents_and_revokes_them_everywhere(phone: TestClient) -> None:
    client, tokens, _ = connect(phone, name="Claude")
    agents = phone.get("/api/agents").json()["agents"]
    assert [a["agent_name"] for a in agents] == ["Claude"] and agents[0]["tokens"] == 1
    assert any(s["scope"] == "payment:request" for s in agents[0]["scopes"])

    r = phone.post(f"/api/agents/{client['client_id']}/revoke")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tokens_revoked"] == 1
    assert body["connector"]["errands_stopped"] == ["AG1"]  # the service was told
    assert phone.get("/api/agents").json()["agents"] == []
    claims = phone.post(
        "/introspect",
        data={"token": tokens["access_token"]},
        headers={"Authorization": f"Bearer {SECRET}"},
    ).json()
    assert claims == {"active": False}
    # And the refresh token cannot bring it back.
    r = phone.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client["client_id"],
        },
    )
    assert r.status_code == 400


def test_two_cardholders_can_connect_the_same_agent_and_revoke_apart(phone: TestClient) -> None:
    """A registered client is one agent installation; two cardholders may connect it. The
    second consent must not overwrite the first (2026-09-24: Claude Code's authorize URL was
    opened in two browsers, one per persona), and Revoke cuts one cardholder's tokens only."""
    client, first_tokens, _ = connect(phone, name="Claude Code")
    verifier, challenge = pkce()
    params = authorize_params(client, challenge)
    r = phone.post(
        "/login",
        data={"subject": PERSONAS[1].customer_id, "pin": "0000", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    query = consent(phone, params)
    second = phone.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client["client_id"],
            "code_verifier": verifier,
        },
    ).json()
    mine = phone.get("/api/agents").json()["agents"]  # signed in as the second cardholder
    assert [(a["agent_name"], a["tokens"]) for a in mine] == [("Claude Code", 1)]
    r = phone.post(f"/api/agents/{client['client_id']}/revoke")
    assert r.status_code == 200 and r.json()["tokens_revoked"] == 1
    assert phone.get("/api/agents").json()["agents"] == []
    introspect = lambda token: phone.post(  # noqa: E731
        "/introspect", data={"token": token}, headers={"Authorization": f"Bearer {SECRET}"}
    ).json()
    assert introspect(second["access_token"]) == {"active": False}
    first = introspect(first_tokens["access_token"])
    assert first["active"] is True and first["sub"] == PERSONAS[0].customer_id


def test_home_is_the_splash_and_then_the_cockpit(phone: TestClient) -> None:
    r = phone.get("/")
    assert (
        r.status_code == 200
        and "Tap to continue" in r.text
        and "You still hold the leash" in r.text
    )
    sign_in(phone)
    page = phone.get("/")
    assert page.status_code == 200
    assert PERSONAS[0].name in page.text and "/static/cockpit.js" in page.text
    assert '"icons"' in page.text  # the one icon set, handed to the script


def test_the_screens_are_the_leash_brands(phone: TestClient) -> None:
    """Spec C3: the app owns the LEASH brand (Logo-System v1.1) — its tokens, its marks — and
    borrows nothing from the leash demo any more."""
    css = phone.get("/static/leash.css")
    assert css.status_code == 200
    for token in (
        "--midnight: #15192C",
        "--orange: #E28E34",
        "--warm: #F7F5F0",
        "--slate: #62697A",
    ):
        assert token in css.text, token
    rules = re.sub(r"/\*.*?\*/", "", css.text, flags=re.S)  # the comments say "no green"
    assert "green" not in rules.lower()
    for script in ("/static/cockpit.js", "/static/onboarding.js"):
        assert phone.get(script).status_code == 200
    assert phone.get("/leash-src/ui/theme.css").status_code == 404
    login = phone.get("/login")
    assert (
        "/static/leash.css" in login.text and "fonts.googleapis.com/css2?family=Inter" in login.text
    )
    # The signet: the master cut's grip is orange, the small cut carries the heavier stroke.
    splash = phone.get("/")
    assert 'stroke="#E28E34"' in splash.text and 'aria-label="LEASH"' in splash.text
    assert 'stroke-width="24.16"' in login.text  # the small cut in the header
