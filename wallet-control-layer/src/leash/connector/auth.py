"""Who is calling the connector, and what they may do.

The agent host presents a bearer token that the Payment App issued when the cardholder
connected the agent. The connector never sees a password and never mints a token: it asks the
app whether the token is live (RFC 7662 token introspection) and reads the answer — the
cardholder, the card, the agent, the scopes. Introspection rather than a self-contained signed
token because revocation has to be immediate: *Revoke* on the phone kills the token at its
source and the agent's very next call is refused, with no key material to distribute and no
denylist to keep in sync. A few seconds of caching keep it from costing a round trip per call.

The scopes are the connector-level form of "no LLM in the decision path" (AGENTS.md §3). The
token an agent holds can carry only the three scopes below. The scopes that confirm a mandate,
answer a step-up or amend a limit do not exist for an agent — they are the app's own session —
so the consent sentence "the agent can never approve its own requests" is a property of the
grammar, not a promise. specs/connector.md §2.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger("leash.connector")

SCOPE_PROPOSE = "mandate:propose"
SCOPE_PAY = "payment:request"
SCOPE_READ = "activity:read"
AGENT_SCOPES: tuple[str, ...] = (SCOPE_PROPOSE, SCOPE_PAY, SCOPE_READ)

#: What each scope lets an agent do, in the cardholder's words. The consent screen and the
#: scope-refusal message both read from here, so the two never disagree.
SCOPE_SENTENCES: dict[str, str] = {
    SCOPE_PROPOSE: "draft a shopping mandate for you to confirm",
    SCOPE_PAY: "ask to pay with your card — every request is checked against your rules",
    SCOPE_READ: "read your rules and what it has already spent",
}


@dataclass(frozen=True, slots=True)
class Principal:
    """The verified identity behind one bearer token."""

    token: str
    subject: str  # the cardholder (customer_id)
    card_id: str
    client_id: str  # the agent, as the app registered it
    agent_name: str
    device_id: str  # the device the consent was given from; the errand runs under it
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def key(self) -> tuple[str, str]:
        """One errand per (cardholder, agent): a second token for the same pair is the same
        agent reconnecting, not a second agent."""
        return (self.subject, self.client_id)


class Unauthenticated(Exception):
    """No usable token. Rendered as a 401 that tells the client where to get one."""

    def __init__(self, description: str, *, error: str | None = "invalid_token") -> None:
        super().__init__(description)
        self.description = description
        self.error = error  # None when no token was presented at all (RFC 6750 §3.1)


class AuthorizationServerUnavailable(Exception):
    """The app that could vouch for the token is not answering. A 503, never a 401: the
    token may be perfectly good, and telling the client to re-authorize would send the
    cardholder through consent again for nothing."""


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal | None:
        """The principal behind a live token, or None when the token is not (or no longer)
        valid. Raises AuthorizationServerUnavailable when the answer cannot be obtained."""


class IntrospectionVerifier:
    """RFC 7662 introspection against the Payment App, with a short positive cache.

    `http` is injectable for the same reason `SandboxClient`'s is: the tests mount the app
    in-process through an ASGI transport, so no port is ever opened.
    """

    def __init__(
        self,
        app_url: str,
        secret: str,
        *,
        http: httpx.Client | None = None,
        cache_seconds: float = 3.0,
        timeout: float = 5.0,
    ) -> None:
        self.app_url = app_url.rstrip("/")
        self.secret = secret
        self._http = http or httpx.Client(timeout=timeout)
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, Principal | None]] = {}
        self._lock = threading.Lock()

    def verify(self, token: str) -> Principal | None:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(token)
            if hit is not None and hit[0] > now:
                return hit[1]
        principal = self._introspect(token)
        with self._lock:
            # A negative answer is cached only briefly: a token that was just revoked must
            # not keep working, and one that was just issued must not keep failing.
            ttl = self.cache_seconds if principal is not None else 0.5
            self._cache[token] = (now + ttl, principal)
            if len(self._cache) > 512:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        return principal

    def forget(self, token: str | None = None) -> None:
        with self._lock:
            if token is None:
                self._cache.clear()
            else:
                self._cache.pop(token, None)

    def _introspect(self, token: str) -> Principal | None:
        try:
            r = self._http.post(
                f"{self.app_url}/introspect",
                data={"token": token},
                headers={"Authorization": f"Bearer {self.secret}"},
            )
        except Exception as exc:  # whichever transport the injected client speaks
            raise AuthorizationServerUnavailable(f"{self.app_url}: {exc}") from exc
        if r.status_code != 200:
            raise AuthorizationServerUnavailable(
                f"{self.app_url}/introspect answered HTTP {r.status_code}"
            )
        body: dict[str, Any] = r.json()
        if not body.get("active"):
            return None
        return principal_from_claims(token, body)


def principal_from_claims(token: str, claims: dict[str, Any]) -> Principal:
    """The introspection response, or the app's own session record, as a Principal."""
    scopes = frozenset(str(claims.get("scope", "")).split())
    return Principal(
        token=token,
        subject=str(claims.get("sub", "")),
        card_id=str(claims.get("card_id", "")),
        client_id=str(claims.get("client_id", "")),
        agent_name=str(claims.get("client_name") or claims.get("client_id") or "an agent"),
        device_id=str(claims.get("device_id", "")),
        scopes=scopes & frozenset(AGENT_SCOPES),
    )
