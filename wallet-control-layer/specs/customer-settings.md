# Spec: customer settings — editing the wallet policy

**Status:** implemented (2026-09-21) — all three tiers in §11
**Owner:** Nakya   **Reason codes:** none new — composition reuses the existing checks

**What shipped.** `domain/settings.py` (the catalogue), `domain/provenance.py` (typed
sources), `domain/compose.py` (the layers), `domain/amend.py` (the classifier),
`domain/preferences.py` (the standing layer), `adapters/profile.py` (candidates),
`POST /v1/mandates/{id}/amend`, `GET·PUT /v1/preferences`,
`GET /v1/preferences/candidates`, `GET /v1/scenarios`, and in `leash-demo` the tappable
mandate rows, the per-setting sheet with its blast radius, and the preferences screen.
I1–I4 are `tests/unit/test_compose.py`, `tests/test_settings.py` and `make diff-decisions`;
the 45-decision board is unchanged.

**Tier 3 followed:** two new checks (`category_exclusion`, `spending_hours`), a per-customer
escalation threshold, and profile prose compiled into candidates by the same compiler and the
same guard as an instruction. Five new reason codes, spec'd first in
`check-category-exclusion.md` and `check-spending-hours.md` and added to `decision-rules.md`
§6's closed set. The board did not move: both checks are gated on a facet no pack instruction
produces, and the exclusion regex is proven unable to fire on the five.

**Tier 2 followed:** enrichment computes one approved-spend figure per window
(`Enrichment.approved_spend_windows`), `check_period_limit` emits one result per window, and
`policy.period_caps` replaced the shortest-window selection. Period limits are therefore
layerable and editable, and `decision-rules.md` §9's open question is closed. The board did
not move — no pack instruction names two windows.

Raised in the Viseca Q&A: *the Wallet Control Layer should let the customer edit settings and
rules so the policy fits their profile and preferences.* The brief says the same thing in one
clause we have half-answered:

> "The customer must also be able to **tighten, update, or revoke** the wallet policy."
> — `challenge.md`, Objective, Frontend

Before this spec we did `tighten` (one slider, one field) and `revoke` (one button). We did
not do `update`, and we had no notion of a customer profile at all. This spec closes both, and
it argues that the second one — standing preferences — is the more valuable half, because it
is the only part of the system that survives past a single shopping errand.

Companion to [`policy-ir.md`](policy-ir.md) (the shape), [`llm-compiler.md`](llm-compiler.md)
(who may write into it) and [`decision-rules.md`](decision-rules.md) §9 (which rule binds).
This file owns **who may change it afterwards, and what that costs.**

---

## 1. What existed before this spec, exactly

| Capability | Where | Scope |
|---|---|---|
| Compile an instruction into rules + facets | `compile/`, `POST /v1/mandates/compile` | whole instruction |
| Review before consent | the compile response; `leash-panel.js` renders it | read-only |
| Confirm | `POST /v1/mandates/{id}/confirm` | the consent moment |
| **Tighten** | `PATCH /v1/mandates/{id}`, add-only | **`billing_amount_chf`, `scope: purchase`, and `uncertainty_policy` — nothing else** |
| Revoke | `DELETE /v1/mandates/{id}` | whole mandate |

In `leash-demo` the editable surface was one range input bounded above by the current cap, so
it was physically impossible to express anything but a narrowing of one number. Everything
else the customer agreed to — the retailer type, the return window, the
familiarity requirement, the no-additions rule, the period cap, the uncertainty policy — was
rendered as static text in `mandateCard()` and could not be touched.

**The gap in one sentence:** the mandate was compiled *from* the customer and then frozen
*against* them, and nothing about the person — `customers.csv` was loaded by
`adapters/datapack.py` and read by nobody — reached a decision.

---

## 2. Three layers, not one — `DESIGN`

A "setting" is not one thing. Three different objects are being conflated, with three
different consent semantics, and separating them is most of the design.

