# Decision semantics — normative spec

**This file is the source of truth.** Python in `src/leash/domain/` is one implementation of
it; `tests/vectors/*.yaml` is the executable conformance suite. A port to another language is
correct when it passes the vectors and matches this document.

Status of each rule: `NORMATIVE` (fixed by the platform or the data — not our choice) or
`DESIGN` (our decision — changeable, but change the spec first).

---

## 1. Value types

### 1.1 Money — `NORMATIVE`

Represented as a signed integer count of **centimes** (1 CHF = 100). No binary floating point
at any point in the decision path.

- Parse: `Decimal(str(json_value))`, scale by 100, round **half-even** to integer centimes.
- Format for output: two decimals, half-even.
- FX: `billing_amount_chf = amount × fx_rates[currency]`, fixed table dated 2026-08-01 —
  CHF `1.000000`, EUR `0.950000`, GBP `1.120000`, USD `0.870000`.
- **Prefer the event's `billing_amount_chf`.** Use the table only to convert item lines
  (`unit_price × rate`) which carry their own currency.
- Every CHF comparison uses `billing_amount_chf`. Comparing `amount` against a CHF cap is a
  defect. (Fixture `AU0032`: 260.00 EUR = 247.00 CHF against a 250 cap.)

### 1.2 Time — `NORMATIVE`

Two clocks; never mix them.

| Clock | Fields | Used for |
|---|---|---|
| **Simulated** | `authorization.timestamp` | periods, velocity, familiarity, ordering |
| **Real** | `runtime.received_at`, `deadline_at` | response deadline only |

All comparisons in UTC, timezone-aware — except the days of the week a customer names, which
are read on their own clock, Europe/Zurich (check-spending-hours.md §"Days").

### 1.3 Tri-state fields — `NORMATIVE`

`order_returnable` / `order_cancellable` ∈ `{true, false, unknown, not_applicable}`.

- `true` / `false` — the term is stated.
- `unknown` — **not stated**. This is *uncertainty*, routed to `uncertainty_policy`.
  Coercing it to `false` is a defect.
- `not_applicable` — the term does not apply to this fulfilment type (e.g. a digital item).
  Not a violation on its own, but it *is* a signal about what kind of thing is being bought.

---

## 2. Window semantics — `NORMATIVE`

### 2.1 Rolling period window

For a current attempt `C` and a period of `N` days, a **final approval** `A` is in the window iff

```
C.timestamp − N days  ≤  A.timestamp  <  C.timestamp
```

Lower bound inclusive, upper bound exclusive, current attempt excluded.

**A mandate may carry more than one period rule, and every one of them is enforced.** The
windows are independent: `CHF 300 / 7 days` and `CHF 1000 / 30 days` are two separate
constraints over the same ledger, and a purchase must satisfy both. Enrichment therefore
computes **one figure per distinct `period_days`** in the policy, not one figure. See §9.

- Only **finalized approvals** count. A stepped-up authorization is *pending* and contributes
  nothing until resolved as `approve`.
- A declined attempt never contributes.
- Approvals that fall outside the window have **aged out** and must be dropped.
- Cumulative-since-run-start is **wrong**. (Fixture `AU0011`: rolling = 223.00/300 → approve;
  cumulative = 387.50/300 → wrongly declines.)

### 2.2 Velocity window

`authorization.recent_attempt_count_10m` is supplied by the platform with the same shape:
earlier attempts in this run where `C.ts − 10min ≤ A.ts < C.ts`, **counting all statuses**.
Use the field; do not recompute it differently.

---

## 3. Untrusted text — `NORMATIVE`

Untrusted sources: `items[].item_details`, `merchant.merchant_name`, `purchase_description`.

**Invariant (testable):** no limit, list, threshold, or verdict may be modified by a value
sourced from untrusted text.

Two permitted operations:

1. **Typed fact extraction.** An extractor returns a value of a fixed type or nothing.
   `return_window_days: int | None`, `size: int | None`, `item_kind: enum | None`.
   An extractor that can only emit an integer cannot be argued into emitting a verdict.
   Extracted facts are **claims**, recorded with their source.
2. **Manipulation detection.** Pattern match for imperative/authority framing directed at an
   automated system. A hit emits `merchant_text_manipulation` evidence. It never alters a limit.

