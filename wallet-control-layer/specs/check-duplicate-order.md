# Spec: duplicate_order

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `duplicate_order` (**concern** — see below), `legitimate_requote`

## Motivation

> The brief: "Track relevant state over time so rolling limits, retries, **duplicate
> requests**, and prior decisions are handled correctly **without blocking ordinary purchases
> unnecessarily**."

SCEN0004 ships both halves of this, and getting one right while getting the other wrong is the
trap:

| | `AU0036` — a duplicate | `AU0042` — a legitimate re-quote |
|---|---|---|
| Merchant | `ME0022`, same as `AU0035` | `ME0022` |
| Amount | **289.00 — identical** to `AU0035` | **350.00 — different** from `AU0037`'s 520.00 |
| Cart | identical item, quantity and unit price | — |
| Gap | 25 minutes after an **approved** order | 1 day after a **declined** one |
| `related_authorization_id` | *empty* | `AU0037`, status `declined` |
| `recent_attempt_count_10m` | **0** — the platform's velocity field cannot see it | 0 |

Swept across the pack, these are the **only** two rows of their kind: exactly one duplicate
pair and exactly one re-quote.

**Penalising `AU0042` is over-blocking.** The brief lists "retried after a decline" among the
attempts that look alarming and are legitimate. A re-quote at a *lower* price after a decline
is ordinary commerce, and this check gives it an explicit `legitimate_requote` pass rather than
staying silent about it.

## Why the platform's own fields are not enough

`recent_attempt_count_10m` is a **10-minute** window (`runtime.history_window_minutes`), and so
is `context.recent_authorizations`. `AU0036` arrives **25 minutes** after `AU0035`, so both are
`0`/empty on it. Duplicate detection has to come from our own run-scoped memory of what we
decided — which the engine keeps anyway for idempotency and the rolling window.

Platform **redelivery** is a different thing and is already handled: delivery is at least once,
`authorization_id` is the idempotency key, and a repeat returns the stored decision without
re-evaluating. Anything reaching *this* check therefore carries a **new** authorization id — a
genuinely new order that happens to be identical.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `enrichment.is_duplicate_of` | `str \| None` | Prior authorization id this repeats |
| `enrichment.is_requote_of` | `str \| None` | Prior **declined** authorization this re-prices |
| `authorization.related_authorization_id` / `_status` | `str \| None` | The link that marks a re-quote |

### Enrichment rule (`adapters/parse.py`)

An attempt is a duplicate of a prior attempt **in the same run** when all of:

1. the current attempt carries **no** `related_authorization_id` — a linked attempt is a
   re-quote, not a duplicate;
2. same `merchant_id`;
3. same `billing_amount_chf`;
4. same cart signature — sorted `(item_id, quantity, unit_price)` across lines;
5. within the duplicate window, default **60 minutes** of simulated scenario time;
6. **the prior attempt was approved, or is stepped-up and awaiting the customer.**

**Condition 6 is what keeps retries legal.** Repeating an order that was *declined* is a retry,
not a double-spend — nothing was charged the first time. The original reason still applies and
the other checks will reach it again on its own merits; declining it a second time as a
"duplicate" would attach the wrong explanation. The set of committed ids is read from the
ledger the engine already maintains (`approvals` ∪ `pending`), so no new state is introduced.

**Condition 4 is what keeps order splitting legal.** SCEN0001's `AU0005` (CHF 70.00) and
`AU0006` (CHF 65.00) are six minutes apart at the same merchant — same shop, near-same time,
**different amounts and different carts**. Deliberate order splitting is a rolling-limit
question, not a duplicate one, and this check must stay out of it. Same for SCEN0003's
`AU0028`/`AU0030` burst.

## Rule

| # | Condition | Verdict | Reason code |
|---|---|---|---|
| 1 | `is_duplicate_of` set | `concern` (weight **2.0**) | `duplicate_order` |
| 2 | `is_requote_of` set | `pass` | `legitimate_requote` |
| 3 | otherwise | `pass`, **no reason code** | — |

### Why a concern and not a violation

The customer never wrote "do not buy this twice", so this is an inferred signal, not a stated
rule — the same test applied in `merchant_lookalike` and `unrequested_addon`. And because
platform redelivery is handled by idempotency, a duplicate reaching this check is a genuinely
new order for the same thing, which a customer might actually want.

Weight 2.0 equals `STEP_UP_THRESHOLD`, so it escalates on its own: *"you approved this same
CHF 289.00 order at PixelHarbor 25 minutes ago — buy it again?"* That is a useful intervention;
a flat decline on a second identical purchase is over-blocking.

`duplicate_order` moves from the violations group to the concerns group in
`decision-rules.md` §6, and `CONCERN_WEIGHTS` gains `duplicate_order: 2.0`.

## Failure mode

The window is 60 minutes of **simulated** time. A duplicate arriving 61 minutes later is not
caught here — the rolling period limit is what bounds the damage. Widening the window trades
false negatives for false positives on genuine repeat purchases; 60 minutes is a judgement,
not a fact, and lives in one named parameter.

## Fixtures

- **Exercised by:**
  - `AU0036` — `concern` → **`step_up`**. The only decision change.
  - `AU0042` — `pass` with `legitimate_requote`, visible in the approval's reason codes.
- **Must NOT change:**
  - `AU0005`/`AU0006` (SCEN0001 order splitting) — different amounts, different carts.
  - `AU0028`/`AU0030` (SCEN0003 burst) — different amounts.
  - `AU0037` — 265 minutes after `AU0035` and a different amount; outside the window twice over.

Expected regression: **exactly 1** decision changes — `AU0036` approve → `step_up`.

## Vectors

`tests/vectors/duplicate_order.yaml` — includes the duplicate, the re-quote, the
declined-prior retry, and the different-amount near-miss.

## Open questions

- [ ] Should a duplicate of a **stepped-up, not yet resolved** order behave differently from a
      duplicate of an approved one? Today both are duplicates, which is the safe reading: the
      first order may still be approved by the customer.
- [ ] 60 minutes is a guess. If Viseca has a house figure for duplicate-authorization windows,
      use theirs — it is one constant.
