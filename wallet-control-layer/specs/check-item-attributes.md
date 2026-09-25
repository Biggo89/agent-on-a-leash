# Spec: item_attributes

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `item_attributes_match` *(new)*, `item_attribute_mismatch`,
`item_attributes_unknown`

## Motivation

> SCEN0002 — "Replace my worn road-running shoes **in size 43**."

`AU0013` is a road-running shoe from the right retailer, CHF 155 (under the 200 cap), with a
30-day return window — and it is **size 42**. Every other check passes it. The scenario
description calls this out as *"item attributes"*, alongside substitution and unrequested
additions.

Verified across the pack: `AU0013` is the **only** attribute mismatch, and SCEN0002 is the
**only** instruction that states an attribute at all.

## Scope boundary

Third of the three item checks (see [`item_matches_request`](check-item-matches-request.md)):

| Check | Question | `AU0013` |
|---|---|---|
| `item_matches_request` | Is what was asked for present? | **passes** — it *is* a road-running shoe |
| **`item_attributes`** | Is it the **right variant**? | **violation** — size 42, not 43 |
| `unrequested_addon` | Is anything else present too? | n/a |

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `enrichment.item_facts[].size` | `int \| None` | `None` ⇒ this line states no size. Index-aligned with `authorization.items` |
| `policy.intent_facets[kind=item_attribute]` | object \| absent | Absent ⇒ no attribute requirement |
| `policy.intent_facets[kind=item_identity]` | object \| absent | Used to scope *which* lines are checked |

`facet.require.size` is the requested size.

### Only identity-matching lines are checked

`AU0018` is a size-43 shoe **plus** an "Extended protection plan" that states no size. A
protection plan has no size and never will; comparing it against "size 43" would produce a
spurious `item_attributes_unknown` on an order that is fine.

So the candidate lines are **the lines `item_matches_request` matched**, via a shared pure
helper (`domain/checks/item.matching_lines`) so the two checks cannot drift apart. When no
identity facet exists, every line is a candidate. When an identity facet exists but nothing
matched it, this check returns `not_applicable` — `item_matches_request` has already declined,
and a second violation about the size of a thing the customer never asked for is noise.

### Size comes from untrusted text

Same standing as the return window (`decision-rules.md` §3): the size exists nowhere but
`item_details`, so extracting it is legitimate typed fact extraction. `extract_item_facts`
returns `int | None` — an extractor that can only emit an integer cannot be argued into
emitting a verdict.

## Rule

`required` = `facet.require.size`. `candidates` = identity-matching lines (see above).
`stated` = the sizes those candidates declare, ignoring `None`.

| # | Condition | Verdict | Reason code |
|---|---|---|---|
| 1 | no `item_attribute` facet | `not_applicable` | — |
| 2 | facet present, no known attribute in `require` | `unknown` | `item_attributes_unknown` |
| 3 | an identity facet exists but no line matched it | `not_applicable` | — |
| 4 | no candidate states a size | `unknown` | `item_attributes_unknown` |
| 5 | any stated size ≠ `required` | `violation` | `item_attribute_mismatch` |
| 6 | otherwise | `pass` | `item_attributes_match` |

**Row 5 is deliberately strict** — *any* mismatching line fails, rather than "at least one line
matches". A cart containing both a size-43 and a size-42 shoe is not a fulfilled request; it is
an order that will need a return.

**Row 4** is uncertainty, not a pass: an unstated size cannot be confirmed as correct. It routes
to the mandate's `uncertainty_policy` (`ask` ⇒ `step_up`).

### New reason code

`item_attributes_match` is added to the approvals vocabulary in `decision-rules.md` §6.
Reusing `item_matches_request` would make the two item checks indistinguishable in an audit
record, which is the opposite of what reason codes are for.

## Failure mode

`extract_item_facts` matches **numeric** sizes only (`size\s+(\d{1,3})`). The pack also
contains letter sizes — `size M` on `AU0020`'s helmet and six SCEN0003 clothing lines, `size S`
on six more — but **no instruction ever requests one**, so they are never compared. Both sides
ignore letter sizes consistently, so nothing is silently mis-decided today. See Open questions.

## Fixtures

- **Exercised by:** `AU0013` — size 42 against a required 43 → `violation`.
- **Must NOT change:**
  - `AU0012`, `AU0014`–`AU0016`, `AU0019`, `AU0021`–`AU0023` — size 43, `pass`.
  - `AU0018` — size-43 shoe + a plan stating no size. The plan is not an identity match, so it
    is not a candidate; the check passes on the shoe alone.
  - `AU0017` (trail shoe) and `AU0020` (helmet) — already declined by `item_matches_request`;
    nothing matched identity, so this check is `not_applicable` and adds no second reason.
  - All of SCEN0000, SCEN0001, SCEN0003, SCEN0004 — no `item_attribute` facet.

Expected regression: **exactly 1** decision changes — `AU0013` approve → decline.

## Vectors

`tests/vectors/item_attributes.yaml` — includes the mismatch, the unstated-size uncertainty,
the identity-scoping case, and the strict multi-line rule.

## Open questions

- [ ] Letter sizes (`S`/`M`/`L`) are not extracted, so "buy it in size M" would compile to no
      attribute requirement and go unenforced. Making `size` a string would ripple through
      `ItemFacts`; worth doing only if a fixture or a judge's question demands it.
- [ ] `27-inch` in SCEN0004 is handled as an **identity keyword**, not an attribute. Both
      readings are defensible; it works and is tested where it is. Revisit only if the LLM
      compiler wants one consistent home for measurements.