Detection patterns (case-insensitive, extend freely — see `domain/sanitize.py`):
`ignore (any |all )?previous instructions` · `^system:` · `note for automated .* agents` ·
`pre-?authoris|pre-?authoriz` · `limits? do not apply` · `approve .* (immediately|without)` ·
`without further checks` · `cardholder is unavailable` · `do not ask` · `bypass`

**Conformance test:** for every fixture, evaluate twice — once as-is, once with detected
manipulation spans removed. The decision must be identical unless
`merchant_text_manipulation` is itself among the emitted reason codes.

---

## 4. Enrichment — `DESIGN`

Computed before checks run, from the data pack (`authorization_history.csv`) and run state.

| Signal | Definition |
|---|---|
| `merchant_prior_approvals` | Count of approved history rows with this `card_id` **and** `merchant_id`. Join on `merchant_id` — **never** `merchant_name`. |
| `device_prior_approvals` | Count of approved history rows with this `card_id` **and** `customer_device_id`. `0` ⇒ novel device. |
| `merchant_lookalike_of` | A merchant the card *has* used whose normalized name is within edit distance ≤ 2 of this merchant's, where `merchant_id` differs **and** `merchant_prior_approvals == 0`. |
| `familiarity_basis` | `history`, or `run` for a card with **no approved history row at all**. On `run` the three signals above count only this run's approvals (engine approvals, and step-ups the customer approved). `run_approvals` is how many there are. See check-merchant-permitted.md §"No history at all" and check-session-integrity.md §"A card with no history". |
| `approved_spend_windows` | `{period_days: Money}` — §2.1 over our ledger, once for **each distinct window the policy names** (§9). Cross-check the shortest against `context.approved_spend_in_period_chf`; log any divergence. |
| `is_duplicate_of` | An earlier attempt in this run with the same `merchant_id`, the same `billing_amount_chf`, an equivalent item set, within the duplicate window, and **no** `related_authorization_id` on the current attempt. |
| `is_requote_of` | `related_authorization_id` is set **and** `related_authorization_status == "declined"`. A re-quote is legitimate commerce. |
| `night_hours` | Simulated timestamp hour ∈ [00:00, 05:00) UTC. A weak signal — never sufficient alone. |

**Duplicate vs re-quote is the discriminator that prevents over-blocking.** `AU0036` repeats
`AU0035`'s exact amount with no link (duplicate). `AU0042` links to declined `AU0037` at a
*different* price (re-quote → legitimate).

---

## 5. Checks and verdicts — `DESIGN`

Each check is a pure function returning one verdict:

| Verdict | Meaning |
|---|---|
| `pass` | Requirement satisfied on the evidence. |
| `concern` | A risk signal, not a breach of a stated rule. |
| `violation` | A stated requirement is definitely breached. |
| `unknown` | The fact needed cannot be established from the evidence. |
| `not_applicable` | The policy contains no such requirement. |

**All checks always run.** Never short-circuit — a complete evidence set is the audit trail,
and a judge asking "what else did you look at?" needs an answer.

### Combination — `DESIGN`

```
if any verdict == violation                    → decline
elif any verdict == unknown                    → uncertainty_policy   (ask ⇒ step_up)
elif concern_score >= STEP_UP_THRESHOLD        → step_up
else                                           → approve
```

- `uncertainty_policy` ∈ `{ask, decline, approve}`; all five scenarios say "Ask me when
  uncertain" ⇒ `ask` ⇒ `step_up`.
- `concern_score` is the weighted sum of `concern` verdicts. Weights and the default
  threshold live in one config block — **never scattered through the checks**, so they can be
  tuned and shown.
- **The threshold may be set per policy.** `policy.step_up_threshold`, falling back to
  `evaluator.STEP_UP_THRESHOLD`, is how a customer says *"ask me more often"* or *"ask me
  less"* (`customer-settings.md` §7). **Lower is stricter** — it takes less to escalate — so
  composition takes the `min` across layers and an amendment may only move it down for free.
  Weights stay global: they encode what a signal *means*, which is the engine's judgement,
  not the customer's. The threshold encodes how much evidence that customer wants before
  being interrupted, which is theirs.
  `evaluator.CONCERN_WEIGHTS` is authoritative and complete: a concern code with no entry
  raises rather than silently scoring zero, and `CheckResult` carries no weight of its own.
