"""The connector's HTTP surface, mounted on the decision service.

    POST /mcp                                     the agent's calls (bearer token required)
    GET  /.well-known/oauth-protected-resource     where a client learns who issues tokens
    GET  /connector/proposals, /connector/agents   what the cardholder's phone renders
    POST /connector/agents/{client_id}/revoke      the phone pulling the leash

The discovery chain is the MCP authorization specification's, so any client finds its way
with no configuration: an unauthenticated POST gets a 401 whose ``WWW-Authenticate`` header
names the protected-resource document (RFC 9728); that document names the Payment App as the
authorization server; the client reads the app's metadata (RFC 8414), registers itself
(RFC 7591), runs the PKCE authorization-code flow, and comes back with a bearer token that this
module verifies by introspection (RFC 7662).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..runtime.supervisor import Supervisor, SupervisorError
from . import mcp
from .auth import (
    AGENT_SCOPES,
    AuthorizationServerUnavailable,
    IntrospectionVerifier,
    Principal,
    TokenVerifier,
    Unauthenticated,
    bearer_token,
)
from .errand import Connector

log = logging.getLogger("leash.connector")

DEFAULT_APP_URL = "http://127.0.0.1:8081"
DEFAULT_INTROSPECTION_SECRET = "dev-introspection-secret"
REALM = "wallet-control-layer"


def tracing() -> bool:
    """`LEASH_CONNECTOR_TRACE=1`: log what a client sends — the headers, then each method,
    with the `initialize` parameters — so a difference from the reference client is one log
    line rather than a debugging session. Off by default; a token is never logged."""
    return os.environ.get("LEASH_CONNECTOR_TRACE", "").strip().lower() not in ("", "0", "false")


def trace_request(request: Request) -> None:
    h = request.headers
    log.info(
        "mcp <- %s accept=%r content-type=%r mcp-protocol-version=%r mcp-session-id=%r "
        "origin=%r user-agent=%r",
        request.client.host if request.client else "?",
        h.get("accept"),
        h.get("content-type"),
        h.get("mcp-protocol-version"),
        h.get("mcp-session-id"),
        h.get("origin"),
        (h.get("user-agent") or "")[:80],
    )


def trace_messages(message: Any) -> None:
    for m in message if isinstance(message, list) else [message]:
        if not isinstance(m, dict):
            continue
        raw = m.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        detail = ""
        if m.get("method") == "initialize":
            detail = (
                f" protocolVersion={params.get('protocolVersion')!r}"
                f" clientInfo={params.get('clientInfo')!r}"
                f" capabilities={sorted((params.get('capabilities') or {}).keys())}"
            )
        elif m.get("method") == "tools/call":
            detail = f" tool={params.get('name')!r}"
        log.info("mcp <- %s id=%r%s", m.get("method") or "(response)", m.get("id"), detail)


class RpcResponse(JSONResponse):
    """JSON with `default=str`: a tool answer may carry Money or a datetime."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, default=str, ensure_ascii=False).encode("utf-8")


class ConnectorState:
    """Process-wide wiring: the verifier and the registry, bound to whichever Supervisor the
    service currently hands out (the tests replace it per test)."""

    def __init__(self) -> None:
        self.verifier: TokenVerifier | None = None
        self._connector: Connector | None = None
        self._lock = threading.Lock()

    def connector(self, supervisor: Supervisor) -> Connector:
        with self._lock:
            if self._connector is None or self._connector.supervisor is not supervisor:
                self._connector = Connector(supervisor)
            return self._connector

    def verifier_or_default(self) -> TokenVerifier:
        with self._lock:
            if self.verifier is None:
                self.verifier = IntrospectionVerifier(
                    os.environ.get("WALLET_APP_URL", DEFAULT_APP_URL),
                    os.environ.get("WALLET_INTROSPECTION_SECRET", DEFAULT_INTROSPECTION_SECRET),
                )
            return self.verifier

    def reset(self) -> None:
        with self._lock:
            self.verifier = None
            self._connector = None


STATE = ConnectorState()


# ------------------------------------------------------------------ urls


