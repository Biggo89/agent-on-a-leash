# Policy IR — instruction → executable permissions

The **Policy IR** is our internal representation, produced by the compiler and reviewed by the
customer before it becomes a mandate. It is deliberately **richer than the API's `hard_rules`**,
because `hard_rules` is a small `{field, operator, value}` format that cannot express
everything the five instructions actually say.

## Why an IR at all

Take SCEN0002:

> *"Replace my worn road-running shoes in size 43. Buy only from a specialist sports retailer,
> only if the order can be returned within 14 days or more, and pay no more than CHF 200."*

`hard_rules` can express `billing_amount_chf <= 200`. It cannot express *"road-running shoes,
size 43"*, *"specialist sports retailer"*, or *"returnable within ≥ 14 days"* — those need a
cart-line predicate, a merchant-category predicate, and a fact parsed out of merchant text.

So: **`hard_rules` carries what it can** (the API stores and echoes it, and the customer sees
it), and the **IR carries the rest as `intent_facets`**, enforced by our checks. The mandate
stays honest about what the platform can represent; the engine still enforces the whole
instruction.

## Shape

```yaml
source_instruction: "<verbatim — hash-verified before submit>"
uncertainty_policy: ask            # ask | decline | approve

rules:                             # → mandate.hard_rules
  - field: billing_amount_chf
    operator: "<="
    value: 200
    currency: CHF
    scope: purchase
    confidence: high
    provenance: "pay no more than CHF 200"
  - field: billing_amount_chf       # a limit written in another currency: value is CHF,
    operator: "<="                  # converted once at the pack's fixed rate, rounded down;
    value: 190.00                   # `stated` keeps what the customer wrote (compiler rule 7)
    currency: CHF
    scope: purchase
    stated: {amount: 200.00, currency: EUR, rate: 0.95}
    confidence: high
    provenance: "Pay no more than EUR 200"
  - field: billing_amount_chf       # a price per unit times the count the instruction quotes
    operator: "<="                  # (compiler rule 8); `derived` is ours and never on the wire
    value: 600.00
    currency: CHF
    scope: purchase
    derived: {unit: night, count: 3, unit_amount: 200.00, unit_currency: CHF}
    confidence: medium
    provenance:                     # two spans: a list of {source, quote}
      - {source: instruction, quote: "CHF 200 per night"}
      - {source: instruction, quote: "3 nights"}

intent_facets:                     # → our checks; NOT expressible as hard_rules
  - kind: item_identity
    require: {item_category: sporting_goods, item_kind: road_running_shoe}
    provenance: "Replace my worn road-running shoes"
    confidence: high
  - kind: item_attribute
    require: {size: 43}
    provenance: "in size 43"
    confidence: high
  - kind: merchant_type
    require: {merchant_category_in: [sporting_goods]}
    provenance: "only from a specialist sports retailer"
    confidence: medium             # "specialist" is a judgement → open question
  - kind: order_terms
    require: {return_window_days_min: 14}   # and/or cancellable: true ("refundable rate only")
    provenance: "returned within 14 days or more"
    confidence: high
  - kind: no_additions
    provenance: "Do not add anything I did not ask for"   # SCEN0004
    confidence: high

guidance:                          # → mandate.guidance (explanatory, shown to customer)
  - "Only road-running shoes in size 43 will be bought."
open_questions:                    # → mandate.open_questions (asked at confirmation)
  - "Does 'specialist sports retailer' include a sustainable-goods shop that sells sportswear?"
```

## Compiler rules

1. **Never alter `source_instruction`.** Hash it at compile time and verify before every
   submit — the API rejects a changed instruction and the run is lost.
2. **Every rule and facet carries `provenance`**: the substring of the instruction it came
   from. This is what lets the customer check the compilation, and what makes the demo
   convincing.
3. **`confidence: low` becomes an `open_question`, not a silent rule.** The customer resolves
   ambiguity, not the compiler. This is the "clearly highlight any uncertainty" requirement.
4. **Deterministic fallback is mandatory.** With no model available the compiler must still
   produce a usable IR (amounts, currencies, periods, categories are all regex-extractable
   from these instructions) and mark the rest as open questions. The decision path must never
   depend on a model being reachable.
5. **Compile once, at mandate creation.** Never at decision time.
6. **`uncertainty_policy` is read from the instruction, and only ever tightened.** The
   default is `ask` — the middle of `approve → ask → decline` — so an instruction that says
   nothing about uncertainty still gets the behaviour all five pack scenarios ask for. A
   customer who states the strict policy outright (*"If you are not sure, decline"*) gets
   `decline`. **A customer who states the loose one does not get `approve`**: a cue list can
   always be widened and never made complete (`llm-compiler.md`), and a false positive on
   the loose side spends money on a guess, where a false negative on the strict side only
   asks a question. So the deterministic compiler moves this field toward `decline` and
   never away from it — the same asymmetry as rule 3 and the spend safety floor. A model may
   propose `approve`; the guard in `llm-compiler.md` decides what happens to that, and a
   human confirms it either way.

7. **Every enforced amount is CHF.** Checks compare `billing_amount_chf`, so a limit written in
   another currency is converted at compile time at the pack's fixed rate, rounded down to the
   centime, and keeps what was written under `stated`. The platform bills at that same fixed
   rate, so the converted cap is exact. A currency with no rate is an open question, never a
   guessed rule. `llm-compiler.md` §"Foreign-currency caps".
8. **A price per unit is not an order cap.** *"CHF 200 per night"* with *"3 nights"* quoted is
   a per-order cap of CHF 600. The rule quotes both spans, and `derived` records the
   arithmetic for the review screen. Without a quoted count it stays per order and becomes an
   open question. `llm-compiler.md` §"Derived caps".

**Who may write into the IR, and what is done to what a model writes, is
[`llm-compiler.md`](llm-compiler.md).** That file owns the guard rails, the low-confidence
asymmetry, the spend safety floor and the instruction round-trip; this file owns the shape.

## Mandate lifecycle

`draft` → `POST /v1/mandates` → `TM…` → `POST /v1/mandates/{draft_id}/confirm {"confirmed": true}`
→ active → `PATCH` (**tighten-only**: `hard_rules` may add, never remove; `uncertainty_policy`
may only move toward `decline`) → `DELETE` (revoke).

A run snapshots the mandate at start; later patches apply to later runs. **Revocation is a
demo beat** — it is the clearest possible answer to "how does the customer retain control?".
