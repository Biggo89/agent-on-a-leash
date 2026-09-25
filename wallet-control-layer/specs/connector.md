# Spec: the connector — how an agent is let onto the leash

**Status:** implemented 2026-09-24 · **Owner:** Nakya
**Code:** `src/leash/connector/` (the agent-facing surface) · `payment_app/` (the mock issuer app)
**Design note:** `../../connector/README.md` · **Tests:** `tests/test_connector.py`, `tests/test_payment_app.py`

The Wallet Control Layer decides; something has to let an agent *ask*. This is that surface,
built the way the Gmail and GitHub connectors are built: one URL, an OAuth consent, a small set
of tools. Nothing here decides anything — every verdict still comes from `domain.evaluator`,
and every order an agent places is judged by the same `RunSession` a scenario run uses.

**Name.** The connector is **Leash Wallet**: `serverInfo` `{"name": "leash-wallet", "title":
"Leash Wallet"}`, and `resource_name` in the protected-resource document. A client registers it
under that name. Claude Code accepts only letters, digits, `-` and `_` in a server name, so
there it is `Leash-Wallet`.

---

## 1. Shape

```
agent host (Claude, ChatGPT, Claude Code)
   │ POST /mcp  — JSON-RPC 2.0 over Streamable HTTP, Authorization: Bearer <token>
   ▼
connector (leash.connector, mounted on the decision service)
   │ introspects the token against the app (RFC 7662), maps it to a cardholder + card
   │ turns an order into an authorization.request, evaluates it, records it
   ▼
Supervisor tables: sessions · runners · mandates · audit   ← the same ones /v1/* reads

Payment App (payment_app/): authorization server + the cardholder's phone
   /.well-known/oauth-authorization-server · /register · /authorize · /token · /introspect · /revoke
   /  (the phone: the onboarding as the consent, the Cockpit after it)
```

**Invariant C1 — one state.** An agent errand is a `RunSession` registered in
`Supervisor.sessions` and `Supervisor.runners`. Consequences, all verified by tests: its
step-ups are listed by `GET /v1/step-ups` and answered by `POST /v1/step-ups/{id}/resolve`;
its decisions appear in `GET /v1/decisions`; a tightening made in the app reaches it through
`Supervisor._refresh_sessions` like any other run.

**Invariant C2 — no second decision path.** `Errand.request()` performs exactly the steps of
`ScenarioRunner._decide()` minus the submit: `parse_event` → `evaluate` → ledger → audit.
An errand's audit lines carry `upstream.channel = "connector"` and `submitted = false`.

## 2. Scopes — the grammar of what an agent can hold

| Scope | Sentence on the consent screen | Tools it unlocks |
|---|---|---|
| `mandate:propose` | draft a shopping mandate for you to confirm | `propose_mandate` |
| `payment:request` | ask to pay with your card — every request is checked against your rules | `request_payment`, `payment_status` |
| `activity:read` | read your rules and what it has already spent | `whats_allowed`, `recent_activity`, both resources |

There is no scope that confirms a mandate, answers a step-up, amends a limit or revokes. Those
are the app's own session (`/v1/*`, called by the phone), never a token an agent can carry. The
authorization server refuses any other scope at `/authorize` (`invalid_scope`), and the
connector refuses a tool whose scope the token lacks with a tool error naming the scope and
the sentence. A client that asks for no scope gets all three.

## 3. The tools

| Tool | Backed by | Returns |
|---|---|---|
| `whats_allowed()` | the Supervisor's mandate copy + the errand's window + `HistoryIndex` | rules as sentences quoting the instruction, budget left, drafts waiting, shops the card uses |
| `propose_mandate(instruction)` | `Supervisor.create_mandate` | the draft id, compiled rules, open questions; "waiting for the cardholder" |
| `request_payment(order)` | `Errand.request` | `approve` / `decline` / `step_up` with the customer message; on step-up the seconds left |
| `payment_status(authorization_id)` | the errand's decided set and step-ups | `approved` / `declined` / `pending` (+ seconds) / `expired`; once a step-up is answered, `customer_message` says what the cardholder did and `asked_message` keeps the question |
| `recent_activity(limit)` | the errand's decisions | newest first, as the cardholder reads them |

Every text answer is plain language (`render.py`); reason codes travel only in the structured
object. Order shape and validation: `orders.py`. A malformed order is a tool error, never a
JSON-RPC error and never a decision.

### The order → event mapping (`orders.py`)