def public_base(request: Request) -> str:
    """The URL the outside world reaches this service at.

    `LEASH_PUBLIC_URL` when set (a tunnel whose hostname the process cannot see), else the
    request's own base — which honours `X-Forwarded-Proto`, so behind a tunnel it is https.
    """
    configured = os.environ.get("LEASH_PUBLIC_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def authorization_server(request: Request) -> str:
    """Where tokens come from. `WALLET_APP_MOUNT` means the app is mounted on this very
    process (tools/connect.py), so it shares the public base."""
    mount = os.environ.get("WALLET_APP_MOUNT", "").strip()
    if mount:
        return public_base(request) + "/" + mount.strip("/")
    return os.environ.get("WALLET_APP_URL", DEFAULT_APP_URL).rstrip("/")


def resource_metadata(request: Request) -> dict[str, Any]:
    base = public_base(request)
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [authorization_server(request)],
        "scopes_supported": list(AGENT_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Leash Wallet",
        "resource_documentation": f"{base}/docs",
    }


def _origin_allowed(request: Request) -> bool:
    """DNS-rebinding guard the MCP transport asks for on local servers. Server-side clients
    send no Origin; a browser must be on this host."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = (urlsplit(origin).hostname or "").lower()
    own = (request.url.hostname or "").lower()
    return host in ("localhost", "127.0.0.1", own)


# ------------------------------------------------------------------ auth


def authenticate(request: Request) -> Principal:
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        raise Unauthenticated("a bearer token is required", error=None)
    principal = STATE.verifier_or_default().verify(token)
    if principal is None:
        raise Unauthenticated("the token is invalid, expired or revoked")
    return principal


def unauthorized(request: Request, exc: Unauthenticated) -> JSONResponse:
    base = public_base(request)
    challenge = [f'Bearer realm="{REALM}"']
    if exc.error:
        challenge.append(f'error="{exc.error}"')
        challenge.append(f'error_description="{exc.description}"')
    challenge.append(f'resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"')
    return JSONResponse(
        status_code=401,
        content={"error": {"code": exc.error or "unauthenticated", "message": exc.description}},
        headers={"WWW-Authenticate": ", ".join(challenge)},
    )


# ------------------------------------------------------------------ routes


def router(get_supervisor: Callable[[], Supervisor]) -> APIRouter:
    r = APIRouter()

    @r.get("/.well-known/oauth-protected-resource", tags=["connector"])
    @r.get("/.well-known/oauth-protected-resource/mcp", tags=["connector"])
    def protected_resource(request: Request) -> dict[str, Any]:
        """RFC 9728: who may vouch for a token presented to /mcp, and which scopes exist."""
        return resource_metadata(request)

    @r.post("/mcp", tags=["connector"])
    async def mcp_post(request: Request) -> Response:
        """One JSON-RPC message or a batch. Every call is authenticated; the token's scopes
        decide which tools answer."""
        if tracing():
            trace_request(request)
        if not _origin_allowed(request):
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "origin_refused", "message": "cross-site origin"}},
            )
        try:
            # Off the event loop: introspection is a blocking call, and when the app is
            # mounted on this very process it is served by this very loop — verified
            # 2026-09-24 with the official client, which got a 503 until this moved.
            principal = await run_in_threadpool(authenticate, request)
        except Unauthenticated as exc:
            return unauthorized(request, exc)
        except AuthorizationServerUnavailable as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "authorization_server_unavailable",
                        "message": f"cannot verify the token: {exc}",
                    }
                },
                headers={"Retry-After": "2"},
            )
        raw = await request.body()
        try:
            message = json.loads(raw or b"")
        except ValueError:
            return RpcResponse(
                status_code=400, content=mcp.RpcError(mcp.PARSE_ERROR, "invalid JSON").body(None)
            )
        if tracing():
            trace_messages(message)
        connector = STATE.connector(get_supervisor())
        # The handlers are synchronous and may call upstream; keep the event loop free.
        answers = await run_in_threadpool(_handle_all, message, connector, principal)
        if not answers:
            return Response(status_code=202)
        if isinstance(message, list):
            return RpcResponse(content=answers)
        return RpcResponse(content=answers[0])

    @r.get("/mcp", tags=["connector"], include_in_schema=False)
    @r.delete("/mcp", tags=["connector"], include_in_schema=False)
    def mcp_other(request: Request) -> Response:
        """This server never opens a stream of its own and keeps no session to end."""
        if tracing():
            log.info(
                "mcp <- %s %s accept=%r (answered 405)",
                request.method,
                request.url.path,
                request.headers.get("accept"),
            )
        return Response(status_code=405, headers={"Allow": "POST"})

    @r.get("/connector/status", tags=["connector"])
    def status(request: Request) -> dict[str, Any]:
        """For the phone's badge and the operator: where things are and what is connected."""
        connector = STATE.connector(get_supervisor())
        return {
            "mcp_url": f"{public_base(request)}/mcp",
            "authorization_server": authorization_server(request),
            "resource_metadata": f"{public_base(request)}/.well-known/oauth-protected-resource/mcp",
            "tools": [t.name for t in mcp.TOOLS],
            "scopes": mcp.scope_catalogue(),
            "agents": len(connector.agents()),
            "proposals": len(connector.proposals),
        }

    @r.get("/connector/proposals", tags=["connector"])
    def proposals() -> dict[str, Any]:
        """Which agent drafted which mandate — so the phone can say 'proposed by Claude'."""
        return {"proposals": STATE.connector(get_supervisor()).proposals_view()}

    @r.get("/connector/agents", tags=["connector"])
    def agents() -> dict[str, Any]:
        """Every errand this process has run: its window, its counters, what is waiting."""
        return {"agents": STATE.connector(get_supervisor()).agents()}

    @r.post("/connector/agents/{client_id}/revoke", tags=["connector"])
    def revoke(client_id: str, subject: str | None = None) -> dict[str, Any]:
        """The phone pulled the leash. Stops the errand and revokes the mandate — for one
        cardholder when `subject` is given, since two can have connected the same agent; the
        app revokes the token on its side, and the agent's next call is refused."""
        result = STATE.connector(get_supervisor()).revoke_agent(client_id, subject=subject)
        if not result["errands_stopped"] and not result["mandates_revoked"]:
            if result["errors"]:
                raise SupervisorError(502, "revoke_failed", "; ".join(result["errors"]))
            raise SupervisorError(404, "not_found", f"nothing is connected as {client_id}")
        return result

    return r


def _handle_all(message: Any, connector: Connector, principal: Principal) -> list[dict[str, Any]]:
    batch = message if isinstance(message, list) else [message]
    if isinstance(message, list) and not batch:
        return [mcp.RpcError(mcp.INVALID_REQUEST, "empty batch").body(None)]
    answers = [mcp.handle(m, connector, principal) for m in batch]
    return [a for a in answers if a is not None]
