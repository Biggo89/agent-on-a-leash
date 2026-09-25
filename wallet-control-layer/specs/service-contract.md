# Spec: the UI ↔ engine HTTP contract

**Status:** proposed — **needs the frontend devs' sign-off before either side builds**
**Owner:** Nakya (engine side)   **Base URL:** `http://127.0.0.1:8000`

TASKS.md Phase 3 requires this contract to be *"agreed with the frontend devs **before** either
side builds"*. This file is the agreement; `postman/leash.postman_collection.json` is its
executable form — import it and every endpoint below is one click, with no code.

> **Boundary.** The UI never talks to the organizers' sandbox directly, and never holds the
> team API key. It talks only to this service, which owns the key, the Policy IR, the ledger,
> and the audit trail. One place holds run state, so there is nothing to keep in sync.

---

## 0. Shapes shared by every endpoint

Errors use the same envelope as the organizers' API, so one error renderer covers both:

```json
{"error": {"code": "mandate_not_active", "message": "confirm the draft first"}}
```

| Status | When |
|---|---|
| `400` | malformed body |
| `404` | unknown id |
| `409` | state conflict (already decided, mandate not active) |
| `422` | valid JSON, invalid content (loosened `uncertainty_policy`, changed instruction) |
| `502` | the upstream sandbox rejected or was unreachable — `error.upstream` carries its status |

A `decision` is always one of the three exact lowercase strings `approve` · `decline` ·
`step_up`. A `resolution` is `approve` · `decline` only.

---

## 1. Meta

### `GET /healthz`
Unauthenticated. `{"status":"ok","engine_version":"leash-0.1.0","upstream":{"base_url":"…","reachable":true,"mode":"replica|live"},"checks_registered":12}`

Use it as the UI's connection badge. `mode` is `live` when the service was started against the
organizers' URL — the demo should show which one it is talking to.

### `GET /v1/config`
The decision configuration, for a "how does it work" panel: `concern_weights`,
`step_up_threshold`, `deadline_reserve_ms`, `checks` (ids in evaluation order), `data_pack`.

Also `settings`: the catalogue of what a customer may edit, each entry naming the **check that
reads it**. That field is the contract, not documentation — a setting no check reads renders on
the customer's screen as an enforced rule and enforces nothing, so the UI mirrors this table
and `leash-demo/check.mjs` asserts its copy against it (`specs/customer-settings.md` §7).

### `GET /v1/scenarios`
The list the UI boots from, in the shape its panels render: `id`, `name`, `instruction`,
`control_question`, `theme`, `period_days`, `event_count` and the compiled **`ir`**. Separate
from `/v1/config` because the UI needs it on every boot, and `ir` is not optional: the opening
beat of the demo is the cardholder's sentence with every span that became a rule highlighted,
and without it there is nothing to highlight.

`?mode=` picks the compiler, as on `/v1/mandates/compile`; the default is `baseline` (instant,
deterministic). The leash demo asks for `auto`: the live API's instructions carry wording the
baseline was never written for. Each instruction is compiled **once per mode** and cached, in
parallel, so the IR a customer reviewed is the IR `POST /v1/mandates` is sent. Only what the
requested compiler produced is cached; a model fallback is served, labelled in
`compiler_notes`, and retried next time. `make serve-live` warms the `auto` cache at start.

---

## 2. Mandate lifecycle — create · confirm · tighten · revoke

The full lifecycle from `policy-ir.md`, exposed so the authoring UI never needs the sandbox.

### `POST /v1/mandates/compile`  → the **review screen**
```json
{"instruction": "<cardholder's own words>", "mode": "auto"}
```
Returns the Policy IR **without submitting anything**: `rules`, `intent_facets`, `guidance`,
`open_questions`, each carrying its `provenance` (the substring of the instruction it came
from) and `confidence`. This is what the customer reviews before confirming.

Also returns `compiler` (which compiler produced this) and `compiler_notes` (every guard
action taken — what a model proposed and we would not enforce). Render the notes: they are
what makes "the model does not decide" visible rather than asserted (`specs/llm-compiler.md`).

`mode` is optional — `auto` (default), `baseline`, or `llm`. Calling it twice, once with
`baseline`, gives the **side-by-side** the demo uses: the regex floor next to what the model
added.

Idempotent and side-effect free, but **not free**: in `auto` mode with a key configured this
calls a model and takes seconds. Bind it to a *Compile* button, not to every keystroke.

