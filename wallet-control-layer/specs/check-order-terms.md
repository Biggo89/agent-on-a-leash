# Spec: order_terms

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `order_terms_acceptable`, `return_window_too_short`, `order_not_returnable`,
`return_terms_unknown`, `order_cancellable`, `order_not_cancellable`, `cancellation_terms_unknown`

## Motivation

> SCEN0002 — "…**only if the order can be returned within 14 days or more**, and pay no more
> than CHF 200."

This is the check where the two halves of a single requirement live in two different places
with two different trust levels:

| Question | Source | Trust |
|---|---|---|
| *Can* the order be returned? | `authorization.order_returnable` | **Platform** — trusted |
| For *how long*? | `items[].item_details` | **Merchant** — untrusted |

The duration exists nowhere else in the event. Parsing it is legitimate fact extraction
(`decision-rules.md` §3); it is a *claim*, recorded with its source.

## Why facet-gating is not optional here

**All ten SCEN0001 grocery orders carry `order_returnable = "false"`** — groceries are not
returnable, which is entirely normal. A version of this check that ran whenever the field was
present would decline every household grocery delivery in the pack.

It runs **only** when the instruction states a return requirement. SCEN0001 says nothing about
returns, so the check is `not_applicable` there. Across the pack: 31 attempts `true`, 12
`false` (eleven of them SCEN0001 groceries plus `AU0014`), 1 `unknown` (`AU0016`), 1
`not_applicable` (`AU0043`).

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `authorization.order_returnable` | `true`\|`false`\|`unknown`\|`not_applicable` | `unknown` ⇒ **not stated**, which is uncertainty — never `false` |
| `enrichment.item_facts[].return_window_days` | `int \| None` | `None` ⇒ this line states no window |
| `enrichment.item_facts[].final_sale` | `bool` | Merchant claim that returns are refused |
| `policy.intent_facets[kind=order_terms]` | object \| absent | Absent ⇒ no return requirement |

`facet.require.return_window_days_min` is the minimum acceptable window in days.

### The trusted field wins

`order_returnable` is platform-supplied and authoritative for *whether* returns exist. A
merchant **cannot** talk its way past `order_returnable="false"` by writing "returns accepted
within 30 days" into a product description. The text supplies only the duration, and only once
the trusted field has already said returns are possible.

`final_sale` in merchant text contributes a stated window of **0 days** — a fact about the
product in the same category as a duration, not an instruction.

## Rule

`min_days` = `facet.require.return_window_days_min`.
`stated` = the `return_window_days` values across cart lines, with `final_sale` counting as `0`.

Evaluated in order; the first matching row wins:

| # | Condition | Verdict | Reason code |
|---|---|---|---|
| 1 | no `order_terms` facet | `not_applicable` | — |
| 2 | facet present, `min_days` missing | `unknown` | `return_terms_unknown` |
| 3 | `order_returnable == "false"` | `violation` | `order_not_returnable` |
| 4 | `order_returnable == "not_applicable"` | `violation` | `order_not_returnable` |
| 5 | `order_returnable == "unknown"` | `unknown` | `return_terms_unknown` |
| 6 | `order_returnable == "true"` but no line states a window | `unknown` | `return_terms_unknown` |
| 7 | `min(stated) < min_days` | `violation` | `return_window_too_short` |
| 8 | `min(stated) >= min_days` | **`pass`** — boundary: "14 days or more" is `>=` | `order_terms_acceptable` |

**Row 4 (`not_applicable`):** the term does not apply to that fulfilment type — a digital
voucher cannot be returned at all. If the customer required returnability, an order that
inherently cannot be returned does not satisfy it. See Open questions.

**Multi-line carts** take the **minimum** stated window: an order is only as returnable as its
least returnable line. A line that states nothing is covered by the order-level flag rather
than forcing `unknown` — otherwise `AU0018` (shoe stating 30 days + a protection plan stating
none) would come back uncertain on return terms, when its actual problem is an unrequested
add-on that belongs to a different check.

## Cancellation — `AGREED 2026-09-24`

> Live SCEN0124 — "Book me a hotel in Munich for 3 nights … at most CHF 200 per night,
> **refundable rate only**."

**The requirement key.** `order_terms.require.cancellable: true`. It is the only value that
means anything, because no customer asks for "non-refundable only". So the guard drops a
`false` (`llm-compiler.md` rail 4). The baseline reads it from *"refundable (rate/booking/
fare/ticket) (only)"*, *"free cancellation"* and *"cancellable"*. It never reads it from
*"non-refundable"* or *"nonrefundable"* (lookbehind), nor from *"not refundable"*,
*"never refundable"* or *"no refundable"* (a negation just before).