| Event field | Source |
|---|---|
| `merchant.*` | the data pack's record when the name matches exactly after `normalize_name`; else a synthetic id `ME-AGENT-…`, category `unknown` |
| `items[].item_category` | the catalogue's category when the name is a catalogue product; else the agent's; else `unknown` |
| `amount`, `items_subtotal`, `delivery_fee` | computed from the lines with `Decimal`, half-even to centimes; the agent cannot state a total |
| `billing_amount_chf` | `amount × fx[currency]` from the pack's rates |
| `customer_device_id` | the token's `device_id` — the device the consent was given from |
| `recent_attempt_count_10m` | earlier attempts of this errand in the last 10 minutes |
| `related_authorization_status` | the decision of the named earlier order of this errand |
| `deadline_at` | now + 8 s, so the deadline guard sees the platform's budget |
| `timestamp` | the real clock, UTC |

Merchant matching is exact, not fuzzy, on purpose: a shop whose name merely resembles a known
one must stay a different merchant, or `check_merchant_lookalike` could never fire.

## 4. Step-ups without a platform

An errand keeps its own clock. `expires_at = asked_at + step_up_timeout_seconds` (from
bootstrap, 120 s by default). On every later call that looks (`request`, `status`,
`snapshot`), a step-up past its window is closed as `expired`: the ledger releases it, a
`resolution` line with `resolved_by: timeout` is appended, and `final_status` folds to
`declined`. A late answer from the phone is refused with 409.

## 5. Lifecycle

| Event | Effect |
|---|---|
| agent proposes | `Proposal` recorded (draft id, agent, cardholder); `GET /connector/proposals` names it |
| phone confirms | the Supervisor's copy turns `active`; the next tool call starts an errand |
| phone confirms a newer draft | the running errand stops as `superseded`; a fresh one starts with a fresh ledger |
| phone revokes the agent | app: this cardholder's consent and tokens for the agent deleted (another cardholder's consent to the same registered client is untouched — grants are keyed by agent *and* cardholder) → connector: this cardholder's errands stopped, their mandates revoked upstream → next call 401 `invalid_token` |
| mandate revoked elsewhere | noticed within `MANDATE_RECHECK_SECONDS` (5 s) on the next order; the errand stops |
| token expires | the app's `/introspect` answers inactive → 401; the client refreshes and continues |
| the service restarts | the proposals — agent, cardholder, instruction, the compiled IR — come back from `LEASH_CONNECTOR_STATE`, and the mandate's status is re-read upstream, so the agent's next call finds its mandate active and the phone lists it again; the errand's window starts at zero, the audit trail keeps what was approved |

## 6. Transport and discovery

* `POST /mcp`: one JSON-RPC message or a batch; requests answered as `application/json`,
  notifications and client responses with `202`; `GET`/`DELETE /mcp` are `405`. Protocol
  versions echoed when known (`2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05`), else
  `2025-06-18`. `Origin` must be this host or localhost (DNS-rebinding guard).
* `401` carries `WWW-Authenticate: Bearer realm=…, resource_metadata="…/.well-known/oauth-protected-resource/mcp"`;
  with no token at all there is no `error` parameter (RFC 6750 §3.1).
* `GET /.well-known/oauth-protected-resource[/mcp]` (RFC 9728) names the app under
  `authorization_servers` and the three scopes.
* The app serves RFC 8414 metadata, RFC 7591 registration (public clients with
  `token_endpoint_auth_method: none`, or a secret), PKCE S256 required, single-use codes,
  refresh rotation, RFC 7662 introspection behind a shared secret, RFC 7009 revocation.
  The registration response echoes only the metadata that was registered: an absent
  `client_uri` stays absent, never an empty string — a client that validates the response
  as URLs-or-nothing (Claude Code, verified 2026-09-24) refuses the whole flow otherwise.
* Public URL: `LEASH_PUBLIC_URL` when set, else the request's own base (which honours
  `X-Forwarded-Proto` behind a tunnel). Mounted mode (`tools/connect.py`): the app lives under
  `/app` of the same origin, and the path-aware metadata document is served at
  `/.well-known/oauth-authorization-server/app` as RFC 8414 requires.

## 7. What is mock, and what is not

Mock: the personas (from the data pack), the PIN (any), the introspection secret (shared
configuration), the state (one JSON file). Not mock: the flow — discovery, registration,
consent, PKCE, introspection, revocation — and the scope grammar. An issuer's real app replaces
`payment_app/` without the connector changing.

**Invariant C3 — one brand.** The app's screens are the LEASH brand's (Logo-System v1.1):
its tokens in `payment_app/static/leash.css` — Midnight for text and primary actions, Warm
for the splash and the mark's circle, Orange only for the grip and where a human decides,
never as text, no green anywhere — and its marks in `payment_app/brand.py`: the signet in
the three cuts the board prescribes by size (master from 48 px, small cut from 24, the
loopless 16-px cut below), the wordmark as paths. `payment_app/ui.py` defines no colour of
its own, and the app borrows nothing from the leash demo: the demo is the pitch surface, the
app is the cardholder's, and each carries the brand on its own.