### `POST /v1/mandates`
```json
{"instruction": "…", "uncertainty_policy": "ask"}
```
Compiles (unless the UI passes an edited `ir`), submits the draft upstream, returns
`{"draft_id":"TM…","status":"draft","ir":{…},"hard_rules":[…],"guidance":[…],"open_questions":[…]}`.

**The instruction is hashed at compile time and verified byte-identical before submit.** The
sandbox rejects a changed instruction and the run is lost; the UI must send the cardholder's
text unmodified — no trimming, no smart quotes. If the UI edits an IR it was handed and posts
it back, the hash catches that too: `422 instruction_altered`. And the instruction the platform
echoes is hashed as well — a mismatch is `409 instruction_not_echoed`, raised at creation
rather than surfacing later as a run that refuses to start (`specs/llm-compiler.md`).

### `POST /v1/mandates/{draft_id}/confirm`
Body `{"confirmed": true}`. Returns `{"mandate_id":"TM…","status":"active", …}`.
A draft is not enforceable until this call: it is the customer's consent, and the demo shows it.

### `GET /v1/mandates/{mandate_id}` · `GET /v1/mandates`
The stored resource, including `guidance` and `open_questions` — which are **not** carried on
live events, so the UI must read them here (`technical_details.md` §2).

### `PATCH /v1/mandates/{mandate_id}` — **tighten-only**
`hard_rules` may add, never remove; `uncertainty_policy` may only move `approve → ask → decline`.
A loosening attempt is `422 policy_loosened` / `422 rules_not_preserved` — surface it as a
message, not a retry. A run keeps the snapshot taken at its start; a patch applies to later runs.

**Lowering a cap means sending both rules, not replacing one.** To take a CHF 400 per-order
limit down to 250, send `hard_rules: [{…400…}, {…250…}]`; sending `[{…250…}]` alone is
`422 rules_not_preserved`, because from the guard's side a removal and a tightening look
identical. The engine then enforces **250** — the tightest cap of a scope binds, not the first
(`decision-rules.md` §9), so the superseded 400 is inert. It stays on the record because it is
what the customer originally consented to.

**Unknown keys are refused, not ignored** — the one endpoint where they are. Every field here
is optional, so an unrecognised body is not "the meaningful fields still arrived", it is an
empty patch returning `200` while the mandate never moves. A UI patching the create-shape
(`{"instruction": …}`) would be told its tightening succeeded. There is deliberately no
`instruction` field: a mandate is tightened against its stored rules, never recompiled from
new text — recompiling is `POST /v1/mandates`, which starts a fresh draft the customer confirms.

### `POST /v1/mandates/{mandate_id}/amend` — **the one entry point for an edit**
```json
{"settings": {"per_order_limit_chf": 250}}
```
The caller does not say whether this narrows or widens; the engine classifies it
(`specs/customer-settings.md` §6) and routes accordingly. `settings` uses the same vocabulary
as `/v1/preferences`, so one editor in the UI serves both surfaces, and every key is one the
`settings` table in `GET /v1/config` publishes.

A **narrowing** applies at once, add-only, and says so:
```json
{"kind": "tighten", "applied": true, "mandate_id": "TM…", "ir": {…},
 "amendment": {"kind": "tighten", "changes": [{"setting": "per_order_limit_chf",
   "before": "CHF 400.00", "after": "CHF 250.00", "kind": "tighten", "why": "a lower per-order limit"}]},
 "runs_refreshed": ["RUN_…"]}
```
`runs_refreshed` names the live runs that picked it up. A run keeps its mandate snapshot
upstream, but every layer here can only reduce what the agent may do, so a narrowing lands on
the **next authorization of a run already under way** rather than waiting for the next errand.

A **widening** is not a patch at all. It returns a draft and changes nothing:
```json
{"kind": "widen", "applied": false, "awaiting": "confirm", "draft_id": "TM…", "ir": {…},
 "amendment": {…}, "note": "confirm this to make it enforceable; it binds from the next run"}
```
Confirm it with the ordinary `POST /v1/mandates/{draft_id}/confirm`. Widening what an agent
may do is a new consent moment, not an edit to an old one — and because a run snapshots its
mandate at start, it binds from the next run. **`GET /v1/mandates/{id}` must still show the
old policy until the draft is confirmed**; that is invariant I2 and `tests/test_settings.py`
asserts it.