**The trusted field.** `authorization.order_cancellable`, a platform string: `"true"`,
`"false"`, `"unknown"`, `"not_applicable"`. It is parsed and, until now, was never read. The
item name is not evidence. The live pack sells *"Hotel room, refundable rate"* (IT0171) beside
*"Hotel room, non-refundable rate"* (IT0172), and a name keyword `refundable` matches both,
because `non-refundable` tokenises to `non`, `refundable`. The catalogue points at the field
itself: *"cancellation terms are carried on the order"*. The rule mirrors how
`return_window_days_min` reads the trusted `order_returnable`.

| # | Condition | Verdict | Reason code |
|---|---|---|---|
| C1 | facet without `cancellable: true` | the term is not judged at all | — |
| C2 | `order_cancellable == "true"` | `pass` | `order_cancellable` |
| C3 | `order_cancellable == "false"` | `violation` | `order_not_cancellable` |
| C4 | `order_cancellable == "not_applicable"` | `violation` — like row 4: an order that cannot be cancelled at all does not meet "refundable only" | `order_not_cancellable` |
| C5 | `order_cancellable == "unknown"` or anything else | `unknown` — not stated is uncertainty, never a negative | `cancellation_terms_unknown` |

Evidence: `order_cancellable` and `cancellable_required=true`. The violation's remedy is
*"The same order at a refundable rate would meet your terms."*

**One result per stated term.** A facet stating only `cancellable` is judged on cancellation
alone. A hotel room is `order_returnable: not_applicable`, and rows 2-4 must not decline a
booking whose instruction never mentioned returns. A facet stating both terms returns two
results, returns first, the way `category_exclusion` reports each dimension. A facet stating
neither still returns row 2's `return_terms_unknown`, exactly as before.

**Edits keep it.** `cancellable` is an instruction-only key (`settings.INSTRUCTION_ONLY_KEYS`).
The "Minimum return window" control has no switch for it, so `preferences.apply_settings`
carries it through an edit of that control. `amend.classify` compares it: dropping it widens,
adding it tightens. `compose` keeps it when any layer states it.

**Must not change.** No repo-pack instruction mentions refunds or cancellation, so no facet in
the 45-decision board gains the key (`make diff-decisions`: unchanged).

## Failure mode

The duration comes from untrusted text, so a merchant that omits or garbles it produces
`unknown` → the mandate's `uncertainty_policy` (`ask` ⇒ `step_up`) rather than a silent
approval. Verified against the injection invariant: stripping manipulation spans from the
SCEN0004 fixtures does not remove their return-window phrases, so no decision moves.

## Fixtures

**Exercised by** (SCEN0002, `return_window_days_min = 14`):

| Fixture | `order_returnable` | Text | Expected |
|---|---|---|---|
| `AU0014` | `false` | "clearance line, sold as final sale" | `violation` — `order_not_returnable` |
| `AU0015` | `true` | "returns accepted within 7 days" | `violation` — `return_window_too_short` |
| `AU0016` | `unknown` | "return policy not stated by the seller" | `unknown` → `step_up` |
| `AU0019` | `true` | "returns accepted within **14** days" | **`pass`** — the boundary |
| `AU0012`, `AU0013`, `AU0017`, `AU0020`–`AU0023` | `true` | 30 days | `pass` |
| `AU0018` | `true` | 30 days + a line stating none | `pass` — min of stated is 30 |

**Must NOT change:**

- **All of SCEN0001** (`AU0001`–`AU0011`) — `order_returnable="false"` on every grocery order,
  but no `order_terms` facet, so `not_applicable`. If any of these move, the gating is broken.
- `AU0043` (SCEN0004, `not_applicable`) — no facet in that scenario either.
- `AU0021` stays declined on the per-order limit; `AU0022` on merchant type.

Expected regression: **exactly 3** decisions change — `AU0014` and `AU0015` approve → decline,
`AU0016` approve → step_up. `AU0016` is the pack's only `step_up` so far.

## Vectors

`tests/vectors/order_terms.yaml` — includes the `>=` boundary, each tri-state value, the
final-sale case, the multi-line minimum, and the trusted-field-beats-text case.

## Open questions

- [ ] `not_applicable` → `violation` or `unknown`? Currently `violation`: the customer asked
      for returnability and this order cannot offer it. Stepping up would let them waive the
      requirement for a digital item. Only reachable in the pack via `AU0043`, which has no
      `order_terms` facet, so nothing depends on it today.
- [ ] Should a line that states **no** window in a multi-line cart force `unknown`? Today the
      order-level flag covers it. Revisit if a fixture appears where the unstated line is the
      one that matters.
