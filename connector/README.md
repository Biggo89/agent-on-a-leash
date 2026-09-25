# Leash Wallet — connector design

**Status:** built and verified 2026-09-24 · **Owner:** Nakya · **Code:** `wallet-control-layer/src/leash/connector/`, `wallet-control-layer/payment_app/` · **Normative:** `wallet-control-layer/specs/connector.md`
**Shareable page:** `design.html` in this folder is the same content as a page for the team.
**Connect Claude in five minutes:** [`SETUP-WITH-CLAUDE.md`](SETUP-WITH-CLAUDE.md). The connector is named
**Leash Wallet** (`Leash-Wallet` in Claude Code, which allows no spaces in a server name).

How a cardholder plugs the Wallet Control Layer into an AI agent the way they plug in Gmail
or GitHub: one URL, a login to their payment app, a consent screen, done. The agent gets a
small set of tools. The cardholder keeps every decision that matters in the app.

The payment app is a **mock**. The control layer is the real one in `../wallet-control-layer`.

**What exists now.** Everything below is implemented, with two deliberate changes from the
first draft of this note, both explained where they occur: tokens are verified by
**introspection** rather than a signed JWT (§5), and the mock app lives **inside the
`wallet-control-layer` project** beside `sandbox/` rather than at the repo root (§5), so it
shares the one `uv` environment and the one `make check`. Verified end to end against the
official MCP Python client (`make probe-connector`) and the phone in a browser.

```bash
cd wallet-control-layer
make sandbox            # terminal 1: the offline replica
make connect            # terminal 2: service + Payment App under /app, one port (:8010)
make probe-connector    # terminal 3: the official MCP client runs the whole flow
# an agent for real:   claude mcp add --transport http Leash-Wallet http://127.0.0.1:8010/mcp
# the phone:           http://127.0.0.1:8010/app/
```

---

## 1. The short answer

Build the connector as a **remote MCP server with OAuth 2.1**. That is exactly what the
Gmail and GitHub connectors are under the hood. The existing FastAPI service is the resource
server; the mock Payment App is the authorization server. The cardholder pastes one URL, logs
into the app, sees a consent screen in the app's own look, and the agent has its tools.
No API keys, no config files.

```
Claude / ChatGPT / Claude Code           (MCP client, already speaks OAuth)
        │  one URL: https://wallet.example/mcp
        ▼
Connector  = MCP server, Streamable HTTP, mounted at /mcp on the leash service
        │  validates the bearer token, calls the Supervisor
        ▼
Wallet Control Layer  (unchanged: mandates, decide, ledger, audit, step-ups)

Mock Payment App  = OAuth authorization server + consent screen + phone screens
        (login, "connect this agent", step-up answers, connected-agents list)
```

The connector is thin. It re-implements nothing in the engine. It owns three things: the
token check, the mapping from token to cardholder and card, and a curated set of tools.

## 2. What the customer experiences

1. **Add the connector.** In Claude: Settings → Connectors → Add custom connector → paste the
   URL. Claude fetches the server's metadata, registers itself as a client automatically, and
   opens the login page. ChatGPT and Claude Code do the same from the same URL.
2. **Sign in to the app.** The LEASH splash — *Let the agent shop. You still hold the
   leash.* — then the mock's own sign-in: a persona from the data pack, any PIN. Not a
   generic OAuth page.