A **contradiction** is `422 preference_conflict` with both sides named — an empty allow-list
reaches the check as `unknown`, routes through `uncertainty_policy`, and would turn every
order into a step-up the customer could not connect to the setting they just cleared.

### `DELETE /v1/mandates/{mandate_id}` — **revoke**
Sets `status: "revoked"`. The platform then rejects new runs against it before any request
reaches us. **This is a demo beat** (`policy-ir.md`): the clearest answer to "how does the
customer retain control?" is a button that stops the agent.

A revoke also answers every question the mandate still has open, in this order:

1. **Stop** every run bound to the mandate, so nothing new is decided under a permission being
   withdrawn — not even an order another run's loop is handed from the team-global queue (§7).
2. **Decline** each step-up of theirs still waiting on the customer, through the platform's
   `/resolve`, with the message *"Declined: the cardholder revoked the permission this purchase
   relied on."* The revoke *is* the customer's answer. Left pending, the step-up would hold the
   platform's whole team queue for up to 120 s with nobody left to answer it (§5).
3. **Revoke** upstream. The decline goes first because an answer on an active mandate is
   specified by the platform, and one on a revoked mandate is not.

```json
{"mandate_id": "TM…", "status": "revoked", …,
 "stopped_runs": ["RUN_…"], "declined_step_ups": ["AU0044-…"]}
```

Each decline is appended to the audit trail as a `resolution`, beside the original step-up.

---

## 2b. Standing preferences — the customer, not the errand

`specs/customer-settings.md` §2. A mandate is an errand and dies with it. This layer is the
person: it survives every mandate and composes **under** it, tightest-wins, so it can only
ever make a mandate narrower and never needs its own consent moment (invariant I1).

### `GET /v1/preferences` · `PUT /v1/preferences`
```json
{"per_order_limit_chf": 300,
 "merchant_type": {"merchant_category_in": ["groceries", "household"]},
 "no_additions": true,
 "origins": {"no_additions": {"source": "profile", "quote": "avoids gift vouchers"}}}
```
`PUT` replaces the whole document — there is no partial, because a preferences screen shows
everything and saving it whole is the only way *"I cleared that setting"* and *"I did not send
that setting"* stay distinguishable. `origins` is how a setting accepted from the profile keeps
saying so on the review screen.

Refusals are `422` and name the setting:

| Code | When |
|---|---|
| `unknown_setting` | no check reads it — an inert control is worse than a missing one, because the customer believes it protects them |
| `setting_not_standing` | it describes this errand's object (`item_identity`, `item_attribute`) or is the period cap, which cannot be layered (§3.4) |
| `unknown_category` | not in the data pack's closed vocabulary |

### `GET /v1/preferences/candidates?scenario_id=…`
What the cardholder's profile proposes — **proposals a human accepts, never rules**.
`customers.csv` is descriptive prose written by the organizers, not the customer's stated
intent, so deriving enforced rules from it would be the compile problem again with the same
over-blocking risk. Every candidate quotes the field it came from, and `not_offered` says why
each unmapped field stays out, so "why isn't that a setting?" has an answer from the API.

---

## 3. Runs

### `POST /v1/runs`
```json
{"scenario_id": "SCEN0001", "mandate_id": "TM…", "auto_resolve": null}
```
Starts the scenario upstream **and** spawns the run loop that long-polls, decides and submits.
Returns immediately with `{"run_id":"RUN_…","scenario_id":"…","mandate_id":"…","status":"running","total":10}`.

- `mandate_id` **omitted** ⇒ the service compiles the scenario's own `cardholder_instruction`,
  creates and confirms a mandate itself, and reports which one it used. This is the
  one-call path for a non-developer with Postman; it is a convenience, not a shortcut past
  consent, and the response names the mandate so the UI can show what was confirmed.
- `auto_resolve` ∈ `{null, "approve", "decline"}` — demo aid only. `null` (default) leaves
  step-ups waiting for a real human in §5 — and since the platform holds a run at each one,
  the run waits with them. A script that needs a run to finish on its own passes `"decline"`:
  a declined step-up leaves every window where the regression board has it.
- `counters.total` is the platform's own total where it reports one (the replica does). The
  real API does not — it generates each order only once the one before is settled — so the
  service fills it from the scenario's catalogue `event_count`.

### `GET /v1/runs` · `GET /v1/runs/{run_id}`
Progress and live state — the UI's main polling endpoint (1 s is fine):

