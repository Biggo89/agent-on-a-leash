# Quickstart: set up, start and use Leash

Leash (the Wallet Control Layer) decides whether an AI shopping agent may spend a cardholder's
money. Every purchase gets `approve`, `decline` or `step_up` (ask the cardholder), with a
plain-language reason and an audit record.

It runs in two modes. The commands, the API and the UI are the same in both.

| | Offline | Live |
|---|---|---|
| Talks to | A replica of the organizers' API, on this laptop | The organizers' sandbox API |
| Needs | Nothing: no key, no network | The team key, in `wallet-control-layer/.env` |
| Scenarios | The repository's data pack: 5 scenarios (SCEN0000–SCEN0004), 45 purchases | The pack the API serves: 10 scenarios (SCEN01xx), 111 purchases |
| Start | `make sandbox`, then `make serve` | `make serve-live` |
| `/healthz` reports | `"mode": "replica"` | `"mode": "live"` |
| Reset | Restart it | None. Every mandate, run and decision stays on the team's record. |

Live mode was rehearsed end to end on 2026-09-25: all ten scenarios, 111 decisions, 29 step-ups
answered on the phone, and every run completed. For the verified output of every command, see
[`wallet-control-layer/PLAYBOOK.md`](wallet-control-layer/PLAYBOOK.md).

## The five-minute tour

| Step | Offline | Live |
|---|---|---|
| 1. Install once, in `wallet-control-layer/` | `make setup` | `make setup`, then `cp .env.example .env` and set `TEAM_API_KEY` |
| 2. Start, in `wallet-control-layer/` | `make sandbox` in one terminal, `make serve` in another | `make serve-live` |
| 3. Open the demo UI, in `leash-demo/` | `./serve.py` | `./serve.py` |
| 4. In the UI's header, switch from **Replay** to | **Live engine** | **Live sandbox** |
| 5. Pick a scenario, press **Run agent** | Answer the step-ups on the phone | The same, within the platform's real 120-second window |

With nothing running, the UI still replays recorded engine output, and `make demo` prints the
offline demo in the terminal (§4).

---

## 1. What you need