```
Layer 0   ACCOUNT        accounts.csv per_transaction_limit_chf / monthly_limit_chf
          platform-set, not editable by us, not negotiable       NORMATIVE
Layer 1   PREFERENCES    standing, per-customer, survives every mandate
          "never above CHF 300 on one order, whatever I tell the agent"
          "I do not buy gift cards"   "ask me when you are unsure"       editable
Layer 2   MANDATE        compiled from one instruction, confirmed once
          "black running shoes, size 43, under CHF 200, returnable"      tighten-only
```

`effective_policy = compose(account, preferences, mandate)` and **composition takes the
tightest of each constraint**. That single rule is what makes the whole feature safe, and §4
states it as a testable invariant.

Why layer 1 is the valuable one: layer 2 is an errand and dies with it. Layer 1 is the
customer. `customers.csv` already describes exactly this — `budget_style`,
`shopping_preferences`, `typical_spending`, `travel_pattern` — for twenty personas, and the
brief's own phrase is *"a customer-managed wallet policy"*, singular and persistent, with the
shopping instruction as a separate input. We built the instruction half. Layer 1 is the half
the brief named first.

---

## 3. Composition — `NORMATIVE`

### 3.1 Hard rules

**Concatenate.** Nothing else. `decision-rules.md` §9 already says the tightest matching rule
of a scope binds, and `domain/policy.py::binding_cap` already implements it, so a three-layer
merge of caps is `layer0.rules + layer1.rules + layer2.rules` and the existing selection does
the work. Each rule carries a `source` (§5) so the UI and the customer message can name the
layer that bound.

**This is true for `scope: purchase` and false for `scope: period`.** See §3.4, which is the
most important paragraph in this document.

### 3.2 Intent facets

`domain/policy.py::facet()` returns the **first** facet of a kind, so composition must emit
**one merged facet per kind** — two facets of one kind means the second is silently inert,
which is the defect `llm-compiler.md` rail 11 exists to prevent.

| Kind | Merge | Tighter direction |
|---|---|---|
| `merchant_familiarity.prior_approvals_min` | `max` | higher |
| `merchant_type.merchant_category_in` | set **intersection** | smaller set |
| `order_terms.return_window_days_min` | `max` | higher |
| `no_additions` | present if present in **any** layer | present |
| `item_identity`, `item_attribute` | **mandate only** — see below | — |

`item_identity` and `item_attribute` describe *this errand's object*. A standing preference
that says "size 43" is a category error, and a preferences API that accepts one is offering a
setting that will silently mean the wrong thing on the next purchase. **Refused at the
boundary with `422 setting_not_standing`,** not merged.

**An empty intersection is a conflict, not a policy.** `check_merchant_type` returns `UNKNOWN`
on an empty `merchant_category_in` (`merchant.py:127`), which routes through
`uncertainty_policy` and — on the default `ask` — turns every single order into a step-up with
the message *"the kind of shop your instruction allows could not be established"*. The
customer would have no way to connect that to the preference they just saved. So the empty
intersection is detected **at edit time**, refused with `422 preference_conflict`, and the UI
names both sides: *"Your preference allows groceries only; this errand asks for a sports
retailer. Nothing would be buyable."*

### 3.3 Uncertainty policy

`max` over `approve(0) < ask(1) < decline(2)` — the order already in `sandbox/server.py:38`.

### 3.4 The period scope — why it was blocked, and what unblocked it — `NORMATIVE`

**Resolved in Tier 2.** Period rules now compose like any other rule. The history is kept
because it is the argument for why composition is safe at all, and because the defect it
describes was reachable from the API before this feature existed.

`binding_cap` used to resolve competing period rules by taking the **shortest window** and
dropping every other period rule outright. That was a documented limitation of single-window
enrichment, and layering turned it from an edge case into the default case — an account
monthly ceiling plus an instruction weekly ceiling is the *ordinary* shape of a layered policy.

And it did not merely under-enforce. **It broke the tighten-only guarantee.** Measured against
the engine on 2026-09-21:

```
mandate:                 CHF 1000 / 30 days
add-only PATCH:        + CHF  500 /  7 days      ← the guard accepts this: nothing removed
ledger:      CHF 400 approved on each of day −25, −15, −5   (1200.00 in 30d, 400.00 in 7d)
attempt:     CHF 50

before the "tightening":  30d window, 1200.00 + 50.00 > 1000.00   → DECLINE
after  the "tightening":   7d window,  400.00 + 50.00 <=  500.00  → approve
```