```json
{"run_id":"…","scenario_id":"…","status":"running|complete|stopped|failed",
 "counters":{"total":10,"delivered":7,"decided":7,"approve":4,"decline":2,"step_up":1},
 "window":{"period_days":7,"approved_spend_chf":"223.00","limit_chf":"300.00",
           "pending_step_up_chf":"45.00"},
 "decisions":[{ /* the §4 decision summary, newest last */ }],
 "error":null}
```

`window` is the rolling-window ledger the platform leaves to us (`decision-rules.md` §2.1) —
worth showing on screen, because "223.00 of 300.00 in the last 7 days" is the clearest
possible evidence that the window rolls. `limit_chf` is the "of 300.00": the tightest cap on
that window in the run's composed policy, so a mid-run tightening moves it. Each entry of
`windows` carries its own.

### `POST /v1/runs/{run_id}/stop`
Stops the loop after the in-flight decision. Does not touch upstream state.

---

## 4. Decisions

### `POST /v1/decide` — evaluate one event, submit nothing
```json
{"event": { /* a complete authorization.request, exactly as the API delivers it */ },
 "policy": {"uncertainty_policy":"ask","hard_rules":[],"intent_facets":[]},
 "run_id": null}
```
The engine's pure decision function over HTTP. Nothing is sent to the sandbox and no ledger is
mutated, so the UI can use it as a **what-if**: change a rule, re-post the same event, show
the decision flip. Returns the §4 decision object below.

`policy` omitted ⇒ the mandate snapshot carried on the event is used. `run_id` supplied ⇒ the
run's ledger and history are used **read-only** for enrichment, so window figures match what
the live loop saw.

The decision object (also embedded in run progress and in the audit record):

```json
{"authorization_id":"…","source_authorization_id":"AU0011","decision":"approve",
 "reason_codes":["within_period_limit"],
 "customer_message":"Approved: CHF 45.00 at …",
 "evidence":[{"field":"…","value":"…"}],
 "checks":[{"check_id":"…","verdict":"pass|concern|violation|unknown|not_applicable",
            "reason_code":"…","detail":"…","concern_weight":null}],
 "score":{"concern_score":0.0,"step_up_threshold":2.0},
 "timing":{"latency_ms":1.9,"budget_ms":7810.0,"guard_tripped":false},
 "engine_version":"leash-0.1.0","decided_at":"…"}
```

`checks` always contains **every** check, passes included. Rendering only the ones that fired
loses the point: the audit value is in the size of the evidence set.

### `GET /v1/decisions?run_id=&limit=`
Recent decisions, newest first. The feed for a live decision list.

Each carries `context` beside the §4 summary: what the engine *derived* rather than was sent —
`purchase_description`, `device_id`, `window_before_chf`, and `enrichment` (prior approvals at
this shop, a lookalike, a duplicate or re-quote, recent orders at the same shop, night hours,
and the manipulation spans with their sanitized excerpts). It is the shape the leash demo's
replayed fixtures carry, so a live order renders with the same evidence as a replayed one. It
is served here only and never written to the audit trail, whose own `enrichment` deliberately
does not store hostile excerpts a second time.

### `GET /v1/audit/{authorization_id}` · `GET /v1/audit?limit=`
The persisted record from [`audit-record.md`](audit-record.md), with any later `resolution`
and `replay` records folded in. `GET /v1/audit/{id}/raw` returns the unfolded lines as
`text/plain` — one JSON object per line, in the order they were appended — for the "show me
the append-only trail" moment.

---

## 5. Step-up — the human confirmation surface

The most demo-visible surface in the build (`GUIDELINES.md` §12) and the reason `step_up`
exists as a third answer at all.

### `GET /v1/step-ups`
Everything waiting on the customer — the notification list:

```json
{"step_ups":[{"authorization_id":"…","source_authorization_id":"AU0016","run_id":"…",
  "asked_at":"…","expires_at":"…","seconds_remaining":97,
  "amount_chf":"179.00","merchant_name":"…",
  "customer_message":"Needs your confirmation: CHF 179.00 at … Approve or decline in the app.",
  "reason_codes":["return_terms_unknown"],
  "evidence":[{"field":"order_returnable","value":"unknown"}]}]}
```

`customer_message` is the notification body — it is written for a person and never contains a
reason code (`decision-rules.md` §8). `expires_at` is the platform's 120-second step-up
window; show the countdown, because an expiring confirmation is a real state the UI must
handle.

