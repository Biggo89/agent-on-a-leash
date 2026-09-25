"""The mock Payment App: an OAuth 2.1 authorization server wearing the cardholder's phone.

Two jobs, one process, because the consent screen *is* the phone:

* **Authorization server** for the connector — metadata (RFC 8414), dynamic client
  registration (RFC 7591), the PKCE authorization-code flow, refresh, token introspection
  (RFC 7662) and revocation (RFC 7009). Tokens are opaque and live here, which is what makes
  *Revoke* on the phone immediate: the connector asks this app on every call.
* **The cardholder's screens** — the LEASH app as the storyboard draws it (``ui.py``,
  ``static/leash.css``, the marks in ``brand.py``): the splash, the sign-in, and when an
  agent asks to connect, the onboarding — *Leash protects your payments*, five steps,
  *Confirm Leash*, which is the OAuth consent. After it the phone is the Cockpit
  (``static/cockpit.js``): spent and available, the leash's state, the open decisions —
  drafts to confirm with every rule quoting the cardholder's words, step-ups with their
  countdown — the connected agents with Revoke, recent decisions. The screens talk to the
  decision service exactly as the leash demo does (``/v1/...``), plus the two connector
  routes that name which agent proposed what.

Everything here is a mock: personas come from the data pack, any PIN works, the state is one
JSON file under ``out/``. What is *not* a mock is the shape of the flow — an MCP client that
can connect to Gmail can connect to this.

    make payment-app     → http://127.0.0.1:8081   (beside `make serve`)
    make connect         → both on :8010, the app under /app, for one tunnel
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from leash.adapters.datapack import DataPack, data_dir
from leash.connector.auth import AGENT_SCOPES, SCOPE_SENTENCES

from . import ui

log = logging.getLogger("payment_app")

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
#: The window the onboarding's "how you usually pay" reads, ending at the pack's last record.
PATTERN_DAYS = 90
DEFAULT_STATE_PATH = ROOT / "out" / "payment-app.json"
DEFAULT_SERVICE_URL = "http://127.0.0.1:8000"
DEFAULT_INTROSPECTION_SECRET = "dev-introspection-secret"
SESSION_COOKIE = "wallet_session"
ACCESS_TOKEN_SECONDS = 3600
REFRESH_TOKEN_SECONDS = 30 * 24 * 3600
CODE_SECONDS = 600
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")

app = FastAPI(title="Wallet — mock Payment App", version="0.1.0", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ------------------------------------------------------------------ configuration


def service_url() -> str:
    """Where the decision service is, for server-side calls (revoke)."""
    return os.environ.get("LEASH_SERVICE_URL", DEFAULT_SERVICE_URL).rstrip("/")


def browser_service_url() -> str:
    """Where the *browser* reaches the service. Empty means same origin (mounted under it)."""
    configured = os.environ.get("LEASH_SERVICE_BROWSER_URL")
    return configured.rstrip("/") if configured is not None else service_url()


def introspection_secret() -> str:
    return os.environ.get("WALLET_INTROSPECTION_SECRET", DEFAULT_INTROSPECTION_SECRET)


def issuer(request: Request) -> str:
    """This app's public base: env override, else the request's own (root_path included, so
    it is `…/app` when mounted on the service)."""
    configured = os.environ.get("WALLET_APP_PUBLIC_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def root_path(request: Request) -> str:
    return str(request.scope.get("root_path", "")).rstrip("/")


# ------------------------------------------------------------------ personas


@dataclass(frozen=True, slots=True)
class Persona:
    customer_id: str
    name: str
    card_id: str
    card_label: str
    last4: str
    device_id: str
    account_limit_chf: str
    monthly_limit_chf: str
    purchases: int
    pattern: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "name": self.name,
            "card_id": self.card_id,
            "card_label": self.card_label,
            "last4": self.last4,
            "device_id": self.device_id,
            "account_limit_chf": self.account_limit_chf,
            "monthly_limit_chf": self.monthly_limit_chf,
            "purchases": self.purchases,
            "pattern": dict(self.pattern),
        }


def _last4(card_id: str) -> str:
    digits = "".join(ch for ch in card_id if ch.isdigit()) or "0"
    return f"{(int(digits) * 7919) % 10000:04d}"


FREQUENCY = ((8, "rarely"), (24, "now and then"))
STYLE_CLAUSE = {
    "careful": "you compare before you pay",
    "balanced": "you keep to a budget",
    "flexible": "you decide as you go",
    "planned_high_value": "you plan the bigger purchases",
}


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _round_to(value: Decimal, step: int, *, up: bool) -> int:
    quotient = value / step
    whole = int(quotient.to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR))
    return max(step, whole * step) if up else max(0, whole * step)


def spending_pattern(
    rows: list[dict[str, str]],
    *,
    end: datetime,
    budget_style: str,
    known_shops: list[tuple[str, int]],
    account_limit: Decimal,
) -> dict[str, Any]:
    """Step 2 of the onboarding — *How you usually pay* — from the card's own approved
    purchases in the last `PATTERN_DAYS` days of the record. Counts and a typical range (the
    middle half of the amounts), never a judgement of the person; the sentence is built from
    the counts and the pack's own budget-style label. The suggested cap is the top of the
    typical range rounded up to fifty francs, never above the account's per-transaction
    limit."""
    since = end - timedelta(days=PATTERN_DAYS)
    recent = [r for r in rows if _stamp(r["timestamp"]) >= since]
    sample = recent or rows
    amounts = sorted(Decimal(r["billing_amount_chf"]) for r in sample) or [Decimal("50")]
    low = amounts[len(amounts) // 4]
    high = amounts[(len(amounts) * 3) // 4]
    count = len(recent)
    known = sum(
        1 for r in recent if int(r.get("approved_merchant_transaction_count_before") or 0) > 0
    )
    frequency = next((word for limit, word in FREQUENCY if count <= limit), "often")
    share = known / count if count else 0
    where = (
        "mostly at merchants you know"
        if share >= 0.6
        else "often at merchants you know"
        if share >= 0.3
        else "often at new merchants"
    )
    clause = STYLE_CLAUSE.get(budget_style, "you decide as you go")
    typical_high = _round_to(high, 5, up=True)
    cap = _round_to(high, 50, up=True)
    if account_limit > 0:
        cap = min(cap, int(account_limit))
    return {
        "purchases_90d": count,
        "known_90d": known,
        "typical_low": _round_to(low, 5, up=False),
        "typical_high": typical_high,
        "frequency": frequency,
        "sentence": f"You buy {frequency}, {where}, and {clause}.",
        "suggested_cap_chf": max(50, cap),
        "top_shop": known_shops[0][0] if known_shops else None,
    }


def load_personas() -> list[Persona]:
    """One persona per customer in the pack, on their everyday card, ordered by how much
    history that card has — the richest first, because familiarity is what the demo shows."""
    pack = DataPack.load()
    devices: dict[str, Counter[str]] = {}
    purchases: Counter[str] = Counter()
    approved: dict[str, list[dict[str, str]]] = {}
    shops: dict[str, Counter[str]] = {}
    latest = datetime(2000, 1, 1, tzinfo=UTC)
    with (data_dir() / "authorization_history.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            latest = max(latest, _stamp(row["timestamp"]))
            if row.get("status") != "approved":
                continue
            card = row["card_id"]
            if row.get("customer_device_id"):
                devices.setdefault(card, Counter())[row["customer_device_id"]] += 1
            if row.get("transaction_type") == "purchase":
                purchases[card] += 1
                approved.setdefault(card, []).append(row)
                shops.setdefault(card, Counter())[row["merchant_name"]] += 1
    personas: list[Persona] = []
    for customer in pack.customers.values():
        accounts = [
            a for a in pack.accounts.values() if a["customer_id"] == customer["customer_id"]
        ]
        cards = [
            c
            for a in accounts
            for c in pack.cards.values()
            if c["account_id"] == a["account_id"] and c.get("status") == "active"
        ]
        if not cards:
            continue
        cards.sort(key=lambda c: (c.get("card_purpose") != "everyday", -purchases[c["card_id"]]))
        card = cards[0]
        account = next(a for a in accounts if a["account_id"] == card["account_id"])
        used = devices.get(card["card_id"], Counter()).most_common(1)
        limit_raw = str(account.get("per_transaction_limit_chf", "") or "0")
        pattern = spending_pattern(
            approved.get(card["card_id"], []),
            end=latest,
            budget_style=str(customer.get("budget_style", "")),
            known_shops=shops.get(card["card_id"], Counter()).most_common(3),
            account_limit=Decimal(limit_raw),
        )
        personas.append(
            Persona(
                customer_id=customer["customer_id"],
                name=customer["persona_name"],
                card_id=card["card_id"],
                card_label=(
                    f"{card.get('card_purpose', 'card').replace('_', ' ')} "
                    f"{card.get('card_type', '')}"
                ).strip(),
                last4=_last4(card["card_id"]),
                device_id=used[0][0] if used else f"DVC-{card['card_id'][-4:]}",
                account_limit_chf=limit_raw,
                monthly_limit_chf=str(account.get("monthly_limit_chf", "") or "0"),
                purchases=purchases[card["card_id"]],
                pattern=pattern,
            )
        )
    personas.sort(key=lambda p: (-p.purchases, p.name))
    return personas


PERSONAS: list[Persona] = load_personas()
PERSONA_BY_ID = {p.customer_id: p for p in PERSONAS}


# ------------------------------------------------------------------ state


def _grant_key(client_id: str, subject: str) -> str:
    """One consent per (agent, cardholder). A registered client is one agent installation,
    and two cardholders can connect it: on 2026-09-24 Claude Code opened its authorize URL in
    the laptop's own browser as well, a second persona signed in there, and a grant keyed by
    client alone was overwritten with the wrong cardholder."""
    return f"{client_id}|{subject}"


class Store:
    """Clients, codes, tokens, consents and phone sessions. One JSON file, saved on every
    mutation, so a restart keeps every connected agent. `None` keeps it in memory (tests)."""

    TABLES = ("clients", "codes", "tokens", "refresh", "grants", "sessions", "profiles")

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, dict[str, Any]] = {t: {} for t in self.TABLES}
        self.load()

    def reset(self, path: Path | None = None) -> None:
        with self.lock:
            self.path = path
            self.data = {t: {} for t in self.TABLES}
            self.load()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); starting empty", self.path, exc)
            return
        for table in self.TABLES:
            self.data[table] = dict(loaded.get(table) or {})
        # Grants were keyed by client alone until 2026-09-24.
        self.data["grants"] = {
            (k if "|" in k else _grant_key(k, str(v.get("subject", "")))): v
            for k, v in self.data["grants"].items()
        }

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def __getitem__(self, table: str) -> dict[str, Any]:
        return self.data[table]


def _state_path() -> Path | None:
    raw = os.environ.get("WALLET_APP_STATE")
    if raw == "":
        return None
    return Path(raw) if raw else DEFAULT_STATE_PATH


STORE = Store(_state_path())


class Wiring:
    """What the app calls out to. Injectable so the tests mount the service in-process."""

    def __init__(self) -> None:
        self.service_http: httpx.Client | None = None

    def service(self) -> httpx.Client:
        if self.service_http is None:
            self.service_http = httpx.Client(timeout=10.0)
        return self.service_http


WIRING = Wiring()


def _now() -> int:
    return int(time.time())


# ------------------------------------------------------------------ helpers


async def _form(request: Request) -> dict[str, str]:
    """An `application/x-www-form-urlencoded` body, by hand: the OAuth endpoints and the two
    phone forms need nothing more, and a multipart parser would be a dependency."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    return dict(parse_qsl(raw, keep_blank_values=True))