- A check may return **one result or several**. One concept sometimes carries several named
  signals — session integrity emits device novelty, velocity and hour separately so each is
  weighted in the one config block and named individually in the audit record.

### Weight tiers — `DESIGN`

| Weight | Meaning | Codes |
|---:|---|---|
| **2.0** | Escalates alone: something is acting against the cardholder's interest | `merchant_text_manipulation`, `merchant_lookalike`, `device_novel`, `unrequested_addon`, `duplicate_order` |
| **1.0** | Needs corroboration: ordinary alone, meaningful in company | `velocity_elevated`, `unusual_hour` |

The line is **evidence of an adversary versus a single ambient signal**. A merchant instructing
the control layer, a shop impersonating one the cardholder trusts, a device never seen on the
card, a seller adding to the basket, the same committed order placed twice — each is one party
acting against the cardholder, and one is enough to ask. Night hours or a burst of attempts are
ordinary in isolation and only mean something together.

### Measured behaviour — `tools/tune.py`

Tuning here can never mean fitting the fixtures: the pack ships no expected decisions
(`metadata.json`: `contains_expected_decisions=false`), and steering the 45 toward a target
would be the prohibited hardcoding done statistically. It means knowing what the config does.

**That is now a test rather than a promise.** `tests/unit/test_no_hardcoding.py` replays all
45 with every identifier — authorization, scenario, request, mandate, profile — replaced by a
consistent bijection under two fixed seeds, and requires the same decisions, reason codes and
customer messages. Every *fact* is left untouched, because facts are what a decision is
legitimately made of. A second test walks the AST of `src/` and fails on a fixture identifier
used as a value anywhere outside a docstring.

Against the current board (17 approve / 20 decline / 8 step_up):

- **Every weight connects to a check that emits it.** Six earlier entries — `merchant_unfamiliar`,
  `cross_border`, `amount_above_norm`, and the three session signals when they lived inside the
  session check — moved nothing. A knob that moves nothing is misleading config.
- **Accumulation is real:** 8 events carry one concern, 2 carry two, 2 carry three; scores span
  2.0 to 4.0.
- **Each knob owns exactly one fixture**, with no cross-coupling —
  `device_novel`→`AU0026`, `duplicate_order`→`AU0036`, `split_order_suspected`→`AU0006`,
  `merchant_text_manipulation`→`AU0040`, `unrequested_addon`→`AU0018`. Any single behaviour can
  be dialled independently.
- **Raising any weight changes nothing**; the system is saturated upward. Only lowering moves a
  decision, which is the safe direction for a control layer to be insensitive in.
- **The SCEN0003 burst is over-determined** (`AU0027`–`AU0030` sit 1.0–2.0 above the threshold),
  while single-signal escalations sit exactly on it by construction.
- **The threshold is on a plateau, not a knife-edge:** the board is unchanged for any value in
  `[0.5, 2.0]`, and the nearest cliff is at 2.5.
- Rationale for the ordering: a definite breach of an instruction the customer wrote outranks
  a soft risk signal; and an unestablished fact must never be resolved silently in the
  agent's favour.

### Why `concern` accumulates rather than triggering alone — `DESIGN`

A single weak signal (novel device on a familiar merchant, under cap — fixture `AU0026`)
should not block ordinary shopping. Several together (novel device **+** unfamiliar merchant
**+** velocity **+** night hours — `AU0027`–`AU0030`) should. And once the session returns to
a known device, the score falls again on its own — recovery is automatic, which is exactly
what SCEN0003 asks for. Sticky "once suspicious, always suspicious" state would be
over-blocking.

---

## 6. Reason-code vocabulary — `DESIGN`, closed set

Adding a code is a spec change. Stable snake_case, emitted in `reason_codes[]`.

**Approvals**
`within_per_order_limit` · `within_period_limit` · `merchant_familiar` · `merchant_meets_requirement` ·
`item_matches_request` · `item_attributes_match` · `order_terms_acceptable` · `session_consistent` ·
`legitimate_requote` · `categories_permitted` · `within_spending_hours` · `goal_already_fulfilled` ·
`order_cancellable` · `within_spending_days`