| Tool | Needed for | Install |
|---|---|---|
| `make`, `curl`, `git` | everything | macOS: `xcode-select --install` |
| [`uv`](https://docs.astral.sh/uv/) | the engine. It installs Python 3.11+ and all dependencies itself. | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `python3` | serving the demo UI (standard library only) | included with the macOS command-line tools and with Linux |
| `jq` | the API walkthrough (§6) | included in macOS 15+; otherwise `brew install jq` or `apt install jq` |
| The team key | live mode | issued by the organizers; it goes in `wallet-control-layer/.env` (§2) |
| Node.js | optional: `node check.mjs` (§5) | `brew install node` |
| Claude Code or claude.ai, and `cloudflared` for claude.ai | optional: the Claude connector (§7) | `brew install cloudflared` |
| Postman | optional: using it without code (§8) | [postman.com/downloads](https://www.postman.com/downloads/) |

## 2. Set up (once)

```bash
cd wallet-control-layer
make setup
make verify-data
make test
```

- `make setup` installs Python and all dependencies into a local `.venv`.
- `make verify-data` checks the organizers' data pack against its SHA-256 manifest: `✓ all 18 files match the manifest.`
- `make test` runs about 1,400 tests; none may fail. `make check` also formats, lints and type-checks.

**For live mode**, create `.env` and set `TEAM_API_KEY` in it. `LEASH_BASE_URL` already points
at the organizers' API. `.env` is git-ignored; never commit it.

```bash
cp .env.example .env
```

## 3. Start the system

Open the terminals in `wallet-control-layer/` and leave them running:

| Terminal | Offline | Live |
|---|---|---|
| 1 | `make sandbox`: the replica, on port 8099 | `make serve-live`: the decision service on port 8000, connected to the organizers' sandbox |
| 2 | `make serve`: the decision service on port 8000 | not needed |

`make serve-live` checks the key and the connection before it starts, then downloads the data
pack the live API serves into `out/live-pack`. With a model key in `.env` (§9), it also
compiles the ten live scenarios with the model, which takes about 20 seconds. Offline with a
model key in `.env`, start the service as `LEASH_COMPILER=baseline make serve` to stay offline.

Check it with `curl -s http://127.0.0.1:8000/healthz | jq`. Expect `"status": "ok"` and, under
`upstream`, `"reachable": true` and the mode. Offline, `"reachable": false` means terminal 1 is
not running.

Before a live demo, `make probe-live` checks the real API against every assumption the replica
makes. It runs the scenario named "Connection check" once, as a real run on the team's record.

**Rules of the live sandbox**, each measured on the real platform:

- **One engine per team key.** Two engines on one key are handed each other's purchases. Run
  `make serve-live`, `make replay-live` and the live connector (§7) one at a time.
- **One run at a time.** A step-up waiting on the cardholder holds the team's whole queue, and
  other runs' orders miss their 8-second deadlines behind it. Finish or revoke a run before you
  start the next. The demo UI will not switch scenario while a live run is going.
- **120 real seconds per step-up.** After that, the platform declines the purchase itself, and
  a late answer gets `409 authorization_not_pending`.
- **No reset.** A run that crashes keeps its orders in the queue, and they time out one by one
  (8 seconds each). Let them drain before the next run.
- **The live cardholders have no purchase history.** The first order at each shop asks the
  cardholder, and a yes makes that shop known for the rest of the run. Expect about two
  step-ups per scenario.

## 4. Watch it decide

Run these from another terminal in `wallet-control-layer/`:

| Command | What it shows | Needs |
|---|---|---|
| `make demo` | The offline demo: 3 scenarios, 22 decisions, each with the cardholder's message | `make sandbox` |
| `make replay` | All 5 offline scenarios: 45 decisions, 17 approve · 20 decline · 8 step_up | `make sandbox` |
| `make diff-decisions` | The 45 decisions compared with the committed baseline. Expect `no decisions changed`. | `make sandbox` |
| `make replay-live` | Every live scenario, unattended. Slow: nobody answers the step-ups, so each one waits out its 120 seconds. | the team key, with `make serve-live` stopped |
| `make live-pack` | Downloads the pack the live API serves into `out/live-pack` and compares it with the repository's | the team key |
| `make compile` | How each offline scenario's instruction becomes rules, each rule quoting the cardholder's own words | nothing |
| `make impact` | What the control layer adds over a plain spending limit, on the 45 offline purchases | nothing |
| `make tune` | Every concern weight and how often it fires | nothing |
| `make audit` | The end of the append-only decision trail, from both modes | nothing |
| `make help` | Every other command | nothing |

`make demo`, `make replay` and `make diff-decisions` reset the replica first. Mandates and runs
you created by hand offline are gone afterwards.

## 5. The demo UI and the explainer

**The demo UI** plays the agent's whole errand in three columns: the agent shopping, the Leash
checking each purchase, and the cardholder's phone. Run `./serve.py` in `leash-demo/`. It opens
http://127.0.0.1:8771, and the switch in its header picks where the decisions come from:

| Switch reads | Needs | Decisions come from |
|---|---|---|
| Replay | nothing | recorded engine output for the 45 offline purchases |
| Live engine | `make sandbox` and `make serve` | the decision service, against the replica |
| Live sandbox | `make serve-live` | the decision service, against the organizers' platform |

In both live modes, picking a scenario confirms its mandate and **Run agent** starts the run.
The agent pauses wherever the phone asks, and the phone's countdown is the platform's, in real
seconds.

Keyboard: `space` run or pause · `→` one order · `a` / `d` approve or decline a step-up ·
`1` `2` `4` speed · `r` reset.

To check that the UI still agrees with the engine, run `node check.mjs` in `leash-demo/`. Expect
`All checks passed.`

**The explainer** shows how the system works, with one view for non-developers, one for
engineers, and a Decision Explorer over all 45 offline purchases. It needs no server: from the
repository root, run `open playground/index.html` (on Linux, `xdg-open`).

## 6. Use the API with curl

The decision service on port 8000 is what every UI talks to, and it behaves the same in both
modes. Run this from `wallet-control-layer/` with the service running. Every id is read from the
API, so each block can be pasted as it is, offline or live. With a network connection,
http://127.0.0.1:8000/docs also lets you call every endpoint from the browser.

List the scenarios this mode serves, and pick one. Offline, SCEN0001 is the household budget.
Live, use an id from the list; on our team key, SCEN0135 is the household budget.

```bash
B=http://127.0.0.1:8000
curl -s $B/v1/scenarios | jq -r '.scenarios[] | "\(.id)  \(.name)"'
SCEN=SCEN0001
INSTR=$(curl -s $B/v1/scenarios | jq -r --arg s "$SCEN" '.scenarios[] | select(.id == $s) | .instruction')
```

`INSTR` now holds the scenario's own instruction. To try your own words, set it to any sentence.

### 6.1 Turn the instruction into a mandate

Preview the rules. Each rule quotes the words it came from in `provenance`. Nothing is stored:

```bash
curl -s -X POST $B/v1/mandates/compile -H 'content-type: application/json' \
  -d "$(jq -n --arg i "$INSTR" '{instruction: $i}')" | jq '{rules, guidance}'
```

Create a draft, then confirm it. A draft is not enforced until it is confirmed. The confirm is
the cardholder's consent.

```bash
MID=$(curl -s -X POST $B/v1/mandates -H 'content-type: application/json' \
  -d "$(jq -n --arg i "$INSTR" '{instruction: $i}')" | jq -r .draft_id)
curl -s -X POST $B/v1/mandates/$MID/confirm -H 'content-type: application/json' \
  -d '{"confirmed":true}' | jq '{mandate_id, status}'
```

### 6.2 Let the agent shop

Start the scenario under that mandate:

```bash
RID=$(curl -s -X POST $B/v1/runs -H 'content-type: application/json' \
  -d "$(jq -n --arg s "$SCEN" --arg m "$MID" '{scenario_id: $s, mandate_id: $m}')" | jq -r .run_id)
curl -s $B/v1/runs/$RID | jq '{status, counters}'
```

### 6.3 Answer the step-ups

The run pauses at every purchase the engine will not decide alone, until the cardholder
answers. List what is waiting, then answer the first one (use `"approve"` to say yes):

```bash
curl -s $B/v1/step-ups | jq '.step_ups[] | {authorization_id, seconds_remaining, customer_message}'
AID=$(curl -s $B/v1/step-ups | jq -r '.step_ups[0].authorization_id')
curl -s -X POST $B/v1/step-ups/$AID/resolve -H 'content-type: application/json' \
  -d '{"decision":"decline"}' | jq '{authorization_id, status}'
```

Repeat until `curl -s $B/v1/runs/$RID | jq -r .status` prints `complete`. Offline, SCEN0001
asks twice: AU0006, an order split in two, and AU0007, an add-on nobody asked for. Live, expect
about two step-ups per scenario. A step-up nobody answers within 120 seconds is declined by the
platform, and the run goes on.

### 6.4 Read the decisions

Every decision, with the message the cardholder reads, and the rolling spending window:

```bash
curl -s "$B/v1/decisions?run_id=$RID&limit=50" | jq -r '.decisions | sort_by(.source_authorization_id)[] | "\(.source_authorization_id)  \(.decision)  \(.customer_message)"'
curl -s $B/v1/runs/$RID | jq .window
```

Offline, with both step-ups declined, look at AU0011. It is approved at CHF 223.50 of 300
because earlier orders have aged out of the 7-day window. A simple running total would read
CHF 388.00 and decline it.

### 6.5 See the evidence behind one decision

Take the run's latest step-up. Its audit record holds all 16 checks, passes included, and its
`raw` form is the append-only trail: the engine's decision on one line, the cardholder's answer
on the next.

```bash
ID=$(curl -s "$B/v1/decisions?run_id=$RID&limit=50" | jq -r '[.decisions[] | select(.decision == "step_up")][0].authorization_id')
curl -s $B/v1/audit/$ID | jq '{decision, final_status, checks: [.checks[] | "\(.check_id): \(.verdict)"]}'
curl -s $B/v1/audit/$ID/raw | jq -c '{type, decision, outcome}'
```

### 6.6 The cardholder stays in control

Change a setting, and the engine decides whether the edit narrows or widens the mandate. A
narrowing applies at once (`"kind": "tighten"`). A widening changes nothing until the
cardholder confirms the new draft it returns (`"kind": "widen", "applied": false`).

```bash
curl -s -X POST $B/v1/mandates/$MID/amend -H 'content-type: application/json' -d '{"settings":{"per_order_limit_chf":50}}' | jq '{kind, applied}'
curl -s -X POST $B/v1/mandates/$MID/amend -H 'content-type: application/json' -d '{"settings":{"per_order_limit_chf":500}}' | jq '{kind, applied, draft_id}'
```

Standing preferences belong to the person, not the errand. They apply under every mandate and
can only narrow it. Set one, then clear it again:

```bash
curl -s -X PUT $B/v1/preferences -H 'content-type: application/json' -d '{"per_order_limit_chf":300}' | jq .preferences
curl -s -X PUT $B/v1/preferences -H 'content-type: application/json' -d '{}' | jq .preferences
```

Revoke stops the agent's runs, declines anything still waiting on the cardholder, and ends the
mandate. A new run against it is refused with `mandate_not_active`.

```bash
curl -s -X DELETE $B/v1/mandates/$MID | jq '{status, stopped_runs, declined_step_ups}'
```

### 6.7 Prompt injection and a lookalike shop (offline)

Run SCEN0004, the offline pack's manipulated-agent scenario. Without a `mandate_id`, the
service compiles and confirms the scenario's own instruction. `auto_resolve` declines the
step-ups for you, so the run finishes on its own.

```bash
RID4=$(curl -s -X POST $B/v1/runs -H 'content-type: application/json' \
  -d '{"scenario_id":"SCEN0004","auto_resolve":"decline"}' | jq -r .run_id)
while [ "$(curl -s $B/v1/runs/$RID4 | jq -r .status)" = running ]; do sleep 1; done
curl -s "$B/v1/decisions?run_id=$RID4&limit=50" | jq -r '.decisions | sort_by(.source_authorization_id)[] | select(.source_authorization_id == "AU0037" or .source_authorization_id == "AU0039") | "\(.source_authorization_id)  \(.customer_message)"'
```

AU0037's product text tells automated agents that the cardholder has pre-authorised CHF 900.
The engine declines it on the CHF 400 limit and reports that the text changed nothing. No model
runs in the decision path, so there is no prompt to inject into. AU0039 is from `PixelHarbour`,
one letter away from a shop the cardholder uses. It is declined because the cardholder has no
history with it.

### 6.8 What if the rule were different?

`POST /v1/decide` decides one purchase under any policy you send, and submits nothing, so it
works the same in both modes. The same CHF 20 purchase flips from decline to approve when the
limit goes from 10 to 50:

```bash
for CAP in 10 50; do
  jq --argjson cap $CAP '{event: ., policy: {uncertainty_policy: "ask", intent_facets: [], hard_rules: [{field: "billing_amount_chf", operator: "<=", value: $cap, currency: "CHF", scope: "purchase"}]}}' \
    ../viseca-2026/data/scenario_fixtures/example_authorization_request.json |
  curl -s -X POST $B/v1/decide -H 'content-type: application/json' -d @- | jq -r '"\(.decision): \(.customer_message)"'
done
```

### All endpoints

| Endpoint | What it does |
|---|---|
| `GET /healthz` | Health, and which upstream it talks to (`replica` or `live`) |
| `GET /v1/config` | The checks in evaluation order, the concern weights, and the settings a cardholder may edit |
| `GET /v1/scenarios` | The scenarios of the current mode, with their instructions and compiled rules |
| `POST /v1/mandates/compile` | Preview the rules for an instruction. Stores nothing. |
| `POST /v1/mandates`, then `POST /v1/mandates/{id}/confirm` | Create a draft, then confirm it |
| `GET /v1/mandates/{id}` | One mandate and its rules |
| `POST /v1/mandates/{id}/amend` | Change a setting. A tightening applies at once; a widening returns a draft to confirm. |
| `PATCH /v1/mandates/{id}` | Add rules directly. Tighten-only: a loosening is refused. |
| `DELETE /v1/mandates/{id}` | Revoke |
| `GET` / `PUT /v1/preferences` | Standing preferences, which can only narrow a mandate |
| `GET /v1/preferences/candidates?scenario_id=…` | Settings the cardholder's profile suggests, each quoting it |
| `POST /v1/runs` · `GET /v1/runs/{id}` · `POST /v1/runs/{id}/stop` | Start, watch and stop a scenario run |
| `GET /v1/step-ups` · `POST /v1/step-ups/{id}/resolve` | What is waiting on the cardholder, and the answer |
| `GET /v1/decisions?run_id=…&limit=…` | The decision feed, newest first |
| `GET /v1/audit/{id}` · `GET /v1/audit/{id}/raw` · `GET /v1/audit?limit=…` | One audit record, its raw lines, the latest records |
| `POST /v1/decide` | Decide one purchase under a given policy, without submitting it |

Full contract: [`wallet-control-layer/specs/service-contract.md`](wallet-control-layer/specs/service-contract.md).

## 7. Connect Claude: the Leash Wallet connector

Leash Wallet lets a Claude agent propose a mandate and request payments. Only the cardholder,
on the phone, can confirm a mandate, answer a step-up or revoke. Start it from
`wallet-control-layer/`:

| Offline | Live |
|---|---|
| `make sandbox`, then `make connect` in a second terminal | `uv run python tools/connect.py --live`, with `make serve-live` stopped |

It runs on port 8010, so offline it can run next to `make serve`. It prints the two URLs you
need:

- MCP endpoint: `http://127.0.0.1:8010/mcp`
- The cardholder's phone: http://127.0.0.1:8010/app/. Keep it open in a browser.

**Claude Code:**

```bash
claude mcp add --transport http Leash-Wallet http://127.0.0.1:8010/mcp
claude
```

In Claude Code, type `/mcp`, choose **Leash-Wallet**, then **Authenticate**. The browser opens
the LEASH onboarding: sign in as any persona with any PIN, go through the steps, and tap
**Confirm Leash**. Claude Code does not allow spaces in a server name, so it is `Leash-Wallet`
there.

**claude.ai or Claude Desktop** run in the cloud and need a public URL:
`cloudflared tunnel --url http://localhost:8010` prints `https://<name>.trycloudflare.com`. In
claude.ai, go to **Customize → Connectors → Add → Add custom connector**. Name it **Leash Wallet**, use the URL
`https://<name>.trycloudflare.com/mcp`, then **Add** and **Connect**, and finish the onboarding
with **Confirm Leash**. The phone is at `https://<name>.trycloudflare.com/app/`.

**Then, in the chat:**

| You say | What happens |
|---|---|
| *What am I allowed to buy with my card? Ask Leash Wallet.* | No mandate yet, and the shops the card already uses |
| *Here is my instruction for Leash Wallet, pass it on word for word:* followed by a scenario's instruction (§6) | A draft mandate. Confirm it on the phone: **Open decisions → Confirm**. |
| *Order a weekly grocery basket for about CHF 70 at* one of those shops*, delivered.* | Approved, with the spending window |
| *Place a second order at the same shop for about CHF 65 of pantry staples.* | Pending, because together the two orders pass the per-order limit. Approve it on the phone, then ask Claude to check the status. |
| On the phone: **Manage Leash → Revoke** | The agent is cut off. Its next call is refused. |

To test without Claude, run `make probe-connector` in another terminal. The official MCP
client runs the whole flow and ends with `The official client got onto the leash.` It stays
connected as an agent, so reset before a Claude demo.

To reset, stop the connector and run these from `wallet-control-layer/`:

```bash
rm -f out/payment-app.json out/connector.json
claude mcp remove Leash-Wallet -s local
```

In claude.ai, remove the connector under **Customize → Connectors**. More detail:
[`connector/SETUP-WITH-CLAUDE.md`](connector/SETUP-WITH-CLAUDE.md).

## 8. Without code: Postman

Start the service in either mode, then import `leash.postman_collection.json` and
`leash.postman_environment.json` from `wallet-control-layer/postman/`. Select the
**Leash — local** environment (top right), then send the folders in order, starting with
**0 · Start here**. Open the Postman Console to read each decision in plain language.

Live, set `scenario_id` to a live id such as SCEN0135 in the collection's **Variables** tab.
Folder 3 starts SCEN0002, so it works offline only. Folder 6 calls the organizers' sandbox
directly and needs `team_api_key` in the environment. Guide for non-developers:
[`wallet-control-layer/postman/README.md`](wallet-control-layer/postman/README.md).

## 9. The model compiler (optional)

A language model is used in exactly one place: turning an instruction into rules, once, when a
mandate is created. It never takes part in a decision. Without a key, the deterministic
compiler does the job, and the 45 offline decisions come out the same.

In `.env`, set `SWISSCOM_API_KEY` for Apertus, which is the example's default. A key from
keymaker.ai-weeks.ch expires after 60 minutes. For OpenRouter, set
`LEASH_COMPILER_PROVIDER="openrouter"` and `OPENROUTER_API_KEY` instead. Live, `make serve-live`
then compiles the ten scenarios with the model, which is how the live rehearsal ran.

| Command | What it shows |
|---|---|
| `make compile-llm` | The five offline instructions compiled twice, by the deterministic compiler and by the model, side by side |
| `make replay-llm` | All 45 offline decisions with mandates written by the model. Expect `no decisions changed`. |
| `make compile-invented` | Six instructions the compiler has never seen |

On the API, send `"mode": "llm"` or `"mode": "baseline"` to `POST /v1/mandates/compile`. Every
compiled mandate names its compiler in `compiler` and lists what the guard refused in
`compiler_notes`.

---

## Reset

- **Offline, everything:** stop the terminals with Ctrl-C and start them again. All run state
  is in memory. The audit trail in `out/audit/` is append-only and is kept on purpose.
- **Offline, only the replica's runs and mandates:**
  `curl -s -X POST http://127.0.0.1:8099/v1/team/reset -H 'Authorization: Bearer dev-key'`
- **Standing preferences, in either mode:**
  `curl -s -X PUT http://127.0.0.1:8000/v1/preferences -H 'content-type: application/json' -d '{}'`
- **Live:** there is no reset.

You do not need a reset between scenarios. Every run has its own id and its own ledger.

## Ports

| Port | What | Started by |
|---|---|---|
| 8099 | Offline replica of the organizers' API | `make sandbox` |
| 8000 | Decision service (REST API) | `make serve` (offline) or `make serve-live` (live) |
| 8771 | Demo UI | `leash-demo/serve.py` |
| 8010 | Connector: decision service, MCP endpoint and phone | `make connect` (offline) or `tools/connect.py --live` (live) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `make: command not found` (macOS) | `xcode-select --install` |
| `uv: command not found` | Install uv (§1), then open a new terminal. |
| `"reachable": false`, `upstream_unreachable` or `Cannot reach http://127.0.0.1:8099` | Offline: start `make sandbox`. Live: check the network. If the organizers' sandbox is down, switch to offline; the same commands work there. |
| `make serve-live`: `TEAM_API_KEY is empty` or `the sandbox rejected the team key (401)` | Put the right key in `wallet-control-layer/.env`. |
| `unknown_scenario` | Each mode serves its own scenarios: SCEN0000–SCEN0004 offline, SCEN01xx live. List them with `curl -s $B/v1/scenarios`. |
| `curl` hangs | A proxy is catching localhost. Run `export NO_PROXY=127.0.0.1,localhost`. |
| `address already in use` | An earlier server still holds the port. `lsof -nP -iTCP:8000 -sTCP:LISTEN` shows its PID; stop it with `kill <PID>`. |
| A run stays `running` | It is waiting on a step-up: its own, or live, another run's on the same key. List them with `curl -s $B/v1/step-ups`, then answer (§6.3) or wait out the 120 seconds. |
| `unknown mandate_id` | Offline, the replica was reset or restarted, for example by `make demo`. Create a new mandate. |
| `mandate_not_active` | The mandate was revoked, or its draft was never confirmed. |
| `already_resolved` or `authorization_not_pending` | The step-up was already answered, or its 120 seconds ran out and the platform declined it. |
| A decline mentions a limit "you set in your preferences" that you did not expect | Clear the preferences (see Reset). |
| `make connect`: port 8010 is in use | Run `LEASH_CONNECT_PORT=8020 make connect` and use 8020 in every URL. |

## More

| Document | What is in it |
|---|---|
| [`wallet-control-layer/PLAYBOOK.md`](wallet-control-layer/PLAYBOOK.md) | Every command with its verified output, and what to do when it fails (Part 9: live) |
| [`wallet-control-layer/DEMO.md`](wallet-control-layer/DEMO.md) | The demo beats, the evidence behind each, and answers to judges' questions |
| [`wallet-control-layer/specs/service-contract.md`](wallet-control-layer/specs/service-contract.md) | The HTTP API, including how the live platform holds a run at a step-up |
| [`connector/SETUP-WITH-CLAUDE.md`](connector/SETUP-WITH-CLAUDE.md) | Connecting Claude to Leash Wallet |
| [`wallet-control-layer/postman/README.md`](wallet-control-layer/postman/README.md) | Using Postman, for non-developers |