def _error_json(status: int, error: str, description: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": error, "error_description": description},
        headers={"Cache-Control": "no-store"},
    )


def _page(html: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})


def _session(request: Request) -> dict[str, Any] | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    with STORE.lock:
        session = STORE["sessions"].get(sid)
    return dict(session) if session else None


def _persona_of(session: dict[str, Any]) -> Persona | None:
    return PERSONA_BY_ID.get(str(session.get("subject", "")))


def _safe_next(request: Request, candidate: str) -> str:
    """Only ever redirect back into this app: a path under our own root, never a scheme or
    another host."""
    root = root_path(request)
    if (
        candidate.startswith("/")
        and not candidate.startswith("//")
        and candidate.startswith(root + "/")
    ):
        return candidate
    return f"{root}/"


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303, headers={"Cache-Control": "no-store"})


def _client_credentials(request: Request, form: dict[str, str]) -> tuple[str, str | None]:
    """client_id and secret from Basic auth or the form body."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            client_id, _, secret = decoded.partition(":")
            return client_id, secret
        except (ValueError, UnicodeDecodeError):
            return "", None
    return form.get("client_id", ""), form.get("client_secret")


def _authenticate_client(request: Request, form: dict[str, str]) -> dict[str, Any] | None:
    client_id, secret = _client_credentials(request, form)
    with STORE.lock:
        client = STORE["clients"].get(client_id)
    if client is None:
        return None
    if client["token_endpoint_auth_method"] == "none":
        return dict(client)
    if secret and secrets.compare_digest(str(client.get("client_secret", "")), secret):
        return dict(client)
    return None


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


def _issue_tokens(
    client: dict[str, Any], *, subject: str, scope: str, resource: str | None
) -> dict[str, Any]:
    persona = PERSONA_BY_ID[subject]
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    issued = _now()
    record = {
        "client_id": client["client_id"],
        "client_name": client.get("client_name") or client["client_id"],
        "sub": subject,
        "card_id": persona.card_id,
        "device_id": persona.device_id,
        "scope": scope,
        "aud": resource,
        "iat": issued,
        "exp": issued + ACCESS_TOKEN_SECONDS,
        "refresh_token": refresh,
    }
    with STORE.lock:
        STORE["tokens"][access] = record
        STORE["refresh"][refresh] = {
            "access_token": access,
            "client_id": client["client_id"],
            "exp": issued + REFRESH_TOKEN_SECONDS,
        }
        STORE.save()
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_SECONDS,
        "refresh_token": refresh,
        "scope": scope,
    }


def _drop_token(access: str) -> None:
    with STORE.lock:
        record = STORE["tokens"].pop(access, None)
        if record:
            STORE["refresh"].pop(str(record.get("refresh_token", "")), None)


def _drop_client_tokens(client_id: str, subject: str | None = None) -> int:
    """Every token (and unspent code) of this agent — for one cardholder when given."""

    def mine(rec: dict[str, Any], key: str) -> bool:
        return rec.get("client_id") == client_id and (subject is None or rec.get(key) == subject)

    with STORE.lock:
        doomed = [t for t, rec in STORE["tokens"].items() if mine(rec, "sub")]
        for token in doomed:
            _drop_token(token)
        for code in [c for c, rec in STORE["codes"].items() if mine(rec, "subject")]:
            STORE["codes"].pop(code, None)
        STORE.save()
    return len(doomed)


# ------------------------------------------------------------------ discovery


def metadata_document(base: str) -> dict[str, Any]:
    """RFC 8414. `base` is the issuer; when mounted, `…/app`."""
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "introspection_endpoint": f"{base}/introspect",
        "revocation_endpoint": f"{base}/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
        "scopes_supported": list(AGENT_SCOPES),
        "service_documentation": f"{base}/",
    }


@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/openid-configuration")
def discovery(request: Request) -> dict[str, Any]:
    return metadata_document(issuer(request))


# ------------------------------------------------------------------ registration


@app.post("/register", status_code=201)
async def register(request: Request) -> Response:
    """RFC 7591 dynamic client registration — what makes the connector one paste."""
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return _error_json(400, "invalid_client_metadata", "body must be JSON")
    if not isinstance(body, dict):
        return _error_json(400, "invalid_client_metadata", "body must be an object")
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        return _error_json(400, "invalid_redirect_uri", "redirect_uris must be a non-empty list")
    for uri in uris:
        parts = urlsplit(uri)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            return _error_json(400, "invalid_redirect_uri", f"not an absolute URL: {uri}")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1"):
            return _error_json(
                400, "invalid_redirect_uri", f"http is allowed only on localhost: {uri}"
            )
    method = str(body.get("token_endpoint_auth_method") or "client_secret_basic")
    if method not in AUTH_METHODS:
        return _error_json(400, "invalid_client_metadata", f"unsupported auth method {method}")
    grants = body.get("grant_types") or ["authorization_code"]
    if not set(grants) <= {"authorization_code", "refresh_token"}:
        return _error_json(400, "invalid_client_metadata", "unsupported grant_types")

    client: dict[str, Any] = {
        "client_id": f"agent_{secrets.token_hex(8)}",
        "client_id_issued_at": _now(),
        "client_secret_expires_at": 0,
        "client_name": str(body.get("client_name") or "An agent")[:80],
        "redirect_uris": list(uris),
        "token_endpoint_auth_method": method,
        "grant_types": sorted({*grants, "refresh_token"}),
        "response_types": ["code"],
        "scope": " ".join(AGENT_SCOPES),
    }
    # Echo only what was registered (RFC 7591 §3.2.1). A URL field echoed as an empty
    # string is refused by a client that validates the response — Claude Code did, on
    # 2026-09-24 — so an absent `client_uri` stays absent.
    client_uri = str(body.get("client_uri") or "")
    if client_uri and urlsplit(client_uri).scheme in ("https", "http"):
        client["client_uri"] = client_uri
    if method != "none":
        client["client_secret"] = secrets.token_urlsafe(32)
    with STORE.lock:
        STORE["clients"][client["client_id"]] = client
        STORE.save()
    log.info(
        "registered %s as %s: auth=%s redirect_uris=%s grant_types=%s",
        client["client_name"],
        client["client_id"],
        method,
        client["redirect_uris"],
        client["grant_types"],
    )
    return JSONResponse(status_code=201, content=client, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ authorize


def _authorize_params(source: dict[str, str]) -> dict[str, str]:
    keys = (
        "client_id",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
        "resource",
    )
    return {k: str(source.get(k) or "") for k in keys}


def _validate_authorize(params: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """(client, error). An error before the redirect URI is trusted is a page, never a
    redirect — the one rule of RFC 6749 §4.1.2.1 that matters most."""
    with STORE.lock:
        client = STORE["clients"].get(params["client_id"])
    if client is None:
        return None, "unknown client_id: connect the agent again so it can register"
    if params["redirect_uri"] not in client["redirect_uris"]:
        return None, "redirect_uri is not one this agent registered"
    return dict(client), None


def _redirect_error(params: dict[str, str], error: str, description: str) -> RedirectResponse:
    query = {"error": error, "error_description": description}
    if params.get("state"):
        query["state"] = params["state"]
    return _redirect(f"{params['redirect_uri']}?{urlencode(query)}")


def _setup_from(raw: str) -> dict[str, Any] | None:
    """What the onboarding's steps recorded: whether past payments were used, the suggested
    cap, and the four choices. Kept with the grant for the Cockpit to show; it decides
    nothing — the rules that decide come from the mandate (specs/connector.md §7)."""
    if not raw.strip():
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    raw_choices = body.get("choices")
    choices: dict[str, Any] = raw_choices if isinstance(raw_choices, dict) else {}
    cap = body.get("suggested_cap_chf")
    used = body.get("used_history")
    return {
        "used_history": used if isinstance(used, bool) else None,
        "suggested_cap_chf": (
            int(cap) if isinstance(cap, int | float) and not isinstance(cap, bool) else None
        ),
        "choices": {
            str(k)[:1]: ("allow" if v == "allow" else "ask")
            for k, v in choices.items()
            if str(k)[:1] in "ABCD"
        },
    }


def _requested_scopes(raw: str) -> list[str] | None:
    """The scopes the client asked for, or every agent scope when it asked for none. Anything
    outside the agent grammar is refused — there is no scope that confirms or resolves."""
    wanted = raw.split()
    if not wanted:
        return list(AGENT_SCOPES)
    if not set(wanted) <= set(AGENT_SCOPES):
        return None
    return [s for s in AGENT_SCOPES if s in wanted]


@app.get("/authorize")
def authorize(request: Request) -> Response:
    params = _authorize_params(dict(request.query_params))
    client, problem = _validate_authorize(params)
    if client is None:
        return _page(ui.error_page("Cannot connect", problem or "invalid request"), 400)
    if params["response_type"] != "code":
        return _redirect_error(params, "unsupported_response_type", "only code is supported")
    if not params["code_challenge"] or params["code_challenge_method"] != "S256":
        return _redirect_error(params, "invalid_request", "PKCE with S256 is required")
    scopes = _requested_scopes(params["scope"])
    if scopes is None:
        return _redirect_error(params, "invalid_scope", "an agent can only hold agent scopes")
    log.info(
        "authorize %s: scope=%r redirect_uri=%s resource=%r",
        client["client_id"],
        params["scope"],
        params["redirect_uri"],
        params["resource"],
    )

    session = _session(request)
    persona = _persona_of(session) if session else None
    root = root_path(request)
    if persona is None:
        here = f"{root}/authorize?{urlencode({k: v for k, v in params.items() if v})}"
        return _page(ui.splash_page(next_url=here, root=root))
    with STORE.lock:
        profile = dict(STORE["profiles"].get(persona.customer_id) or {})
    return _page(
        ui.onboarding_page(
            agent_name=str(client.get("client_name") or client["client_id"]),
            persona=persona,
            scopes=[(s, SCOPE_SENTENCES[s]) for s in scopes],
            params={**params, "scope": " ".join(scopes)},
            root=root,
            onboarded=bool(profile.get("onboarded_at")),
        )
    )


@app.post("/authorize")
async def authorize_decision(request: Request) -> Response:
    form = await _form(request)
    params = _authorize_params(form)
    client, problem = _validate_authorize(params)
    if client is None:
        return _page(ui.error_page("Cannot connect", problem or "invalid request"), 400)
    session = _session(request)
    persona = _persona_of(session) if session else None
    if persona is None:
        return _redirect_error(params, "access_denied", "the cardholder is not signed in")
    if form.get("decision") != "approve":
        return _redirect_error(params, "access_denied", "the cardholder declined")
    scopes = _requested_scopes(params["scope"])
    if scopes is None:
        return _redirect_error(params, "invalid_scope", "an agent can only hold agent scopes")

    setup = _setup_from(form.get("setup", ""))
    code = secrets.token_urlsafe(24)
    with STORE.lock:
        STORE["profiles"][persona.customer_id] = {
            **dict(STORE["profiles"].get(persona.customer_id) or {}),
            "onboarded_at": _now(),
            **({"setup": setup} if setup else {}),
        }
        STORE["codes"][code] = {
            "client_id": client["client_id"],
            "redirect_uri": params["redirect_uri"],
            "scope": " ".join(scopes),
            "code_challenge": params["code_challenge"],
            "resource": params["resource"] or None,
            "subject": persona.customer_id,
            "exp": _now() + CODE_SECONDS,
        }
        STORE["grants"][_grant_key(client["client_id"], persona.customer_id)] = {
            "client_id": client["client_id"],
            "agent_name": client.get("client_name") or client["client_id"],
            "subject": persona.customer_id,
            "card_id": persona.card_id,
            "scope": " ".join(scopes),
            "granted_at": _now(),
            "setup": setup,
        }
        STORE.save()
    query = {"code": code}
    if params["state"]:
        query["state"] = params["state"]
    log.info("%s connected %s", persona.name, client.get("client_name"))
    return _redirect(f"{params['redirect_uri']}?{urlencode(query)}")


# ------------------------------------------------------------------ token


@app.post("/token")
async def token(request: Request) -> Response:
    form = await _form(request)
    client = _authenticate_client(request, form)
    if client is None:
        return _error_json(401, "invalid_client", "unknown client or bad secret")
    grant = form.get("grant_type", "")
    log.info(
        "token %s: grant_type=%s client_auth=%s",
        client["client_id"],
        grant,
        "basic"
        if request.headers.get("authorization", "").lower().startswith("basic ")
        else "post"
        if form.get("client_secret")
        else "none",
    )

    if grant == "authorization_code":
        with STORE.lock:
            record = STORE["codes"].pop(form.get("code", ""), None)  # single use
            STORE.save()
        if record is None or record["exp"] < _now():
            return _error_json(400, "invalid_grant", "unknown or expired code")
        if record["client_id"] != client["client_id"]:
            return _error_json(400, "invalid_grant", "code was issued to another client")
        if form.get("redirect_uri", "") != record["redirect_uri"]:
            return _error_json(400, "invalid_grant", "redirect_uri does not match")
        if not _pkce_ok(form.get("code_verifier", ""), record["code_challenge"]):
            return _error_json(400, "invalid_grant", "PKCE verification failed")
        body = _issue_tokens(
            client, subject=record["subject"], scope=record["scope"], resource=record["resource"]
        )
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    if grant == "refresh_token":
        with STORE.lock:
            pointer = STORE["refresh"].get(form.get("refresh_token", ""))
            old = STORE["tokens"].get(pointer["access_token"]) if pointer else None
        if pointer is None or old is None or pointer["exp"] < _now():
            return _error_json(400, "invalid_grant", "unknown or expired refresh token")
        if old["client_id"] != client["client_id"]:
            return _error_json(400, "invalid_grant", "refresh token belongs to another client")
        with STORE.lock:
            grant_row = STORE["grants"].get(_grant_key(client["client_id"], str(old["sub"])))
        if grant_row is None:
            return _error_json(400, "invalid_grant", "the cardholder revoked this agent")
        _drop_token(pointer["access_token"])  # rotation: the old pair dies with the refresh
        body = _issue_tokens(client, subject=old["sub"], scope=old["scope"], resource=old["aud"])
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    return _error_json(400, "unsupported_grant_type", f"unsupported grant_type {grant!r}")


# ------------------------------------------------------------------ introspect · revoke


def _introspection_authorized(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    presented = header[7:] if header.lower().startswith("bearer ") else ""
    return bool(presented) and secrets.compare_digest(presented, introspection_secret())


@app.post("/introspect")
async def introspect(request: Request) -> Response:
    """RFC 7662, for the connector only. The secret is shared configuration, which is a mock's
    shortcut for mutual TLS or a client credential of the resource server's own."""
    if not _introspection_authorized(request):
        return _error_json(401, "invalid_client", "the introspection secret is wrong")
    form = await _form(request)
    with STORE.lock:
        record = STORE["tokens"].get(form.get("token", ""))
        record = dict(record) if record else None
    if record is None or record["exp"] < _now():
        return JSONResponse(content={"active": False}, headers={"Cache-Control": "no-store"})
    record.pop("refresh_token", None)
    return JSONResponse(
        content={"active": True, "token_type": "Bearer", **record},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/revoke")
async def revoke(request: Request) -> Response:
    """RFC 7009. Unknown tokens are a 200 too — the caller learns nothing from the answer."""
    form = await _form(request)
    presented = form.get("token", "")
    with STORE.lock:
        if presented in STORE["tokens"]:
            _drop_token(presented)
        elif presented in STORE["refresh"]:
            _drop_token(STORE["refresh"][presented]["access_token"])
        STORE.save()
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ the phone


@app.get("/login")
def login_page(request: Request, next: str = "") -> Response:
    root = root_path(request)
    return _page(
        ui.login_page(PERSONAS, next_url=_safe_next(request, next or f"{root}/"), root=root)
    )


@app.post("/login")
async def login(request: Request) -> Response:
    form = await _form(request)
    subject, next_url = form.get("subject", ""), form.get("next", "")
    persona = PERSONA_BY_ID.get(subject)
    root = root_path(request)
    if persona is None:
        return _page(
            ui.login_page(
                PERSONAS,
                next_url=_safe_next(request, next_url),
                root=root,
                error="Pick a cardholder.",
            ),
            400,
        )
    sid = secrets.token_urlsafe(24)
    with STORE.lock:
        STORE["sessions"][sid] = {"subject": persona.customer_id, "signed_in_at": _now()}
        STORE.save()
    response = _redirect(_safe_next(request, next_url))
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        samesite="lax",
        path="/",
        secure=request.url.scheme == "https",
        max_age=7 * 24 * 3600,
    )
    return response