`goal_already_fulfilled` is a **recorded finding rather than a problem found**: the thing the
customer named was already bought on this errand, which is worth telling them and — by
default — not worth refusing. It appears under **Concerns** too when the customer sets
`repeat_purchase_action: "ask"`, the same one-meaning/two-strengths pattern as
`unrequested_addon`. See specs/check-goal-fulfilled.md.

**Violations**
`per_order_limit_exceeded` · `period_limit_exceeded` · `merchant_not_permitted` ·
`merchant_type_not_permitted` · `item_not_requested` · `item_attribute_mismatch` ·
`unrequested_addon` · `return_window_too_short` · `order_not_returnable` ·
`cart_contradicts_purpose` · `item_category_excluded` · `merchant_category_excluded` ·
`outside_spending_hours` · `order_not_cancellable` · `item_keyword_excluded` · `outside_spending_days`

The last three are **stated rules**, which is why they are violations rather than concerns.
`outside_spending_hours` is not the same finding as the `unusual_hour` concern below: one is
a rule the customer wrote, the other is our inference about risk, and
`specs/check-spending-hours.md` states why conflating them is wrong in both directions.

`unrequested_addon` appears in **both** groups: it keeps one meaning — the cart contains
something not requested — and the **verdict** carries the strength. A stated no-additions
rule makes it a `violation`; a scope merely implied by naming the item makes it a
`concern` at weight 2.0. See specs/check-unrequested-addon.md.

**Concerns** — every code here has a weight in `CONCERN_WEIGHTS` and a check that emits it
`merchant_text_manipulation` · `merchant_lookalike` · `device_novel` · `unrequested_addon` ·
`duplicate_order` · `split_order_suspected` · `goal_already_fulfilled` · `velocity_elevated` ·
`unusual_hour`

`cross_border` is deliberately **not** a signal: the scenario cardholders shop abroad routinely
(`CA0023` has 23 approved Italian rows, `CA0039` 21 US), so scoring foreignness would penalise
legitimate purchases. See specs/check-session-integrity.md.

**Uncertainty**
`return_terms_unknown` · `item_attributes_unknown` · `merchant_type_unknown` ·
`merchant_familiarity_ambiguous` · `merchant_history_unavailable` · `cancellation_terms_unknown` ·
`insufficient_evidence`

`merchant_familiarity_ambiguous` is the card-or-person question: this card has not used
the shop and the cardholder's other cards have. Unknown rather than a breach, because
"shops I have used before" is a statement about the person — see
specs/check-merchant-permitted.md §"Card or person".

`merchant_history_unavailable` is the no-history question: the card has no approved row in
the history file at all, so "never bought there" cannot be established. The shop has not been
approved earlier in this run either. The live API's cardholders are all in this position — see
specs/check-merchant-permitted.md §"No history at all".

**System**
`engine_error_defaulted` · `deadline_risk` · `idempotent_replay`

The first two are `types.NOT_EVALUATED`: they say **no check ran**, not *a check ran and a
fact was missing*. Under `uncertainty_policy: approve` they are floored at `step_up`
(`types.resolve_uncertainty`), because that setting is consent to resolve a doubt about the
purchase, never consent to skip the checks. `decline` is left alone — a customer who asked for
refusals when unsure is not made safer by being asked instead.

---

## 7. Idempotency and failure — `NORMATIVE`

- Key on `authorization.authorization_id` (run-scoped). `source_authorization_id` is the
  public fixture ID and is **not** unique across runs.
- Delivery is **at least once**. A repeat must return the *identical* decision, with
  `idempotent_replay` added to `reason_codes`. Never re-evaluate — state may have moved.
- After a `step_up`, the decision endpoint refuses further automated decisions. Only
  `/resolve` (`approve` | `decline`) applies.
- **The engine never raises.** Any internal error produces a valid decision derived from
  `uncertainty_policy`, with `engine_error_defaulted`. A crash becomes a platform-side
  decline we never got to explain.
- Latency budget: platform deadline is 8 s from event *generation*. Engine target **< 200 ms**;
  if elapsed time approaches the deadline, emit the `uncertainty_policy` outcome with
  `deadline_risk` rather than missing it.