### The onboarding is the consent

When an agent's authorization request arrives (`GET /authorize`), a signed-out cardholder
sees the LEASH splash, whose one tap leads to the sign-in and back to the same request, the
whole request URL carried in `next`. A signed-in cardholder sees the onboarding
(`ui.onboarding_page`): every step on one page, one shown at a time by
`static/onboarding.js`, the form usable without it.

| Step | Screen | What it shows, and where from |
|---|---|---|
| — | *Leash protects your payments* | the three promises; *Set up Leash* |
| 1 | *Leash uses your past payments* | what Leash looks at; *Allow* continues, *Later* skips to step 5 |
| 2 | *How you usually pay* | the card's own approved purchases in the last 90 days of the record (`spending_pattern`): how many, how many at merchants with an earlier approval, the middle half of the amounts as the typical range, and a sentence built from those counts and the pack's budget-style label — never a judgement of the person; *Adjust* goes to step 4 |
| 3 | *This is your suggested leash* | a per-purchase cap — the top of the typical range rounded up to fifty francs, never above the account's per-transaction limit — and the three *ask first* rules |
| 4 | *What should Leash do for you?* | four purchases, the storyboard's amounts at the cardholder's most-used shop, *Allow automatically* or *Ask me first* each |
| 5 | *Your leash is ready* | the agent by its registered name, the card ending, the rules as chosen, the scopes it asked for as sentences, and the sentence the scope grammar makes true: *«agent» can never approve its own requests, confirm a mandate, or change your limits.* **Confirm Leash** is `POST /authorize` with `decision=approve`; *Not now* is `access_denied`. |

What the cardholder chose travels in the form's `setup` field (JSON: `used_history`,
`suggested_cap_chf`, `choices` A–D) and is kept with the grant and in the app's `profiles`
table per cardholder, with `onboarded_at`; `GET /api/me` returns it. A cardholder who has
been through it once opens later requests on step 5 directly. A malformed `setup` is ignored,
never a failed consent.

**The setup decides nothing.** The rules that decide come from the mandate the agent
proposes from the cardholder's instruction in the chat, confirmed on the phone (§5). The
setup is what the app suggested and what the cardholder chose; the Cockpit shows it under
*Manage Leash* and says so. Writing it into the engine's standing preferences would change
decisions, which is a separate decision (AGENTS.md §1).

### The Cockpit

The phone after sign-in (`static/cockpit.js`): spent and available — the errand's window
against its period cap, else approved agent spend against the account's monthly limit; the
leash's state — *Leash active* with a running errand, *A mandate to confirm* with a draft
waiting, *Leash connected* with a grant and no mandate, *Leash off* otherwise; the binding
per-order and period caps; the open decisions count. A pending step-up is a dark card with
its countdown and *Approve once* in orange. *Open decisions* lists drafts, every rule quoting
the instruction, and step-ups; *Manage Leash* the rules in force with their provenance, the
per-order limit to tighten, the connected agents with *Revoke*, the setup, and sign out;
*Spending* every window, the account's limits and all agent decisions. It reads the same
endpoints the leash demo reads and writes only the cardholder's verbs.

## 8. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `WALLET_APP_URL` | `http://127.0.0.1:8081` | where the connector introspects; the authorization server it advertises. `tools/connect.py` pins it to its own `/app` over loopback, whatever `.env` says |
| `WALLET_APP_MOUNT` | unset | set to `/app` by `tools/connect.py`: advertise the app under this service's own base |
| `WALLET_INTROSPECTION_SECRET` | `dev-introspection-secret` | shared by app and connector |
| `WALLET_APP_STATE` | `out/payment-app.json` | the app's memory; empty string keeps it in memory |
| `LEASH_PUBLIC_URL` | unset | the connector's public base when the process cannot see it |
| `LEASH_SERVICE_URL` | `http://127.0.0.1:8000` | where the app reaches the service (revoke); `tools/connect.py` pins it to its own port |
| `LEASH_CONNECT_PORT` | `8010` | the one-process demo's port — its own, so it runs beside `make serve` on 8000 |
| `LEASH_SERVICE_BROWSER_URL` | = `LEASH_SERVICE_URL` | where the phone's JavaScript reaches it; `""` = same origin |
| `LEASH_CONNECTOR_STATE` | unset; `tools/connect.py` sets `out/connector.json` | where the connector keeps its proposals across restarts; empty keeps them in memory |
| `LEASH_CONNECTOR_TRACE` | unset | `1` logs what a client sends to `/mcp` (headers, protocol version, session id, each method, the `initialize` parameters) and what the app sees at `/register`, `/authorize`, `/token` — never a token. The log to read when a new client misbehaves |