@app.post("/logout")
def logout(request: Request) -> Response:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        with STORE.lock:
            STORE["sessions"].pop(sid, None)
            STORE.save()
    response = _redirect(f"{root_path(request)}/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/")
def home(request: Request) -> Response:
    session = _session(request)
    persona = _persona_of(session) if session else None
    root = root_path(request)
    if persona is None:
        return _page(ui.splash_page(next_url=f"{root}/", root=root))
    return _page(
        ui.cockpit_page(
            persona=persona,
            config={
                "service": browser_service_url(),
                "root": root,
                "subject": persona.customer_id,
                "card_id": persona.card_id,
                "name": persona.name,
            },
            root=root,
        )
    )


def _profile_view(persona: Persona) -> dict[str, Any]:
    with STORE.lock:
        profile = dict(STORE["profiles"].get(persona.customer_id) or {})
    return {
        "persona": persona.as_dict(),
        "service": browser_service_url(),
        "pattern": dict(persona.pattern),
        "setup": profile.get("setup"),
        "onboarded_at": profile.get("onboarded_at"),
        "limits": {
            "per_transaction_limit_chf": persona.account_limit_chf,
            "monthly_limit_chf": persona.monthly_limit_chf,
        },
    }


@app.get("/api/me")
def me(request: Request) -> Response:
    session = _session(request)
    persona = _persona_of(session) if session else None
    if persona is None:
        return _error_json(401, "not_signed_in", "sign in first")
    return JSONResponse(content=_profile_view(persona))


