#!/usr/bin/env python3
"""Does a real MCP client get onto the leash? The official Python SDK, end to end.

Runs the whole chain an agent host runs — the 401, the two discovery documents, dynamic
registration, PKCE consent on the phone (driven here by a script standing in for the
cardholder), the token, then `initialize`, `tools/list`, and a shopping errand — against a
running `make connect`. It is the check that the hand-rolled transport in
`leash/connector/mcp.py` speaks the protocol a client actually speaks, not the one we read.

    make sandbox                 # terminal 1
    make connect                 # terminal 2
    make probe-connector         # terminal 3 — this

The SDK is not a project dependency (the connector needs none); `uv run --with mcp` fetches
it for this run only.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

BASE = os.environ.get("LEASH_PROBE_URL", "http://127.0.0.1:8010").rstrip("/")
MCP_URL = f"{BASE}/mcp"
APP_URL = f"{BASE}/app"
GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def ok(text: str) -> None:
    print(f"  {GREEN}✓{OFF} {text}")


def fail(text: str) -> None:
    print(f"  {RED}✗{OFF} {text}")
    raise SystemExit(2)


async def main() -> int:
    try:
        import httpx2
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
    except ImportError as exc:
        fail(f"{exc} — run with: uv run --with mcp python tools/connector_probe.py")

    print(f"{BOLD}Connector probe{OFF} → {MCP_URL}")

    class Memory:
        """TokenStorage: what the SDK asks us to keep between calls."""

        tokens: Any = None
        client: Any = None

        async def get_tokens(self) -> Any:
            return self.tokens

        async def set_tokens(self, tokens: Any) -> None:
            self.tokens = tokens

        async def get_client_info(self) -> Any:
            return self.client

        async def set_client_info(self, info: Any) -> None:
            self.client = info

    captured: dict[str, str | None] = {}

    async def cardholder_consents(url: str) -> None:
        """The SDK hands us the authorize URL a browser would open. Play the cardholder."""
        async with httpx2.AsyncClient(follow_redirects=False) as browser:
            page = await browser.get(url)
            if "Tap to continue" not in page.text:
                fail(f"expected the LEASH splash at {url}, got HTTP {page.status_code}")
            login = await browser.get(f"{APP_URL}/login")
            if "Sign in" not in login.text:
                fail(f"expected the sign-in screen, got HTTP {login.status_code}")
            subject = re.search(r'name="subject" value="([^"]+)"', login.text)
            if not subject:
                fail("no persona on the sign-in screen")
            query = urlsplit(url).query
            signed = await browser.post(
                f"{APP_URL}/login",
                data={
                    "subject": subject.group(1),
                    "pin": "0000",
                    "next": f"/app/authorize?{query}",
                },
            )
            if signed.status_code != 303:
                fail(f"login answered HTTP {signed.status_code}")
            consent = await browser.get(url)
            if "Confirm Leash" not in consent.text:
                fail("the onboarding did not render")
            title = re.search(r'consent__ttl">([^<]+)<', consent.text)
            ok(f"onboarding shown, ending on: {title.group(1) if title else 'Confirm Leash'}")
            params = dict(parse_qsl(query))
            answer = await browser.post(
                f"{APP_URL}/authorize", data={**params, "decision": "approve"}
            )
            if answer.status_code != 303 or "code=" not in answer.headers.get("location", ""):
                fail(f"consent did not redirect with a code (HTTP {answer.status_code})")
            back = parse_qs(urlsplit(answer.headers["location"]).query)
            captured["code"] = back["code"][0]
            captured["state"] = back.get("state", [None])[0]
            ok("cardholder tapped Connect; the client got its code")

    async def callback() -> Any:
        return AuthorizationCodeResult(code=str(captured["code"]), state=captured["state"])

    provider = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Connector probe",
            redirect_uris=["http://localhost:3030/callback"],  # type: ignore[list-item]
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=Memory(),
        redirect_handler=cardholder_consents,
        callback_handler=callback,
    )

    async with (
        httpx2.AsyncClient(auth=provider, timeout=60) as http,
        streamable_http_client(MCP_URL, http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        init = await session.initialize()
        ok(
            f"initialized: {init.server_info.name} {init.server_info.version}, "
            f"protocol {init.protocol_version}"
        )
        if "never approve your own requests" not in (init.instructions or ""):
            fail("the leash instructions are missing from initialize")
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        ok(f"tools: {', '.join(names)}")
        for verb in ("confirm", "resolve", "amend", "revoke"):
            if any(verb in n for n in names):
                fail(f"an agent must never see a '{verb}' tool")

        allowed = await session.call_tool("whats_allowed", {})
        ok(f"whats_allowed → {DIM}{allowed.content[0].text.splitlines()[0]}{OFF}")  # type: ignore[union-attr]

        scenarios = await http.get(f"{BASE}/v1/scenarios")
        household = next(
            s for s in scenarios.json()["scenarios"] if s["name"] == "Household budget"
        )
        draft = await session.call_tool(
            "propose_mandate", {"instruction": household["instruction"]}
        )
        if draft.is_error:
            fail(f"propose_mandate: {draft.content[0].text}")  # type: ignore[union-attr]
        draft_id = draft.structured_content["draft_id"]  # type: ignore[index]
        ok(f"proposed mandate {draft_id}; waiting on the phone")

        confirmed = await http.post(
            f"{BASE}/v1/mandates/{draft_id}/confirm", json={"confirmed": True}
        )
        if confirmed.status_code != 200:
            fail(f"confirm: HTTP {confirmed.status_code} {confirmed.text}")
        ok("cardholder confirmed it in the app")

        allowed = await session.call_tool("whats_allowed", {})
        shops = allowed.structured_content["known_shops"]  # type: ignore[index]
        grocer = next((s for s in shops if s["merchant_category"] == "groceries"), shops[0])
        order = {
            "merchant": grocer["merchant_name"],
            "items": [{"name": "Fresh produce selection", "quantity": 1, "unit_price": 42.5}],
            "delivery_fee": 5,
            "description": "weekly groceries",
        }
        paid = await session.call_tool("request_payment", order)
        text = paid.content[0].text  # type: ignore[union-attr]
        ok(f"request_payment → {DIM}{text.splitlines()[0]}{OFF}")
        if paid.is_error or not text.startswith(("APPROVED", "DECLINED", "PENDING")):
            fail("request_payment did not answer with a decision")
        auth_id = paid.structured_content["authorization_id"]  # type: ignore[index]
        status = await session.call_tool("payment_status", {"authorization_id": auth_id})
        ok(f"payment_status → {DIM}{status.content[0].text}{OFF}")  # type: ignore[union-attr]

        over = await session.call_tool(
            "request_payment",
            {**order, "items": [{"name": "Weekly grocery basket", "unit_price": 130}]},
        )
        text = over.content[0].text  # type: ignore[union-attr]
        if not text.startswith("DECLINED"):
            fail(f"an order over the cap should be declined, got: {text[:80]}")
        ok(f"over the cap → {DIM}{text.splitlines()[0][:110]}{OFF}")

        recent = await session.call_tool("recent_activity", {"limit": 5})
        ok(f"recent_activity → {len(recent.structured_content['orders'])} orders")  # type: ignore[index]
        resource = await session.read_resource("wallet://ledger/window")  # type: ignore[arg-type]
        ok(f"resource wallet://ledger/window → {DIM}{resource.contents[0].text[:80]}{OFF}")  # type: ignore[union-attr]

    print(f"\n{GREEN}{BOLD}The official client got onto the leash.{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