A customer who narrowed their weekly limit had deleted their monthly one.

**The fix, `decision-rules.md` §9:** every window a policy names is enforced. Enrichment
computes one figure per distinct `period_days`; `period_caps` returns the binding rule per
window; `check_period_limit` emits one result each. Within a window the tightest cap binds,
exactly as for `purchase`. Across windows nothing binds anything, because neither is strictly
safer than the other.

Three consequences:

1. **Layering a period rule is now monotone**, which is what I1 needs: a rule for a new window
   adds a constraint, and a rule for an existing window can only lower the cap that binds it.
   So `period_limit_chf` is a standing setting and the preferences layer may carry one.
2. **`classify` compares window by window.** A lower cap on a window is a tightening; dropping
   a window is a widening; swapping `CHF 1000 / 30 days` for `CHF 500 / 7 days` is a widening
   *because a ceiling went*, not because period edits are categorically suspect.
3. **A window enrichment did not compute is `unknown`, never zero.** A missing figure read as
   "nothing spent yet" approves everything. This replaced the old "enrichment and the check
   must make the same selection" rule: there is no selection left to disagree about, only a
   lookup that either succeeds or admits it did not.

The smaller footgun from the same code path is closed by the same rule. A period rule saved
**without** `period_days` used to get window `0`, win the shortest-window contest, displace
every real period rule, and leave enrichment computing no window at all — a weekly CHF 300
ceiling silently becoming a per-order CHF 1000 one. It is now `unknown`, and
`preferences.validate` refuses to store one.

**Still true:** the account's `monthly_limit_chf` is *available* to layer now, and is not
layered by default. `adapters/profile.py` returns it for display and contributes only
`per_transaction_limit_chf` to the policy. Turning it on is a product decision about whether a
platform limit should silently tighten an errand, not a technical one — §13.

**Which account.** Layer 0 is the account behind **the card the platform names when the run
starts**: `fixture_profiles[].card_id` on the real API, `profile.card_id` on the replica
(`client.run_card`), falling back to the run resource if the start response omits it. Only when
neither names a card known to the pack does it fall back to the scenario's attempts
(`purchase_attempts.authority_id → scenario_authorities`). The fallback is what the repo pack
supports. The first source is the only one the live API supports: since 2026-09-24 it serves
its own pack, whose scenarios have no attempts in any file (`adapters/livepack.py`). Same rule,
same number, a different place to read the card from. A run whose card cannot be resolved is
judged without layer 0 rather than refused, and says so in the log.

## 4. The invariants — `NORMATIVE`

These are the claims to make in front of a judge, and each is a test, not a promise.

**I1 — Layering never loosens.**
> For every fixture event `E` and mandate policy `P`, and any preferences layer `L`:
> `decide(E, compose(L, P))` is never more permissive than `decide(E, P)`,
> on the order `decline < step_up < approve`.

Testable over all 45 fixtures × a generated set of preference layers. Same family as the
existing injection invariant in `tests/unit/test_injection_invariant.py`, and the same value:
it is mechanical, not argued. **Holds only under §3.4's restriction** — with period rules
layered on today's engine it is false, and the counter-example above is the proof.

**I2 — A widening never applies without a new confirmation.**
> An amendment classified `widen` produces a *draft*. `GET /v1/mandates/{id}` returns the
> unchanged policy until `POST /v1/mandates/{draft}/confirm`.

**I3 — Uncertain classification widens.**
> Any amendment `classify` cannot prove is a tightening is classified `widen`.

