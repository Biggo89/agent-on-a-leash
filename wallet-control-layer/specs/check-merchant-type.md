# Spec: merchant_type

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `merchant_meets_requirement`, `merchant_type_not_permitted`, `merchant_type_unknown`

## Motivation

One instruction restricts *what kind of shop* the agent may buy from:

> SCEN0002 — "Buy **only from a specialist sports retailer**, only if the order can be
> returned within 14 days or more, and pay no more than CHF 200."

This is a different requirement from [`merchant_permitted`](check-merchant-permitted.md),
which is about *prior relationship*. The pair is the sharpest over-blocking trap in the pack:

| Fixture | Merchant | Familiar? | Right type? | Correct outcome |
|---|---|---|---|---|
| `AU0023` | Summit Thread (`sporting_goods`) | **no** — 0 prior | **yes** | **approve** — the instruction asks for a specialist retailer, not a familiar one |
| `AU0022` | GreenLoop (`sustainable_goods`) | no — 0 prior | **no** | decline — wrong kind of shop |

Conflating the two requirements gets *both* of these wrong.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `authorization.merchant.merchant_category` | `str` | Schema guarantees non-empty; empty ⇒ treat as unknown |
| `authorization.merchant.merchant_mcc` | `str` (4 digits) | Carried as evidence |
| `policy.intent_facets[kind=merchant_type]` | object \| absent | Absent ⇒ the instruction states no type requirement |

`facet.require.merchant_category_in` is a list of allowed categories from the pack's shared
vocabulary (`groceries`, `sporting_goods`, `electronics`, `clothing`, …).

### Why category and not MCC

MCC is the payment-network standard and is what a production control layer would key on. In
this pack it is **coarser than the category vocabulary** and would produce false passes:

| MCC | Categories sharing it |
|---|---|
| `5399` | `household` **and** `sustainable_goods` |
| `5812` | `dining` **and** `food_delivery` |

`sustainable_goods` (GreenLoop, `AU0022`) shares MCC `5399` with ordinary household
merchants, so an MCC-only rule could not express "specialist sports retailer" against it in
general. `merchant_category` is therefore the matching key here, and `merchant_mcc` is carried
as **evidence** — a card-issuing judge reads `5941` and knows it means Sporting Goods Stores
without translating our vocabulary.

To move to MCC matching in production, add `merchant_mcc_in` to the facet and give the
compiler an MCC vocabulary; the check's shape does not change.

## Rule

`allowed` = `facet.require.merchant_category_in`.

| Condition | Verdict | Reason code | Evidence |
|---|---|---|---|
| no `merchant_type` facet | `not_applicable` | — | — |
| facet present but `allowed` empty/missing | `unknown` | `merchant_type_unknown` | category, mcc |
| `merchant_category` empty | `unknown` | `merchant_type_unknown` | merchant_id, mcc |
| `merchant_category` ∈ `allowed` | `pass` | `merchant_meets_requirement` | category, mcc |
| `merchant_category` ∉ `allowed` | `violation` | `merchant_type_not_permitted` | category, mcc, allowed list |

**Why `violation` and not `unknown`, given "specialist" is a judgement.** The *interpretation*
("specialist sports retailer" ⇒ `sporting_goods`) is uncertain, and that uncertainty is
surfaced at authoring time as an `open_question` the customer answers before the mandate goes
active. Once confirmed, the runtime comparison is mechanical: a merchant's category either is
or is not in the allowed list. Deferring the interpretation to every transaction would turn a
resolved question into a permanent step-up — over-blocking, and it would waste the customer's
confirmation.

## Compiler

`compile/baseline.py` extracts the requirement deterministically:

1. Match `from a|an|the <phrase> <retailer|shop|store|seller|merchant|dealer>`.
   Verified to fire on SCEN0002 only; the "shops I have used before" and "a seller I have
   bought from before" phrasings do not match, so familiarity and type never collide.
2. Scan the captured phrase against a natural-language → category vocabulary
   (`sports`/`sporting`/`athletic` ⇒ `sporting_goods`, and so on). Generic vocabulary, **not**
   a scenario lookup.
3. Emit the facet with `confidence: medium` plus an `open_question` naming the judgement:
   *"Does 'specialist sports retailer' include a shop in another category that also sells
   sports equipment?"*
4. **A phrase with no recognised category emits the open question and no facet.** Guessing a
   category the customer did not state would silently invent a rule; asking is the honest
   fallback, and `merchant_type` then correctly returns `not_applicable`.

## Failure mode

An unrecognised phrase produces no facet, so the check stands down and the requirement goes
unenforced — visibly, via the open question on the mandate. That is preferable to inventing a
category, but it means **the open questions must actually reach the customer** in the
authoring UI. Flagged for whoever owns that surface.

## Fixtures

- **Exercised by:** `AU0022` (GreenLoop, `sustainable_goods` → `violation`), and the ten
  SCEN0002 attempts at TrailSpark plus `AU0023` at Summit Thread (`sporting_goods` → `pass`).
- **Must NOT change:**
  - `AU0023` — unfamiliar but the right kind of shop. Must stay `approve`.
  - All of SCEN0000, SCEN0001, SCEN0003, SCEN0004 — no `merchant_type` facet, so
    `not_applicable` throughout.
  - `AU0021` (CHF 215) stays declined on the per-order limit, not on type.

Expected regression: **exactly one** decision changes — `AU0022` approve → decline.

## Vectors

`tests/vectors/merchant_type.yaml` — includes the no-facet case, the empty-allowed-list case,
the multi-category case, and the unknown-category case.

## Open questions

- [ ] The vocabulary is a fixed keyword map. A cardholder writing "from a proper running shop"
      gets an open question rather than a rule. The LLM compiler (Phase 4) should map these;
      the deterministic fallback deliberately does not guess.
- [ ] Should a `confidence: medium` facet whose `open_question` the customer never answered
      downgrade `violation` to `unknown`? Today it does not — confirmation is treated as
      answering it. Revisit if the authoring UI lets a customer confirm while skipping questions.