@app.get("/api/agents")
def agents(request: Request) -> Response:
    session = _session(request)
    if session is None:
        return _error_json(401, "not_signed_in", "sign in first")
    subject = str(session.get("subject", ""))
    with STORE.lock:
        grants = [dict(g) for g in STORE["grants"].values() if g.get("subject") == subject]
        counts = Counter(
            rec.get("client_id") for rec in STORE["tokens"].values() if rec.get("sub") == subject
        )
    for grant in grants:
        grant["tokens"] = counts.get(grant["client_id"], 0)
        grant["scopes"] = [
            {"scope": s, "sentence": SCOPE_SENTENCES.get(s, s)} for s in grant["scope"].split()
        ]
    return JSONResponse(content={"agents": grants})


@app.post("/api/agents/{client_id}/revoke")
def revoke_agent(client_id: str, request: Request) -> Response:
    """The cardholder pulled the leash. Tokens die here; the errand and the mandate die in
    the connector; the agent's next call is a 401 with a clear message."""
    session = _session(request)
    if session is None:
        return _error_json(401, "not_signed_in", "sign in first")
    subject = str(session.get("subject", ""))
    with STORE.lock:
        grant = STORE["grants"].pop(_grant_key(client_id, subject), None)
    if grant is None:
        return _error_json(404, "not_found", "no such connected agent")
    dropped = _drop_client_tokens(client_id, subject)
    connector: dict[str, Any] = {}
    try:
        r = WIRING.service().post(
            f"{service_url()}/connector/agents/{client_id}/revoke", params={"subject": subject}
        )
        connector = (
            r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        )
        connector["http_status"] = r.status_code
    except Exception as exc:  # noqa: BLE001 — the token is dead either way; say what else happened
        connector = {"error": f"{type(exc).__name__}: {exc}"}
    log.info("revoked %s (%d tokens)", client_id, dropped)
    return JSONResponse(
        content={"client_id": client_id, "tokens_revoked": dropped, "connector": connector}
    )
