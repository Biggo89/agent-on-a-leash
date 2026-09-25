"""HTTP client for the sandbox API — the real one or the offline replica.

Point LEASH_BASE_URL at the replica during development and at the organizers' URL on the day.
The protocol is identical, which is the whole point of the replica.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx


class UpstreamError(Exception):
    """An upstream call that failed. Raised instead of the HTTP library's own exception.

    The client accepts an *injected* HTTP client so the tests can mount the replica's ASGI app
    directly, which means the exception type coming out of it belongs to whichever library
    that client was built from — starlette's TestClient switches between `httpx` and `httpx2`
    depending on what else is installed in the environment. Callers must not have to know, so
    the boundary that owns the transport also owns its error type.
    """

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body if isinstance(body, dict) else {}

    @property
    def code(self) -> str:
        envelope = self.body.get("error", {})
        code = envelope.get("code") if isinstance(envelope, dict) else None
        return str(code or (f"upstream_{self.status}" if self.status else "unreachable"))

    @property
    def detail(self) -> str:
        envelope = self.body.get("error", {})
        return str(envelope.get("message", "")) if isinstance(envelope, dict) else ""


@dataclass(frozen=True, slots=True)
class UpstreamResult:
    """A call whose failure is part of the protocol, not an exception.

    The platform answers a late decision with 408 and a second decision with 409. Both are
    outcomes the run loop has to record and carry on from, so they arrive as data.
    """

    status: int | None  # None when the request never reached the server
    body: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def as_dict(self) -> dict[str, Any]:
        return {
            "submitted": self.status is not None,
            "http_status": self.status,
            "platform_status": self.body.get("status"),
            "idempotent": bool(self.body.get("idempotent")),
            "error": self.error_code,
        }


# ------------------------------------------------------------------ platform shapes
#
# Where the organizers' API and the offline replica report the same fact under different
# names, the difference is absorbed here, once, so nothing downstream reads a raw shape.
# Every entry was found by `make probe-live` against the real sandbox on 2026-09-24.

PENDING_STEP_UP_STATUSES = frozenset({"pending_step_up", "pending_customer"})
"""A step-up still waiting on the customer. ``pending_step_up`` is the real API's status, and
the replica's since it models the hold; ``pending_customer`` is what the replica said before."""

_LIMIT_ALIASES = {
    "decision_timeout_seconds": "decision_deadline_seconds",
    "step_up_timeout_seconds": "step_up_window_seconds",
    "long_poll_max_seconds": "max_long_poll_seconds",
}


def limit_seconds(bootstrap: dict[str, Any], name: str) -> int | None:
    """A timing limit from ``/v1/bootstrap``, under the real API's name or the replica's."""
    limits = bootstrap.get("limits") or {}
    value = limits.get(name, limits.get(_LIMIT_ALIASES.get(name, name)))
    return int(value) if value is not None else None


def run_complete(run: dict[str, Any]) -> bool:
    """Whether the platform has finished a run.

    The real API says ``status: "completed"`` and carries no ``counters`` at all; the replica
    says the same, with ``complete`` beside ``counters``. Reading only the replica's shape left
    every live run polling until it was abandoned as idle. An explicit answer is final: the
    counter fallback would call a run finished while its last order waits on the customer.
    """
    if "status" in run:
        return bool(run["status"] == "completed")
    if "complete" in run:
        return bool(run["complete"])
    counters = run.get("counters") or {}
    return int(counters.get("decided", 0)) >= int(counters.get("total", 0)) > 0


def run_total(run: dict[str, Any]) -> int | None:
    """How many authorizations a run will deliver, when the platform says.

    The real API does not: it generates each event only once the one before it is settled,
    so ``generated_event_count`` is progress, not a total. Callers fall back to the
    scenario's catalogue ``event_count``.
    """
    total = (run.get("counters") or {}).get("total")
    return int(total) if total is not None else None


def run_card(run: dict[str, Any]) -> str | None:
    """The card a run's orders are charged to, as the platform names it.

    The real API lists it under ``fixture_profiles`` (one entry per profile); the replica
    returns a single ``profile``. Live scenarios have no rows in any pack file, so this is the
    only place a live run's cardholder can be read from before its first order arrives.
    """
    profiles = run.get("fixture_profiles") or []
    single = run.get("profile")
    for profile in [*profiles, single] if isinstance(profiles, list) else [single]:
        if isinstance(profile, dict) and profile.get("card_id"):
            return str(profile["card_id"])
    return None


def authorization_decision(row: dict[str, Any]) -> str | None:
    """The decision on a ``/v1/authorizations`` row, as a bare string.

    The real API nests the whole submitted decision object under ``decision``; the replica
    stores the string itself.
    """
    decision = row.get("decision")
    if isinstance(decision, dict):
        decision = decision.get("decision")
    return str(decision) if decision else None


class SandboxClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 40.0,  # above the platform's 25 s long poll, with room to spare
        http: httpx.Client | None = None,
    ):
        self.base_url = (
            base_url or os.environ.get("LEASH_BASE_URL", "http://127.0.0.1:8099")
        ).rstrip("/")
        # `or` rather than a default lookup: .env.example ships TEAM_API_KEY="" because the
        # team key is issued on the day, so the variable is routinely PRESENT AND EMPTY. An
        # empty key builds the header "Bearer " with a trailing space, which h11 rejects as
        # an illegal header value — surfacing as a connection error against the local
        # replica, which needs no key at all.
        self.api_key = api_key or os.environ.get("TEAM_API_KEY") or "dev-key"
        # An injected client lets the tests mount the replica ASGI app directly, so the
        # whole stack — service, run loop, sandbox — runs in one process with no network.
        self._c = http or httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._c.close()

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _json(self, r: Any) -> dict[str, Any]:
        if r.status_code >= 400:
            body: Any = {}
            try:
                body = r.json() if r.content else {}
            except ValueError:
                body = {"raw": r.text}
            raise UpstreamError(
                f"{r.request.method} {r.request.url} -> {r.status_code}",
                status=r.status_code,
                body=body,
            )
        return r.json() if r.content else {}

    def health(self) -> dict[str, Any]:
        """Unauthenticated, but sent through the same client so a test transport applies."""
        return self._json(self._c.get("/healthz"))

    def bootstrap(self) -> dict[str, Any]:
        return self._json(self._c.get("/v1/bootstrap"))

    def reference_data(self) -> dict[str, Any]:
        return self._json(self._c.get("/v1/reference-data"))

    def reference_history_csv(self) -> bytes:
        """The history file as served. CSV, so it does not go through `_json` on success."""
        r = self._c.get("/v1/reference-data/authorization-history.csv")
        if r.status_code >= 400:
            self._json(r)  # raises UpstreamError with the platform's error body
        return bytes(r.content)

    def create_mandate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json(self._c.post("/v1/mandates", json=payload))

    def confirm_mandate(self, draft_id: str) -> dict[str, Any]:
        return self._json(
            self._c.post(f"/v1/mandates/{draft_id}/confirm", json={"confirmed": True})
        )

    def get_mandate(self, mandate_id: str) -> dict[str, Any]:
        return self._json(self._c.get(f"/v1/mandates/{mandate_id}"))

    def patch_mandate(self, mandate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json(self._c.patch(f"/v1/mandates/{mandate_id}", json=payload))

    def revoke_mandate(self, mandate_id: str) -> dict[str, Any]:
        return self._json(self._c.delete(f"/v1/mandates/{mandate_id}"))

    def start_run(self, scenario_id: str, mandate_id: str) -> dict[str, Any]:
        return self._json(
            self._c.post(
                "/v1/scenario-runs", json={"scenario_id": scenario_id, "mandate_id": mandate_id}
            )
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._json(self._c.get(f"/v1/scenario-runs/{run_id}"))

    def next_request(self, wait: int = 25) -> dict[str, Any] | None:
        """Long-poll. Returns the envelope, or None on 204 (nothing actionable)."""
        r = self._c.get("/v1/decision-requests/next", params={"wait": wait})
        if r.status_code == 204:
            return None
        return self._json(r)

    def submit_decision(self, authorization_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json(
            self._c.post(f"/v1/authorizations/{authorization_id}/decision", json=payload)
        )

    def _attempt(self, method: str, url: str, payload: dict[str, Any]) -> UpstreamResult:
        """Post without raising. 4xx is an answer here, not a failure to call."""
        try:
            r = self._c.request(method, url, json=payload)
        # Not `httpx.HTTPError`: the transport may be an injected client from another HTTP
        # library (see UpstreamError). A request that never returned is unreachable either way.
        except Exception as exc:
            return UpstreamResult(None, error_code="unreachable", error_message=str(exc))
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {"raw": r.text}
        if 200 <= r.status_code < 300:
            return UpstreamResult(r.status_code, body)
        envelope = body.get("error", {}) if isinstance(body, dict) else {}
        return UpstreamResult(
            r.status_code,
            body if isinstance(body, dict) else {},
            error_code=envelope.get("code", f"http_{r.status_code}"),
            error_message=envelope.get("message", ""),
        )

    def try_submit_decision(self, authorization_id: str, payload: dict[str, Any]) -> UpstreamResult:
        """Submit a decision, reporting 408/409 as data. Used by the live run loop."""
        return self._attempt("POST", f"/v1/authorizations/{authorization_id}/decision", payload)

    def try_resolve(
        self, authorization_id: str, decision: str, message: str = ""
    ) -> UpstreamResult:
        return self._attempt(
            "POST",
            f"/v1/authorizations/{authorization_id}/resolve",
            {"decision": decision, "customer_message": message, "evidence": []},
        )

    def resolve(self, authorization_id: str, decision: str, message: str = "") -> dict[str, Any]:
        return self._json(
            self._c.post(
                f"/v1/authorizations/{authorization_id}/resolve",
                json={"decision": decision, "customer_message": message, "evidence": []},
            )
        )

    def authorizations(self) -> list[dict[str, Any]]:
        """Every runtime authorization. The real API returns a bare JSON array; the replica
        wraps it in ``{"authorizations": [...]}``."""
        body: Any = self._json(self._c.get("/v1/authorizations"))
        rows = body.get("authorizations", []) if isinstance(body, dict) else body
        return [r for r in rows or [] if isinstance(r, dict)]

    def events(self, since: int = 0) -> dict[str, Any]:
        return self._json(self._c.get("/v1/events", params={"since": since}))

    def reset(self) -> dict[str, Any]:
        return self._json(self._c.post("/v1/team/reset"))