### `POST /v1/step-ups/{authorization_id}/resolve`
```json
{"decision": "approve", "customer_message": "Confirmed by the cardholder in the app."}
```
Calls the sandbox's `/resolve`, records the outcome in the run ledger (an approved step-up
**now** enters approved spend — it did not while pending), and appends a `resolution` audit
record. Returns `{"authorization_id":"…","status":"approved","resolved_by":"customer"}`.

`409 not_stepped_up` if the authorization was never stepped up; `409 already_resolved` if a
human already answered. After a `step_up`, the platform refuses further automated decisions —
this endpoint is the only way forward (`technical_details.md` §5).

`409 authorization_not_pending` if the platform closed the window before the answer arrived.
The answer is **not** applied; the platform has already declined the purchase itself
(`decision_source: "timeout"`, reason `step_up_expired`). The step-up is then marked
`resolved: "expired"` and counts under `counters.final.declined`, so it stops reading as
waiting on the customer.

### On the real platform, a step-up holds the run

Verified against the organizers' sandbox on 2026-09-24, and modelled by the offline replica
since the same day — so an offline rehearsal behaves as a live one:

- While a step-up waits on the customer, the platform generates nothing more for that run. The
  run resumes the moment `/resolve` lands — or when the window closes, which the platform
  records as a decline.
- Every poll hands the pending request back at once, with envelope `status:
  "pending_step_up"`, instead of long-polling. The run loop waits on it (1 s between polls) and
  sends nothing: re-sending the `step_up` is refused with `409 step_up_resolution_required`.
- Expiry is lazy: the platform settles an expired step-up on the next request that touches it,
  which the waiting loop provides within a second.

So `expires_at` counts from when the engine asked, not from when a screen shows the question.
A UI that replays decisions at human speed must count down from `seconds_remaining`, in real
seconds.

---

## 6. What the UI owns, and what it must not do

**The UI owns:** the instruction editor and IR review screen, the confirmation dialog, the
decision list, the step-up notification and its answer, the mandate-revoke control.

**The UI must not:** hold the team API key · call the organizers' API directly · re-implement
any decision logic (`POST /v1/decide` exists so it never has to) · modify the cardholder's
instruction text · treat `step_up` as a decline · display a `reason_code` to the cardholder
(they are for the audit panel; `customer_message` is the human-facing text).

## Open questions for the sign-off conversation

- [ ] Does the UI poll `GET /v1/runs/{id}` (agreed default, 1 s) or do we add SSE? Polling is
      one fewer thing to break on stage; SSE is ~20 lines if the frontend wants it.
- [ ] Who displays `open_questions` at confirmation time — and does an unanswered one block
      confirmation, or just warn? (`policy-ir.md` rule 3 says the customer resolves ambiguity.)
- [ ] Does the demo run one scenario at a time, or several concurrently? The service supports
      concurrent runs; the screen may not want to.

---

## 7. The decision queue is team-global

`GET /v1/decision-requests/next` is scoped to the **team key**, not to a run. Two engines
started against the same key — two people both running `make serve`, or a stale process from
before a restart — will each be handed the other's authorizations.

The runner handles this in two steps:

1. **Route it.** If the delivered `run_id` belongs to a run this process owns, hand the
   envelope to that run's runner, so it is judged against that mandate's facets and counted in
   that run's window.
2. **If nobody owns it, answer without judging it.** We must still answer — a missing decision
   becomes a platform decline nobody ever explained — but we must not apply *this* run's
   policy. `intent_facets` are per-mandate and are **not carried on the wire**, so borrowing
   ours applies the wrong rules with full confidence.

   Measured: `AU0035`, a compliant CHF 289 monitor purchase, was declined as
   `cart_contradicts_purpose` + `merchant_type_not_permitted` when handed to a run enforcing
   *"road-running shoes"*. Evaluating with **no** facets is no better — the facet-gated checks
   stand down and it would approve things the real policy declines, which is the dangerous
   direction.

   So an unowned authorization routes to the **event's own** `uncertainty_policy` (which *is*
   on the wire, in the mandate snapshot) with the single reason code `insufficient_evidence`,
   and is written to the audit trail as `type: "unowned"` rather than as a decision this engine
   made about a purchase it understood. Nothing touches this run's ledger, window or counters;
   the authorization is remembered so a redelivery still replays rather than re-deciding.

**Operationally:** one engine per team key. `GET /healthz` reports the `upstream.base_url` and
`mode` so it is obvious when two people are pointed at the same live sandbox.