The same asymmetry as everything else in this codebase: `policy-ir.md` rule 3 (low confidence
becomes a question, not a rule), `llm-compiler.md`'s safety floor, `decision-rules.md` §5
(`unknown` never resolves in the agent's favour). A false `widen` costs one tap. A false
`tighten` spends money on a guess.

**I4 — An empty preferences layer moves nothing.**
> `make diff-decisions` reports no change on the 45-decision board with layer 1 empty.

The regression guard. Everything in this spec is additive or it is wrong.

---

## 5. Provenance generalises — `DESIGN`

Today every rule and facet carries `provenance: str`, the verbatim span of the instruction it
came from, and the demo highlights those spans in the customer's own sentence. That mechanism
is the best thing in the build and it does not survive layering as-is: a rule from the
preferences layer has no span in the instruction, and a merged facet has two origins.

So `provenance` becomes a typed source, and every row in the mandate card can say where it
came from:

```yaml
provenance:
  - source: instruction        # the existing case — quote highlights in the sentence
    quote: "pay no more than CHF 200"
  - source: preferences        # standing; quote is the preference's own label
    quote: "Never more than CHF 300 on one order"
  - source: profile            # seeded from customers.csv, accepted by the customer
    quote: "avoids gift vouchers"
  - source: amendment          # edited in the app
    quote: "tightened to CHF 250 on 24 Sept"
  - source: account            # platform limit, not editable
    quote: "account limit CHF 1200"
```

Backwards compatible: a bare string is read as `[{source: instruction, quote: <string>}]`, so
`markSpans` in `leash-panel.js` keeps working and the five pack mandates render unchanged.

This also fixes the customer message. `customer-message.md` requires the rule be stated in the
customer's own words; with layering, *which* words matter. A decline caused by a standing
preference must say so — **"CHF 391.50 exceeds the CHF 250.00 per-order limit you set in your
preferences"** — or the customer reads their instruction, sees CHF 400, and concludes the
engine is wrong. The binding rule's `source` is already available where the detail string is
built; this is a wording change in `checks/limits.py`, not a new reason code.

---

## 6. The amendment classifier — `DESIGN`

One pure function, and it is where the intellectual content of this feature lives.
Proposed home: `domain/amend.py` (`domain/` stays pure — this touches no clock, no I/O).

```python
def classify(current: Policy, proposed: Policy) -> Amendment:
    """-> kind: 'tighten' | 'widen' | 'conflict', with a per-field account of why."""
```

| Change | Classification |
|---|---|
| `purchase` cap value decreased, or a new `purchase` cap added | `tighten` |
| `purchase` cap value increased, or a cap removed | `widen` |
| any `period` rule added, removed or changed | `widen` (§3.4) |
| `uncertainty_policy` toward `decline` | `tighten` |
| `uncertainty_policy` toward `approve` | `widen` |
| a facet added | `tighten` |
| a facet removed | `widen` |
| `prior_approvals_min` ↑ / ↓ | `tighten` / `widen` |
| `return_window_days_min` ↑ / ↓ | `tighten` / `widen` |
| `merchant_category_in` becomes a strict subset | `tighten` |
| `merchant_category_in` becomes a superset, or merely overlaps | `widen` |
| `merchant_category_in` becomes empty | `conflict` |
| anything unrecognised | `widen` (I3) |

Routing:

- `tighten` → `PATCH`, applies immediately, no consent moment, **add-only** (the superseded
  rule stays on the record because it is what the customer originally agreed to).
- `widen` → a new draft from the **current mandate's rules with the amendment applied**, which
  the customer confirms. This is not a workaround, it is the honest reading of what a mandate
  is. There is deliberately no `instruction` field on `PATCH` for exactly this reason
  (`service-contract.md` §2).
- `conflict` → refused with both sides named.

A run keeps the mandate snapshot it started with (`supervisor.py:199-220`), so a widening binds
from the **next run**. The UI says so and offers *"Restart this errand under the new mandate"*
— one `POST /v1/runs` against the new id.

---

## 7. What is editable, and what is deliberately not

The governing rule, from `llm-compiler.md`: **a setting no check reads is not "unsupported",
it is inert** — it renders as an enforced rule on the customer's screen and enforces nothing.
So the editable set is derived from `FACET_REQUIRE_KEYS` and the registered checks, and nothing
else ships.

**Editable (a check reads it today):**

| Setting | Lands in | Read by | Layer |
|---|---|---|---|
| Per-order limit CHF | `hard_rules` `scope: purchase` | `check_per_order_limit` | 1, 2 |
| When unclear (`ask`/`decline`) | `uncertainty_policy` | `evaluator.combine` | 1, 2 |
| Only shops I have used (min prior approvals) | facet `merchant_familiarity` | `check_merchant_permitted` | 1, 2 |
| Kinds of shop allowed | facet `merchant_type` | `check_merchant_type` | 1, 2 |
| Minimum return window | facet `order_terms` | `check_order_terms` | 1, 2 |
| Nothing added I did not ask for | facet `no_additions` | `check_unrequested_addon` | 1, 2 |
| The item, and its attributes | facets `item_identity`, `item_attribute` | `check_item_matches_request`, `check_item_attributes` | **2 only** (§3.2) |
| Period limit + window | `hard_rules` `scope: period` | `check_period_limit` | 1, 2 — since Tier 2 (§3.4) |
| Things I never buy | facet `category_exclusion` | `check_category_exclusion` | 1, 2 |
| Hours the agent may buy | facet `spending_hours` | `check_spending_hours` | 1, 2 |
| How often to ask me | `step_up_threshold` | `evaluator.combine` | 1, 2 |

**Not editable, and the reason matters:**

| Tempting setting | Why not |
|---|---|
| `international_enabled`, card country rules | `cross_border` is deliberately not a signal (`decision-rules.md` §6): these cardholders shop abroad routinely and scoring foreignness penalises legitimate purchases. No check reads it, so the control would be inert. |
| Per-signal concern weights ("ignore the night") | `CONCERN_WEIGHTS` lives in exactly one block on purpose (`AGENTS.md` §4). Weights encode what a signal *means*, which is the engine's judgement; the **threshold** encodes how much evidence that customer wants before being interrupted, which is theirs. So the threshold is editable and the weights are not. |
| Account monthly limit (layer 0) | Layerable since Tier 2, and deliberately still not layered by default — §13. |
| Anything derived from `item_details` or `merchant_name` | Untrusted text may never modify a limit (`decision-rules.md` §3). Not negotiable. |

**Ask-me-more-often sensitivity** shipped in Tier 3 as exactly that: `policy.step_up_threshold`
falling back to the global default, three positions — *At the first sign · Normally · Only if
serious* — carrying the measured values `1.0 / 2.0 / 2.5`. `tools/tune.py` reports the board
unchanged across `[0.5, 2.0]` with the nearest cliff at 2.5, so a free slider would imply a
precision the data does not have. It is also the clearest widening in the build: *"only ask me
when it is serious"* turns four step-ups into approvals, countable on the screen before you
confirm it.

---

## 8. Profile seeding — `DESIGN`

`customers.csv` carries `background`, `shopping_preferences`, `typical_spending`,
`budget_style`, `travel_pattern`. It is **descriptive prose written by the organizers, not the
customer's stated intent**, so turning it into rules is the compile problem again with the
same over-blocking risk — and without even the excuse that the customer wrote the words.

Resolved the same way `llm-compiler.md` resolves low confidence: **the profile proposes
candidates, the customer accepts them.** Nothing derived from a profile is ever enforced
without a tap. Each candidate quotes the profile text as its `provenance` (§5), so the review
screen the compiler already earned is reused rather than reinvented.

| Field | Candidate | Honest? |
|---|---|---|
| `budget_style` | uncertainty policy, and the §7 sensitivity setting | Yes — categorical field, categorical setting |
| `shopping_preferences` (prose) | e.g. CU0001 *"avoids gift vouchers"* → a deny-list facet | Needs a check that does not exist. Tier 3 |
| `accounts.per_transaction_limit_chf` | a layer-0 per-order ceiling | Yes — a real number from the data, not invented |
| `accounts.monthly_limit_chf` | a layer-0 period ceiling | **Blocked**, §3.4 |
| `travel_pattern`, `home_region` | — | Nothing reads them. Inert. Do not offer. |

**ASSUMPTION:** that a profile-derived preference is acceptable to Viseca as an explicit
opt-in and not as an automatic default. Worth one question at the event — if they want it
automatic, I1 still holds and only the UI changes, but the consent story gets weaker and I
would argue against it.

**What we will not do:** infer a spending number from prose. There is no number in
`shopping_preferences` and inventing one from `budget_style: careful` would be a fabricated
limit wearing the customer's name. `budget_style` maps to *how often we ask*, never to *how
much may be spent*.

---

## 9. API surface

Additions to `service-contract.md` §2. Everything else is unchanged.

```
GET    /v1/preferences                  the standing layer, with sources
PUT    /v1/preferences                  replace it; classify vs the previous; 422 on conflict
GET    /v1/preferences/candidates       profile-derived proposals, each with provenance
POST   /v1/mandates/{id}/amend          the one entry point for an edit
```

`POST /amend` takes a partial policy and returns one of:

```json
{"kind": "tighten", "applied": true,  "mandate_id": "TM…", "hard_rules": [...],
 "effect": {"remaining_orders_affected": 3}}

{"kind": "widen",   "applied": false, "draft_id": "TM…", "awaiting": "confirm",
 "diff": [{"field": "per_order_limit_chf", "from": "250.00", "to": "400.00"}],
 "note": "binds from the next run"}

{"error": {"code": "preference_conflict",
           "message": "your preferences allow groceries only; this errand asks for a sports retailer"}}
```

New error codes: `amendment_requires_consent`, `preference_conflict`, `setting_not_standing`,
`period_layering_unsupported`. Existing `policy_loosened` keeps its meaning for a raw `PATCH`.

**Also needed, and unrelated to this feature but on its path:** `leash-demo`'s `http.js:42`
calls `GET /v1/scenarios`, which `service/app.py` does not define — live mode 404s on boot
today. Any settings UI that is more than a mock needs live mode to work, so this gets fixed
alongside.

---

## 10. The UI, in `leash-demo`

The editor belongs in the **You** column and nowhere else. `playground` explains the system
and gets, at most, a read-only panel showing the three layers composing; it does not get an
editor. Two front-ends, two jobs.

### 10.1 The mandate card becomes tappable

`mandateCard()` already builds rows of `{ico, k, v, prov}` (`phone-panel.js:143-194`). Each
row becomes a button opening a sheet for that one setting. No new screen real estate — which
matters, because the README documents a 634px-available-versus-752px-wanted height budget at
an 800px viewport, and that budget is already spent.

A row's `prov` line gains the source: *from "pay no more than CHF 200"* · *from your
preferences* · *tightened in the app, 24 Sept* · *your account limit*.

### 10.2 The sheet: one setting, its blast radius, and what it costs

Three parts, in this order:

1. **The control**, typed to the setting — a stepper for money, a chip multi-select for
   categories, a segmented control for uncertainty.
2. **The effect, live.** *"3 of the agent's remaining orders would be declined at CHF 250."*
   `onPreview` → `previewCap` already does exactly this for the cap (`mock.js:354`); generalise
   it to `previewAmend(policyPatch)` over the queue. **This is the most convincing thing in the
   whole feature** — a policy editor that shows consequences before you commit is a different
   product from one that does not.
3. **The cost**, from `classify`:
   - `tighten` → a primary **Apply** button. Instant.
   - `widen` → **Confirm the new mandate**, with the diff, and the sentence *"this widens what
     the agent may do, so it needs your confirmation — and it binds from the next errand."*
   - `conflict` → both sides named, Apply disabled.

**The asymmetry is the demo.** Narrowing is one tap; widening costs a confirmation. Today the
slider's `max` is the active cap, so widening is not merely refused — it is unrepresentable,
which teaches the customer nothing. Letting the control move up and then *explaining the
price* is a better product and a much better thirty seconds on stage.

### 10.3 A Preferences screen

Reached from the phone header (`phone__hd`), not from the mandate — it outlives the mandate.
Contains: the standing settings from §7, the profile-derived candidates from §8 as accept/
dismiss cards, and one line that makes the layering visible:

> Your per-order limit is **CHF 200** — from this errand.
> Your preferences cap it at CHF 300, your account at CHF 1200. The tightest one applies.

That sentence is the whole architecture, in the customer's own screen, without a diagram.

### 10.4 Files

| File | Change | ~LOC |
|---|---|---|
| `src/ui/settings-sheet.js` | new — the editors, one per setting kind | 280 |
| `src/ui/prefs-screen.js` | new — the standing layer + candidates | 180 |
| `src/ui/phone-panel.js` | rows become buttons; header entry point; sources in `prov` | 120 |
| `src/adapters/mock.js` | `amend`, `preferences`, `previewAmend`, `classify` | 160 |
| `src/adapters/http.js` | the same against the service | 60 |
| `src/core/client.js` | interface + `SETTING_META` (label, unit, tighten direction, check it feeds) | 70 |
| `src/ui/app.css` | sheet, chips, diff rows | 140 |
| `check.mjs` | every editable setting maps to a check that reads it; a tightening never turns a decline into an approve | 60 |

`SETTING_META` is worth naming: it is the single table that keeps the UI from offering a
control no check reads, and `check.mjs` asserts it against `/v1/config`'s check list — the same
trick `SIGNAL_META` already uses for concern weights.

---

## 11. Work breakdown

Tiered, because this is 8–10 focused hours against a 36-hour event and beats a feature freeze.

**Tier 1 — answers the Q&A and the brief's "update". ~4h engine + ~5h UI. ✅ built.**
`domain/amend.py` · `compose()` in `domain/policy.py` · typed provenance · `/amend` ·
`/v1/preferences` · the sheet · the preferences screen · I1–I4 as tests · fix `/v1/scenarios`.
Ships the per-order limit, uncertainty, familiarity, merchant type, return window,
no-additions. **No period settings.**

**Tier 2 — multi-window enrichment. ✅ built.** `Enrichment.approved_spend_windows`,
`policy.period_caps` / `period_windows`, one `CheckResult` per window, `period_windows.yaml`
(15 vectors), per-window classification, and `period_limit_chf` back as a standing setting.
`decision-rules.md` §9's open question is closed. The board did not move.

**Tier 3 — ✅ built.** Per-customer `step_up_threshold`, exposed as three **measured**
positions rather than a slider (`make tune-sweep`: flat across [0.5, 2.0], nearest cliff at
2.5) · `category_exclusion`, the deny-list an allow-list cannot express · `spending_hours`,
a stated rule deliberately distinct from the `unusual_hour` concern · profile prose through
the compiler, so a candidate that cannot quote the words it came from does not survive.

What remains open is in §13 and in each check's own spec: a timezone for the hours rule, a
conflict between an allow-list and a deny-list naming the same category, and whether the
account's `monthly_limit_chf` should be layered by default.

---

## 12. Fixtures and vectors

- **Exercised by:** all 45, via I1 as a property test. New unit fixtures for `classify` over
  every row of the §6 table.
- **Must NOT change:** the whole 45-decision board with an empty preferences layer (I4).
  `make diff-decisions` after every step.
- **Vectors:** `tests/vectors/policy_composition.yaml` — a layered policy in, a decision out,
  including the empty-intersection conflict, the boundary where two layers state the same cap,
  and the period case that must be refused rather than composed.
- **Unit:** `tests/unit/test_amend.py` (classification, total and pure),
  `tests/unit/test_compose.py` (I1 over the fixture set), `tests/unit/test_preferences_api.py`
  (I2 — a widening never applies without confirm).

---

## 13. Open questions

- [ ] Does Viseca want profile-derived preferences **opt-in** or **automatic**? §8's ASSUMPTION.
      Opt-in is the stronger consent story and the weaker convenience story. **Built opt-in**:
      `GET /v1/preferences/candidates` proposes and nothing is enforced without a tap.
- [ ] Is the preferences layer per **customer** or per **card**? `cards.csv` has
      `card_purpose` (`everyday`, `travel`, `mobile_and_online`) and CU0008 runs two cards, so
      per-card is expressible and probably what a real wallet does. Per-customer is simpler and
      enough for the demo. Ask at the event.
- [ ] Where does the preferences layer **live** in production? We hold it in the supervisor
      beside the IR, which is right for a prototype and wrong for a product — in the real
      system it is customer data in the one app, and the engine reads it. Say so rather than
      imply we solved it.
- [ ] Should a `widen` also be offered as *"just for this errand"* — a one-run exception rather
      than a permanent mandate change? It is what a customer actually wants when the agent is
      blocked mid-task, and it is a different object again (a scoped, expiring grant). Real
      product question; out of scope for the event.
