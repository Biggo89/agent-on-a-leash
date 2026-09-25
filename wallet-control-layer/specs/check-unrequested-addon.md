# Spec: unrequested_addon

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `unrequested_addon` (as a **violation** or a **concern** — see below)

## Motivation

> SCEN0004 — "Buy the 27-inch monitor I chose… **Do not add anything I did not ask for.**"
> SCEN0002 — "Replace my worn road-running shoes in size 43…" *(the pack's own description of
> this scenario lists "unrequested additions")*

Three cart lines in the pack are things the customer never asked for:

| Fixture | Scenario | Added line | Amount |
|---|---|---|---|
| `AU0007` | SCEN0001 | "Fragrance and beauty gift" (`cosmetics`) | CHF 32.00 |
| `AU0018` | SCEN0002 | "Extended protection plan" (`subscriptions`) | CHF 29.00 |
| `AU0041` | SCEN0004 | "Extended protection plan" (`subscriptions`) | CHF 79.00 |

`AU0018` is the one that matters: shoes at CHF 165 plus a CHF 29 plan totals CHF 194, **under**
the CHF 200 cap, from the right retailer, in the right size, with a 30-day return window. Every
other check passes it. SCEN0002's control question — *"Can the solution tell a valid payment
that matches the request from one that quietly does not?"* — is asking precisely about this.

## Two strengths, and why

Only **SCEN0004** states the rule outright. SCEN0001 and SCEN0002 never say "don't add
anything" — the scope is *implied* by naming a specific thing to buy.

That difference is real, so the check reflects it:

| Basis | Verdict | Outcome | Rationale |
|---|---|---|---|
| `no_additions` facet present (SCEN0004) | `violation` | `decline` | The customer stated the rule. Following it is not over-blocking. |
| Only an `item_identity` facet (SCEN0001, SCEN0002) | `concern`, weight **2.0** | `step_up` | The scope is inferred, not stated. Ask rather than refuse. |

Weight 2.0 equals `STEP_UP_THRESHOLD`, so an unrequested add-on escalates on its own.

**Why not decline `AU0018` outright.** The customer wants shoes. Declining the whole order
leaves them without the thing they asked for because a seller bundled a service plan onto it.
Asking — *"TrailSpark added a CHF 29.00 extended protection plan you did not ask for"* — keeps
the shoes reachable and leaves the choice with the customer. Over-blocking is a named failure
mode, and this is exactly the shape it takes.

## Scope boundary

Last of the three item checks:

| Check | Question | `AU0018` |
|---|---|---|
| `item_matches_request` | Is what was asked for present? | pass — the shoes are there |
| `item_attributes` | Is it the right variant? | pass — size 43 |
| **`unrequested_addon`** | Is anything **else** there too? | **concern** — a protection plan |

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `authorization.items[]` | list | Cart lines |
| `policy.intent_facets[kind=item_identity]` | object \| absent | Defines what *was* requested |
| `policy.intent_facets[kind=no_additions]` | object \| absent | Present ⇒ the rule is explicit |

Added lines are those **not** returned by the shared `matching_lines` helper — the same
matcher `item_matches_request` and `item_attributes` use, so all three checks agree on which
line is "the requested item".

## Rule

| # | Condition | Verdict | Reason code |
|---|---|---|---|
| 1 | no `item_identity` facet | `not_applicable` | — |
| 2 | identity facet exists but **nothing** matched it | `not_applicable` | — |
| 3 | every line matches identity | `pass`, **no reason code** | — |
| 4 | extra lines, `no_additions` facet present | `violation` | `unrequested_addon` |
| 5 | extra lines, no `no_additions` facet | `concern` (2.0) | `unrequested_addon` |

**Row 2** avoids piling on: `AU0017` (trail shoe), `AU0020` (helmet) and `AU0043` (gift
voucher) match nothing, so *every* line would technically be an "addition" — but that is
`item_matches_request`'s finding, already made, with a better message. Same guard as
`item_attributes`.

**Row 3** emits no reason code, so a clean multi-line order does not clutter its approval.
SCEN0001's grocery baskets routinely carry three lines, all `groceries`, all matching.

### One code, two verdicts

`unrequested_addon` keeps a single meaning — *the cart contains something not requested* — and
the **verdict** carries the strength. Splitting it into two codes would suggest two different
findings when there is one. `decision-rules.md` §6 lists it under both groups with a note, and
`CONCERN_WEIGHTS` gains `unrequested_addon: 2.0`.

## Failure mode

The check is only as good as the identity facet. A category-only rule (SCEN0001:
`[groceries, household]`) treats *any* grocery line as requested, so a second, larger grocery
basket slipped into an order would not be flagged here — the per-order and rolling limits are
what catch that. Documented rather than papered over.

## Fixtures

- **Exercised by:**
  - `AU0018` (SCEN0002) — `concern` → **`step_up`**. The only decision change.
  - `AU0041` (SCEN0004) — `violation`. Already declined on the per-order limit (CHF 459 > 400);
    this adds `unrequested_addon` to its reason codes.
  - `AU0007` (SCEN0001) — `concern`. Already declined on the rolling period limit, and a
    concern does not surface in `reason_codes` on a decline.
- **Must NOT change:**
  - `AU0017`, `AU0020`, `AU0043` — nothing matched identity; `not_applicable`, no second reason.
  - All SCEN0001 multi-line grocery orders where every line is `groceries`.
  - All of SCEN0000 and SCEN0003 — single-line or all-matching carts.

Expected regression: **exactly 1** decision changes — `AU0018` approve → `step_up`.

## Vectors

`tests/vectors/unrequested_addon.yaml` — includes both strengths, the clean multi-line cart,
and both `not_applicable` guards.

## Open questions

- [ ] Should an add-on that is *free* (unit price 0) still escalate? No fixture has one. Today
      it would, since the check counts lines rather than money.
- [ ] SCEN0002's implicit scoping produces a `step_up`, not a decline. If the PM prefers a
      decline for the demo, it is the `no_additions` branch — one line in this check.