3. **The onboarding is the consent.** *Leash protects your payments*, then five steps in the
   LEASH design: what Leash looks at, how you usually pay (the card's own history), the
   suggested leash, four purchases to decide, and *Your leash is ready* — which names the
   agent by the name it registered with, the card ending, the scopes as sentences, and the
   one line that is true by construction rather than promised:
   > Claude can never approve its own requests, confirm a mandate, or change your limits.

   **Confirm Leash** is the consent. What the cardholder chose on the way is kept with the
   grant and shown back in the app; the rules that decide come from the next step.
4. **Set up the leash in the chat.** The cardholder types the instruction to the agent in
   their own words. The agent calls `propose_mandate`. The draft appears on the phone with
   every span that became a rule highlighted (the leash-demo already renders this). The
   cardholder confirms **on the phone, not in the chat**.
5. **Shop.** The agent calls `request_payment`. Approve and decline come back in a
   millisecond with the plain-language explanation. A step-up pushes to the phone with the
   120-second countdown; the agent is told "the cardholder has been asked, wait".
6. **Stay in control.** The app has a *Connected agents* page, like Google's third-party
   access page. Revoke there and two things happen at once: the token dies and the mandate is
   revoked. The agent's next call gets a clean "access revoked" answer, not a crash.

## 3. The decisions that make it elegant

- **Consent and confirmation never pass through the agent.** The agent's token can only
  carry scopes for proposing, requesting and reading. The scopes that confirm a mandate,
  resolve a step-up or amend a limit exist only for the app's own session. This is the
  connector-level version of "no LLM in the decision path", and it belongs on the consent
  screen as a sentence.
- **Curated tools, not the OpenAPI dump.** The Python MCP SDK can generate one tool per
  FastAPI route. Don't. The agent sees five verbs (§4). Tool descriptions carry the rules of
  the leash, which is the cheapest way to make a foreign agent behave: fewer declines, better
  demo.
- **The token binds cardholder, card and agent.** A signed JWT: `sub` (cardholder), `card`,
  `client_id` (the agent), `scope`. The connector maps it to the Supervisor. The Supervisor is
  single-tenant today; for the mock, one session per `sub` and nothing else changes.
- **Step-up stays out of band.** MCP has *elicitation*, where the server asks the client to
  ask the user. Tempting for step-ups and wrong for them: the agent mediates the question.
  Use it at most for a compiler open question. Payment consent goes to the phone.
- **Return fast, let the agent poll.** A step-up must not block the tool call for two
  minutes. Return `pending` with the authorization id and a human-readable line;
  `payment_status` resolves it.
- **Errors in cardholder language.** The service already returns explanations. Pass them
  through as the tool result text: "CHF 391.50 exceeds the CHF 250.00 limit you set in the
  app", never "HTTP 409".

## 4. What the agent sees

| Tool | Scope | Backed by | Returns |
|---|---|---|---|
| `propose_mandate(instruction)` | `mandate:propose` | `POST /v1/mandates` | draft id, compiled rules with provenance, "waiting for the cardholder in the app" |
| `whats_allowed()` | `activity:read` | `GET /v1/mandates/{id}` + ledger | active rules in plain language, remaining budget, spending hours |
| `request_payment(order)` | `payment:request` | `POST /v1/decide` | `approve` / `decline` / `pending` + explanation + trust score |
| `payment_status(authorization_id)` | `payment:request` | `GET /v1/step-ups`, audit | resolved decision or still pending, seconds left |
| `recent_activity(limit)` | `activity:read` | `GET /v1/decisions` | the last N decisions as the cardholder would read them |

Optional MCP *resources* for agents that read before they act: `wallet://mandate/current`
and `wallet://ledger/window`.

### Scopes

| The agent may hold | Only the app's own session holds |
|---|---|
| `mandate:propose` | `mandate:confirm` |
| `payment:request` | `stepup:resolve` |
| `activity:read` | `mandate:amend`, `mandate:revoke`, `agent:revoke` |

The right column is what makes the consent sentence "Claude can never…" true rather than
promised.

## 5. Plumbing

### Discovery chain (the MCP authorization spec, so any client finds its way)

1. Client calls `/mcp` without a token → `401` with
   `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`.
2. Client reads that document → it names the mock app as `authorization_servers`.
3. Client reads the app's `/.well-known/oauth-authorization-server`.
4. Client registers itself at `registration_endpoint` (dynamic client registration) and gets
   a `client_id`. Claude also accepts a manually entered id and secret, but DCR is what makes
   it one paste.
5. PKCE authorization-code flow: `/authorize` shows login + consent, `/token` issues an
   opaque token bound to the connector as its `resource`.
6. Every MCP call carries `Authorization: Bearer …`; the connector asks the app whether the
   token is live (**RFC 7662 introspection**, a few seconds of cache) and checks the scope
   against the tool.

Introspection rather than a signed JWT with a JWKS, as the first draft said: it needs no key
material and no dependency (the stdlib has no RSA), and it makes *Revoke* on the phone
immediate — the token dies at its source and the agent's very next call is a 401. An issuer
with a real key infrastructure swaps the verifier; the connector's `TokenVerifier` is one
method.

### The mock Payment App's endpoints

`/.well-known/oauth-authorization-server` · `POST /register` · `GET|POST /authorize` (login +
consent) · `POST /token` · `POST /introspect` · `POST /revoke` — plus the phone screens:
mandate confirmation with every rule highlighting the words it came from, the step-up with
its countdown, connected agents with revoke, recent decisions.

### Who signs in

The personas on the sign-in screen come from the organizers' data pack, not from us —
`load_personas()` in `wallet-control-layer/payment_app/server.py` reads four files under
`viseca-2026/data`:

| File | What it contributes |
|---|---|
| `customers.csv` | the person: `customer_id` and `persona_name`, one persona per customer (20) |
| `accounts.csv` + `cards.csv` | the card behind them: the active *everyday* card, else the one with the most history, and the account's per-transaction limit |
| `authorization_history.csv` | the device the card is normally used from, which becomes the token's `device_id` so the agent's orders do not read as a stranger's phone; and the count of approved purchases, which orders the list richest-history first |

Two details are synthetic and say so on the screen: the card's last four digits are derived
from the card id (the pack has no card numbers), and the PIN is not checked. Everything the
engine judges — familiarity, device, account limit — is the same history the fixtures use, so
a persona who lets an agent shop is judged exactly as the scenarios are.

### Demo — the exact click path

Nothing below needs a developer: one URL, Connect, the onboarding, done. Verified on
2026-09-24 with Claude Code 2.1.280 end to end (the trace of what it sent is one
`LEASH_CONNECTOR_TRACE=1` log; the reference client is `make probe-connector`).

**Claude Code** — no tunnel:

1. `make sandbox` in one terminal; `LEASH_COMPILER=baseline make connect` in another.
2. In the project directory: `claude mcp add --transport http Leash-Wallet http://127.0.0.1:8010/mcp`.
3. Start `claude`. It says *1 MCP server needs authentication · run /mcp*. Type `/mcp`, Enter
   on *Leash-Wallet*, Enter on *1. Authenticate*. The browser opens on the LEASH splash: tap → sign
   in (a persona from the pack, any PIN) → *Leash protects your payments* → the five steps →
   **Confirm Leash**. The terminal says *Authentication successful. Connected to Leash-Wallet.*
   Nothing is typed anywhere: the registration is dynamic and the client id is the app's.
4. In the chat, in this order:
   - *What am I allowed to buy with my card? Ask Leash Wallet.* → `whats_allowed`: no mandate
     yet, what the agent may do, the shops the card already uses.
   - *Here is my instruction for Leash Wallet, pass it on word for word:* + the Household
     budget sentence → `propose_mandate`: the draft id, the compiled rules, *confirm it in
     your Payment App*. Phone (http://127.0.0.1:8010/app/): Open decisions → **Confirm**.
   - *Order a weekly grocery basket for about CHF 70 at* a shop the card already uses →
     `request_payment` → APPROVED, and how much of the CHF 300 window is used.
   - *Place a second order at the same shop for about CHF 65* → PENDING: the phone shows the
     ask card with its countdown — two orders at one shop within minutes that together pass
     the per-order cap. **Approve once**. *Check the status* → approved.
   - Phone: Manage Leash → **Revoke**. The next question in the chat is refused (401); Claude
     Code says Leash Wallet needs signing in again and repeats only what it already knew.

**claude.ai / Claude Desktop** — they run in the cloud, so the MCP endpoint *and* the
authorization server need a public https origin; `make connect` puts both on one:

1. `cloudflared tunnel --url http://localhost:8010` prints `https://<name>.trycloudflare.com`.
   `GET https://<name>.trycloudflare.com/.well-known/oauth-protected-resource/mcp` names
   `…/app` as the authorization server and `…/.well-known/oauth-authorization-server/app`
   shows https endpoints — the service honours `X-Forwarded-Proto`; `LEASH_PUBLIC_URL` is
   for a proxy that does not send it. The session cookie is `Secure; HttpOnly; SameSite=lax`.
2. claude.ai → Settings → Connectors → *Add custom connector* → name **Leash Wallet**, URL
   `https://<name>.trycloudflare.com/mcp` → Add → **Connect**. The popup is the LEASH splash →
   sign in → the onboarding → **Confirm Leash**; the connector then reads *Connected*.
3. In a chat with the Leash Wallet connector enabled, the same lines as above. The phone is
   `https://<name>.trycloudflare.com/app/`, or the local URL on the same laptop.

The demo has its own port, 8010, because it is itself a decision service and `make serve` or
`make serve-live` is usually running on 8000 beside it; a taken port is reported with its
holder, `LEASH_CONNECT_PORT` moves it. It is an extra after the beats in `DEMO.md`; if the
venue Wi-Fi dies, the offline demo is untouched.

### Where it lives

- MCP server: `wallet-control-layer/src/leash/connector/` — `auth.py` (introspection, the
  three scopes), `orders.py` (an order → the event the engine judges), `errand.py` (the
  agent's run: the same `RunSession` a scenario run uses, registered in the Supervisor's
  tables so the phone's `/v1/step-ups` and `/v1/decisions` see it), `render.py` (the words an
  agent reads), `mcp.py` (JSON-RPC over Streamable HTTP by hand, no SDK), `http.py` (the
  routes). Mounted at `/mcp` on the decision service.
- Mock Payment App: `wallet-control-layer/payment_app/`, beside `sandbox/` — infrastructure
  in the same `uv` environment. Not folded into `leash-demo` or `playground`, which each
  already have one job. **Its screens are the LEASH brand's** (Logo-System v1.1): the tokens
  in `static/leash.css`, the signet and wordmark as paths in `brand.py`, the onboarding in
  `ui.py` + `static/onboarding.js`, the Cockpit in `static/cockpit.js`. It borrows nothing
  from the leash demo; each surface carries the brand on its own (spec C3).
- Launchers and checks: `tools/connect.py` (one process, one port), `tools/connector_probe.py`
  (the official MCP client end to end), `tests/test_connector.py`, `tests/test_payment_app.py`.

## 6. Positioning

Google's Agent Payments Protocol and Visa's Trusted Agent Protocol both use *intent mandate*
and *cart mandate* for the objects this project already has. Borrowing that vocabulary in the
tool names costs nothing and makes the connector read as a standard rather than a hackathon
adapter.
