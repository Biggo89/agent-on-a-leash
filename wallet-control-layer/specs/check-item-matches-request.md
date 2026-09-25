# Spec: item_matches_request

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `item_matches_request`, `item_not_requested`, `cart_contradicts_purpose`,
`insufficient_evidence`

## Motivation

Every instruction names *what* to buy, and three scenarios ship a cart that quietly is not it:

> SCEN0002 — "**Replace my worn road-running shoes** in size 43." → a **trail**-running shoe
> (`AU0017`), a **cycling helmet** (`AU0020`)
> SCEN0004 — "**Buy the 27-inch monitor I chose**…" → a **digital gift voucher** (`AU0043`)

SCEN0004's brief calls this *"a cart that contradicts the stated purchase"*. `AU0020` and
`AU0043` are both under their limits, from permitted sellers, with acceptable return terms —
every other check passes them. Only item identity catches them.

## Scope boundary — what this check does NOT do

This is the first of three item checks and the boundaries matter, or they double-fire:

| Check | Question | Example |
|---|---|---|
| **`item_matches_request`** | Is what the customer asked for **present at all**? | `AU0020` — helmet instead of shoes |
| `item_attributes` | Is the matching item the **right variant**? | `AU0013` — size 42 instead of 43 |
| `unrequested_addon` | Is there **anything else** in the cart too? | `AU0018` — shoes **plus** a protection plan |

So this check passes as soon as **one** line matches. `AU0018` (road-running shoes + an
extended protection plan) matches on the shoe line and is this check's business no further;
the add-on belongs to `unrequested_addon`. Likewise `AU0007` (produce + a cosmetics gift)
contains genuine groceries, so it passes here.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `authorization.items[].item_name` | `str` | Matches the catalogue row; the trustworthy half of a cart line |
| `authorization.items[].item_category` | `str` | Shared vocabulary; `gift_card`, `cosmetics`, `membership` have no merchant counterpart |
| `policy.intent_facets[kind=item_identity]` | object \| absent | Absent ⇒ no identity requirement |

`facet.require`: `item_category_in` (list), `item_keywords_all` (list, optional),
`item_description` (str, for the customer message).

### Matching against `item_name`, not `item_details`

Keywords are matched against `item_name` only. The data dictionary guarantees `item_name`
matches the referenced catalogue row, whereas `item_details` is free merchant text and is
where SCEN0004's injections live. Matching identity on the free-text field would let a
merchant relabel a trail shoe as a road shoe in its own description.

## Rule

A cart line **matches** iff:

```
(item_category ∈ item_category_in)   AND   (every keyword appears in normalised item_name)
```

| # | Condition | Verdict | Reason code |
|---|---|---|---|
| 1 | no `item_identity` facet | `not_applicable` | — |
| 2 | facet present, no category **and** no keywords | `unknown` | `insufficient_evidence` |
| 3 | at least one line matches | `pass` | `item_matches_request` |
| 4 | no line matches **and** no line is even in the required category | `violation` | `cart_contradicts_purpose` |
| 5 | no line matches, but some line is in the required category | `violation` | `item_not_requested` |

Rows 4 and 5 are both declines but say different things, and the difference is the whole
point of the check:

- `AU0043` — a `gift_card` where `electronics` was asked for. Nothing in the cart is even the
  right kind of thing → *"this order is for a digital gift voucher, not the 27-inch monitor
  you asked for."*
- `AU0017` — `sporting_goods`, right category, wrong product → *"this order is for
  trail-running shoes, not the road-running shoes you asked for."*

## Compiler

1. Extract the object of the buy verb:
   `(?:buy|order|purchase|replace|get) <descriptor>`, stopping at `for`/`from`/`up to`/
   `in size`/`only`/`and`/`when`/`,`. Verified to yield, across the five instructions:
   `one ordinary grocery item`, `our household groceries`, `my worn road-running shoes`,
   `clothing`, `the 27-inch monitor I chose`.
2. Strip determiners and non-product adjectives (`my`, `our`, `the`, `one`, `worn`,
   `ordinary`, `I`, `chose`…).
3. Map remaining tokens through an **item** category vocabulary — separate from the merchant
   one, because `gift_card`, `cosmetics` and `membership` are item categories with no merchant
   counterpart.
4. **Keywords come only from hyphenated or numeric-qualified compounds** — `road-running`,
   `27-inch`. A compound like that is a product specification; a loose adjective like
   `household` is not, and treating it as one would decline every SCEN0001 grocery basket
   because no item is literally named "household".

   Verified: compounds appear in exactly SCEN0002 and SCEN0004 — the two scenarios where the
   category alone cannot discriminate (`Road-running shoes`, `Trail-running shoes` and
   `Cycling helmet` are all `sporting_goods`).
5. No recognisable category and no compound ⇒ an `open_question`, **no facet**. The compiler
   does not guess what the customer meant to buy.

## Failure mode

A customer writing "buy running shoes" (no hyphen) gets a category-only rule, and a trail shoe
would pass. The deterministic compiler cannot resolve that; the LLM compiler (Phase 4) should,
and until then the gap is visible as an approval rather than hidden. Documented in Open
questions.

## Fixtures

**Exercised by:**

| Fixture | Cart | Expected |
|---|---|---|
| `AU0017` | Trail-running shoes (`sporting_goods`) | `violation` — `item_not_requested` |
| `AU0020` | Cycling helmet (`sporting_goods`) | `violation` — `item_not_requested` |
| `AU0043` | Digital gift voucher (`gift_card`) | `violation` — `cart_contradicts_purpose` |

**Must NOT change:**

- `AU0018` — road-running shoes **+** protection plan. The shoe line matches, so this check
  passes; the add-on is `unrequested_addon`'s.
- `AU0041` — monitor **+** protection plan. Same shape.
- `AU0007` — produce **+** a cosmetics gift. Contains real groceries, so it passes here. (It
  is already declined on the rolling period limit; this check adds no reason code to it.)
- `AU0013` — a size-42 road-running shoe. Identity matches; the size is `item_attributes`.
- All of SCEN0003 — every line is `clothing`, category-only rule, all match.

Expected regression: **exactly 3** decisions change — `AU0017`, `AU0020`, `AU0043`
approve → decline.

## Vectors

`tests/vectors/item_matches_request.yaml` — includes the two distinct violation rows, the
multi-line "one match is enough" case, and the no-facet case.

## Open questions

- [ ] Keywords require a hyphen or a numeric qualifier. "running shoes" written plainly yields
      a category-only rule. Raise with the LLM compiler work rather than loosening the
      deterministic rule, which would start guessing.
- [ ] Should `cart_contradicts_purpose` also fire when the cart contains the right item **plus**
      something from a wholly unrelated category? Today no — that is `unrequested_addon`.