---

## 8. Output — `NORMATIVE` shape, `DESIGN` content

```json
{
  "authorization_id": "AU...",
  "decision": "approve | decline | step_up",
  "reason_codes": ["..."],
  "customer_message": "plain language, names amount + merchant + what happens next",
  "evidence": [{"field": "billing_amount_chf", "value": "247.00"}],
  "engine_version": "leash-x.y.z"
}
```

`customer_message` rules — `DESIGN`:
- Name the amount **in CHF** and the merchant.
- State the rule in the customer's own words ("your CHF 120 per-order limit"), never a code.
- For `step_up`, say what is being asked and how to answer.
- For `decline`, say what would make it acceptable if anything would.
- Never echo untrusted merchant text back to the customer verbatim.
- No internal rule IDs, no stack traces, no jargon.

---

## 9. Hard rules — when several apply to one scope — `NORMATIVE`

The API's `hard_rules` is a list, and nothing in the format stops two entries constraining the
same thing. `PATCH /v1/mandates/{id}` makes that reachable on purpose: it is **tighten-only**,
so a customer narrowing their mandate *adds* a rule rather than editing one.

**The binding cap for a scope is the tightest matching rule, never the first.**

```
candidates = [r for r in hard_rules
              if r.field == "billing_amount_chf"
              and r.scope == <scope>
              and r.operator in ("<=", "<")]
binding    = min(candidates, key=(value, "<" before "<="))
```

Ties on value are broken toward `<`, which excludes the boundary and is therefore the tighter
of the two. No candidates means the check is `not_applicable` — an absent cap is not a cap of
zero.

**Why this is normative and not an implementation detail.** Selecting the first match makes
the decision depend on **list order** — the same mandate, reordered by any serialisation
anywhere between the compiler and the check, decides differently. §7's determinism requirement
rules that out. It is also the only reading under which "tighten" means anything: a customer
who adds `<= 250` beside an existing `<= 400` has said 250, and an engine that answers 400 has
ignored them.

### The period scope carries one window *per stated window* — `NORMATIVE`

Two period rules naming **different** `period_days` are not comparable by value, so neither
can be dropped in favour of the other: 7 days alone permits 1200 in a month, 30 days alone
permits 1000 in an afternoon. **Enforcing both is the only correct answer.**

```
windows  = { r.period_days for r in period rules }        # distinct, all of them
binding  = for each w in windows: min(rules with period_days == w, by (value, "<" first))
verdict  = one result per window; any exceeded window is a violation
```

Within one window the tightest cap binds, exactly as for `purchase`. Across windows nothing
binds anything: each is its own constraint and its own `CheckResult`, so a customer who is
inside their weekly limit and over their monthly one is told which.

**The check reads the window belonging to the rule it is evaluating.** Enrichment computes
the set (`approved_spend_windows`, §4); the check looks up `rule.period_days` in that set.
A rule whose window is absent from the set is `unknown` — never zero — because a missing
figure that silently reads as "nothing spent yet" approves everything. This replaces the older
"enrichment and the check must make the same selection" rule: there is no selection left to
disagree about, only a lookup that either succeeds or admits it did not.

A period rule with **no** `period_days`, or one of zero or less, is not a window and is
`unknown` for the same reason. (The compile guard already refuses to emit one — `llm-compiler.md`
rail 6 — so this is the defence in depth, not the only one.)

**Why this is normative.** Before it, `binding_cap` kept the shortest window and dropped every
other period rule, which made an *add-only* PATCH able to loosen a mandate. Measured against
the engine on 2026-09-21:

```
mandate:              CHF 1000 / 30 days
add-only PATCH:     + CHF  500 /  7 days      ← the guard accepts this: nothing removed
ledger:   CHF 400 approved on each of day −25, −15, −5   (1200.00 in 30d, 400.00 in 7d)
attempt:  CHF 50
  before:  30d window, 1200.00 + 50.00 > 1000.00  → DECLINE
  after:    7d window,  400.00 + 50.00 ≤  500.00  → approve
```

A customer who narrowed their weekly limit had deleted their monthly one. Enforcing every
window is what makes "tighten" mean tighten, and it is what lets a standing preferences layer
carry a period rule at all (`customer-settings.md` §3.4).
