# The mock Payment App

The cardholder's phone and, behind its consent screen, an OAuth 2.1 authorization server. It
stands in for the issuer's real app so the connector (`src/leash/connector/`) can be shown end
to end. Infrastructure, like `sandbox/`: nothing in `src/` imports it.

```bash
make sandbox          # terminal 1 — the offline replica the service talks to
make serve            # terminal 2 — the decision service, with /mcp on it
make payment-app      # terminal 3 — this, on http://127.0.0.1:8081
```

or all of the service-side pieces on one port, which is what a tunnel wants:

```bash
make sandbox
make connect          # service + this app under /app, on :8010 (beside a `make serve` on 8000)
cloudflared tunnel --url http://localhost:8010
```

## The design system

The screens are the LEASH brand's (Logo-System v1.1, decided 2026-09-04): the tokens in
`static/leash.css` — Midnight, Warm, Orange for the grip and for a human decision, Slate,
Mist, no green — and the marks in `brand.py`, the signet in its three cuts and the wordmark,
drawn as paths so no bitmap lives in the repository. Inter is the face. `ui.py` is markup
only and defines no colour of its own. The app borrows nothing from the leash demo any more
(spec C3, *one brand*).

## Screens

| Screen | What it does |
|---|---|
| `/` signed out | the splash: the wordmark, *Let the agent shop. You still hold the leash.*, one tap to the sign-in |
| `/login` | pick a persona from the data pack (the richest history first); any PIN |
| `/authorize` | the onboarding an agent's connect flow lands on: *Leash protects your payments*, then five steps — what Leash looks at, how you usually pay (the card's own history), the suggested leash, four purchases to decide, *Your leash is ready* — and **Confirm Leash**, which is the consent. A cardholder who has been through it opens on the last step |
| `/` signed in | the Cockpit (`static/cockpit.js`): spent and available, the leash's state, open decisions — drafts to confirm with every rule quoting the words it came from, step-ups with their countdown — and *Manage Leash*: rules in force, the per-order limit to tighten, connected agents with **Revoke**, what was chosen when connecting, sign out |

The phone reads the decision service directly — `/v1/mandates`, `/v1/step-ups`,
`/v1/decisions` — exactly as the leash demo does, plus `/connector/proposals` and
`/connector/agents` for who proposed what and how each errand is going. What the onboarding
suggests and what the cardholder chooses is kept with the grant and shown back; the rules
that decide come from the mandate the agent proposes in the chat.

## Personas

The sign-in list is the organizers' data pack, read by `load_personas()` in `server.py`:
`customers.csv` for the person, `accounts.csv` and `cards.csv` for their everyday card and its
account limit, `authorization_history.csv` for the device the card is normally used from
(the token's `device_id`) and the purchase count that orders the list. The last four digits
are derived from the card id and the PIN is not checked; both are said on the screen. The
engine judges a persona's agent against the same history the fixtures use.

## Endpoints an MCP client uses

`/.well-known/oauth-authorization-server` · `POST /register` · `GET|POST /authorize` ·
`POST /token` (authorization_code with PKCE S256, refresh_token) · `POST /introspect` (the
connector, with the shared secret) · `POST /revoke`. Normative detail: `specs/connector.md` §6.

## State

One JSON file, `out/payment-app.json` (`WALLET_APP_STATE`), saved on every change, so a
restart keeps every connected agent. Delete it to forget everything. Set the variable to an
empty string to keep the state in memory.
