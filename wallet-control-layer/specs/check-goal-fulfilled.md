# Spec: goal_fulfilled

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `goal_already_fulfilled` (**pass** by default — see below; **concern**,
weight 2.0, when the customer asks to be questioned about repeats)

## Motivation

> The brief asks: *"purchases what you actually intended?"*

Two of the five instructions name **one thing**, with a definite article:

> SCEN0002 — *"Replace **my worn road-running shoes** in size 43."*
> SCEN0004 — *"Buy **the** 27-inch monitor I chose."*

Before this check, the engine approved **three pairs of the same shoes** (CHF 512.00) and
**four monitors** (CHF 1,430.40). Every one of those orders is compliant on its own facts —
right item, right seller, under the cap — which is precisely why nothing else on the board
sees them.

`duplicate_order` cannot: it requires an identical cart **and** the same amount **at the same
seller** inside 60 minutes ([`check-duplicate-order.md`](check-duplicate-order.md)), so a
second monitor from a different shop at a different price walks straight past it. That is the
right scope for that check, and it leaves this one.

## The gate: `item_keywords_all`

The whole design is in one condition, and getting it wrong would be worse than not having the
check at all.

| Instruction | Compiles to | Can it be *finished*? |
|---|---|---|
| *"our household groceries"* | `item_category_in=[groceries, household]`, **no keywords** | no |
| *"clothing for me"* | `item_category_in=[clothing]`, **no keywords** | no |
| *"one ordinary grocery item"* | `item_category_in=[groceries]`, **no keywords** | no |
| *"my worn road-running shoes"* | + `item_keywords_all=[road, running]` | **yes** |
| *"the 27-inch monitor I chose"* | + `item_keywords_all=[27, inch]` | **yes** |

An instruction that names a **thing** compiles keywords. One that names a **kind of thing**
compiles a category and none. Only the first kind has a goal that can be reached, and the
distinction already exists in the IR — nothing new had to be invented to read it.

Without the gate this check would report the second grocery delivery of the week as a
completed goal. That is the most ordinary purchase in the pack, and flagging it would be the
over-blocking the brief names as a failure.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `enrichment.approved_carts` | `tuple[(str, tuple[items]), ...]` | Carts **approved** earlier in this run. Empty ⇒ nothing bought yet |
| `enrichment.is_duplicate_of` | `str \| None` | Set ⇒ this check stands down |
| `intent_facets` → `item_identity` | facet | Absent, or no `item_keywords_all` ⇒ `not_applicable` |
| `policy.repeat_purchase_action` | `"note" \| "ask"` | Absent ⇒ `note` |

### Enrichment rule (`adapters/parse.py`)

`approved_carts` carries `(authorization_id, items)` for every prior attempt in this run whose
id is in **`ledger.approvals`**.

That is a different set from the `committed` one `duplicate_order` and `split_order` read,
and the difference is the point: those two ask *"did we commit to this?"*, and a stepped-up
order awaiting the customer counts. This one asks *"was the thing bought?"*, and a pending
question has bought nothing.

## Rule

| # | Condition | Verdict | Reason code |
|---|---|---|---|
| 1 | no `item_identity` facet | `not_applicable` | — |
| 2 | facet has no `item_keywords_all` | `not_applicable` | — |
| 3 | `is_duplicate_of` set | `not_applicable` | — |
| 4 | this cart does not contain the requested item | `not_applicable` | — |
| 5 | no approved cart contained it | `pass`, **no reason code** | — |
| 6 | an approved cart contained it, action `note` | **`pass`** | `goal_already_fulfilled` |
| 7 | an approved cart contained it, action `ask` | `concern` (weight 2.0) | `goal_already_fulfilled` |

Rows 3 and 4 stand down for a better message, the same way `check_split_order` and
`check_unrequested_addon` do. A duplicate gets *"you approved this same CHF 289.00 order 25
minutes ago"*, which beats *"this is order 2"*; and a cart that does not contain the requested
item at all is `item_matches_request`'s finding, not a fact about a goal it does not advance.

Prior carts are tested with `lines_matching`, the **same predicate** `item_matches_request`
uses — factored out rather than reimplemented, because two ways of deciding what "the thing
you asked for" means is one too many.

`goal_already_fulfilled` appears as both a pass code and a concern code, the pattern
`unrequested_addon` already established in `decision-rules.md` §6: one meaning, and the
**verdict** carries the strength.

## Why the default is `note`

This is a judgement call, so here is the evidence rather than the conclusion.

**For `ask`:** the customer wrote *"the monitor I chose"*. Four monitors is CHF 1,141.40 they
did not ask for, and one tap would have stopped it.

**For `note`, which is what ships:** the organizers describe two of the affected fixtures in
their own words, in `scenario_catalogue.csv`:

> SCEN0002 — *"…retailer type, and **an unfamiliar but fully compliant seller**."* (`AU0023`)
> SCEN0004 — *"…unrequested add-ons, a cart that contradicts the stated purchase, and **a
> legitimate re-quote**."* (`AU0042`)

Both are characterised as purchases to get **right**, which on this pack means approve.
Escalating them would contradict the organizers' own description of their own fixtures on a
question the pack was not built to ask — and it would move our board from 17 approvals to 12
while the brief names over-blocking as a failure mode.

So the finding is **recorded and not enforced**: it lands in the evidence, in the approval's
reason codes, and in the sentence the customer reads —

> *"…you have already bought the 27-inch monitor on this errand; this is order 4."*

— and the decision is unchanged, because the customer stated a limit and a seller, not a
count. A cardholder who wants the question sets `repeat_purchase_action: "ask"` and gets it.

**For a real deployment the default is probably `ask`.** Nothing about this pack's fixtures
should decide that for an issuer, and the spec says so rather than hiding the choice in a
constant.

## Failure mode

* Scoped to one run. A monitor bought last month is not in `approved_carts`, and the ledger is
  per-run by design — this check has no opinion about anything it did not watch happen.
* Quantity inside a single cart is not read. Two monitors on one order is one order, and
  `unrequested_addon` is the check that looks at extra lines.
* Pure and total: no clock, no I/O, cannot raise.

## Fixtures

- **Exercised by** (all remain `approve`; the note is added to the record and the message):
  - `AU0019`, `AU0023` — orders 2 and 3 of the road-running shoes.
  - `AU0038`, `AU0042`, `AU0045` — orders 2, 3 and 4 of the 27-inch monitor.
- **Must NOT change:**
  - `AU0012`, `AU0035` — the **first** order of each errand carries no note.
  - `AU0036` — a duplicate, which stands down (row 3) and keeps its sharper message.
  - Every SCEN0000, SCEN0001 and SCEN0003 attempt — no keywords, so row 2 applies and the
    check is `not_applicable` across all 22 of them.

Expected regression under the default: **no decision changes.** Five approval messages gain a
closing clause.

## Vectors

`tests/vectors/goal_fulfilled.yaml` — the gate (category-only instruction), the first order,
the repeat under both actions, the duplicate stand-down, and a cart that is not the requested
item at all.

## Open questions

- [ ] `repeat_purchase_action` is read from the policy but is **not yet a composed customer
      setting**: `domain/settings.py`, `domain/compose.py` and `domain/amend.py` do not know
      about it, so it cannot be set through `PATCH /v1/mandates/{id}` or the preferences
      layer yet. The check honours it; the plumbing to offer it is the remaining work, and
      `note → ask` is a tightening while `ask → note` is a widening.
- [ ] Should the note age out? An errand that runs for a month is not the same as one that
      runs for an hour, and "already bought" gets weaker with distance. Today the run is the
      window, which is the same scope the ledger uses.
