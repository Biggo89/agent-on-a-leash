# Live instructions the compiler did not read — mostly `IMPLEMENTED`

**Status:** 2026-09-24. §A (A2 and refundable), §B, §C2 and §D are implemented, on Nakya's
answers to the decisions each section lists. A1 (a nightly cap per line) and C1 (one delivery
a day) are deferred. The proposals below are kept as written, and each implemented section
opens with what was decided and what building it corrected.

Since 2026-09-24 the organizers' API serves ten team-specific scenarios (`adapters/livepack.py`).
Four of the five repo themes carry new instructions and five themes are new. Both compilers
were measured against all ten that day, the baseline and the model (`gemini-3.7-flash` behind
the guard). Four gaps survive the model:

| # | Gap | Scenario | Effect on an order | Status |
|---|---|---|---|---|
| 1 | a cap in EUR enforced as CHF | SCEN0104 | **approves** above the stated limit | fixed — PR #13, `llm-compiler.md` rail 12 |
| 2 | a price per night | SCEN0124 | **declines** every real booking | fixed — §A (A2 and refundable; A1 deferred) |
| 3 | an excluded product that is not a category | SCEN0117 | **approves** alcohol | fixed — §B |
| 4 | how often, and on which days | SCEN0113 | **approves** a second delivery, and weekends | weekends fixed — §C (C2); one-a-day (C1) deferred |
| 5 | categories the live pack added | SCEN0122, SCEN0124 | **declined** the camera lens | fixed — §D |

Over-blocking is a failure mode, not a safe default (`CLAUDE.md`). Gaps 2 and 5 decline the
very purchase the customer asked for, so they matter as much as the approvals. §D declined the
legitimate lens in SCEN0122, the "Manipulated agent" scenario, as well as the attack; it is
fixed.

---

## A. A price per night — SCEN0124 — A2 and "refundable" `IMPLEMENTED`

**Implemented 2026-09-24** on Nakya's answers: A2 now, A1 deferred; the floor may hold a derived
cap; `order_cancellable` is the reading of "refundable". Normative text:
`llm-compiler.md` §"Derived caps" and `check-order-terms.md` §"Cancellation".

**Verified live 2026-09-25** (run `run_55e73eff6014a641`, twelve orders):
- Three-night stays under CHF 200 a night were approved: five bookings, among them 3 × 180 at
  IsarNest.
- The non-refundable room was declined `order_not_cancellable`. So were 4 nights, and 3 × 263
  (over the CHF 600 stay cap).
- The flight and the insurance add-on were declined `item_keyword_excluded`.
- One room whose seller left `order_cancellable` unknown asked the customer.

Two corrections to the proposal below, found while building it:

- **The guard was never the obstacle.** No rail checks a CHF value against the text, so the
  model's CHF 600 was always accepted. The **safety floor** removed it, because the baseline
  read "CHF 200" as a per-order cap. The fix is in the baseline, and no rail admits anything
  new. Decision 2 was really about the floor's guarantee, which now reads "…or, written per
  unit, the amount times the count written beside it".
- **A1 cannot be a new `scope`.** The platform's `mandate_rule` schema allows only
  `purchase`, `period` or null there, with `additionalProperties: false`. A unit cap needs a
  new `field` with `scope: purchase`, or a facet, and the safety floor must first compare
  like with like, because it matches rules by value whatever the field.

> *"Book me a hotel in Munich for 3 nights from 10 September to 13 September, at most CHF 200
> per night, refundable rate only. No flights, no insurance. Ask me when uncertain."*

**Today.** The baseline reads *"CHF 200"* as a per-order cap. The model proposed CHF 600 (3 ×
200), and the guard removed it as `unsupported_cap_removed: CHF 600.00 was not in the
instruction`. The safety floor then left **CHF 200 per order**. A three-night stay at CHF 190 a
night bills CHF 570, and it is declined. Every booking the customer asked for is declined.

**Proposal.** Two readings are possible. Which one is right depends on how the platform bills
a stay, and no live SCEN0124 order has been seen yet. *(Seen 2026-09-25: a stay arrives as quantity
= nights at the nightly rate, e.g. "Hotel room, refundable rate" × 3 at 180.00. A1 is therefore
implementable as proposed, as a new `field`, and would also close A2's one looser case.)*

- **A1 — a cap per unit.** A new rule scope, `unit`: each item line's `unit_price` in CHF must
  be at most the cap. It is faithful to the words and independent of how many nights. It is
  only right if a stay arrives as quantity 3 × the nightly rate. A single line *"3 nights,
  CHF 570"* would be declined.
- **A2 — a derived total.** The guard accepts a cap the model *derived*, when every operand is
  quoted: *"3 nights"* and *"CHF 200 per night"* give CHF 600 per order. Provenance is both
  spans, plus a `derived: "3 × CHF 200"` field so the review screen shows the arithmetic. It
  holds whatever the line shape. It is looser in one case: one night at CHF 590 passes.

Recommended: **A2 now**, since it fixes the decline whatever the line shape, and **A1 as well**
once one SCEN0124 order shows per-night lines. Rail 9 (the safety floor) is untouched. It still
guarantees no cap looser than a written amount, and it has to learn that a quoted multiplier
turns *"CHF 200 per night"* into a 600 floor rather than a 200 one. Without that, it re-adopts
the 200 and undoes A2.

**Also in this sentence:**

- *"refundable rate only"*. Hotel rates are told apart by name only: *"Hotel room, refundable
  rate"* (IT0171) versus *"Hotel room, non-refundable rate"* (IT0172). A keyword
  `refundable` matches **both**, because the tokenizer splits *non-refundable*. The trusted
  field is the platform's `order_cancellable`, which is parsed and never read. Proposal: an
  `order_terms` key `cancellable: true`, checked against `order_cancellable`. It works the way
  `return_window_days_min` reads the trusted `order_returnable`.
- *"No flights, no insurance"* is a category exclusion of `travel`, which needs §D first.
- *"from 10 September to 13 September"* — the stay dates. No facet reads dates. Out of scope
  here and raised as an open question on the mandate.

**Decisions for Nakya**

1. A2 alone, or A2 now and A1 after the first live order? Recommended: both, in that order.
2. May the guard accept a derived number when every operand is quoted? This is the first rail
   that admits a value not written verbatim, so it needs your yes.
3. `order_cancellable` as the reading of "refundable": yes or no?

**Tests.** A three-night stay at CHF 190 approves, and at CHF 210 a night it declines, under
A2 and under A1 separately. `non-refundable` never satisfies "refundable rate only". The safety
floor keeps 600 and does not re-adopt 200.

---

## B. An excluded product that is not a category — SCEN0117 — `IMPLEMENTED`

**Implemented 2026-09-24** on Nakya's answers: yes to keyword exclusion as a new key; yes to
a baseline lexicon. **Verified live 2026-09-25** (`run_f46f346588308168`): a basket of fresh
produce plus "Wine and spirits" was declined `item_keyword_excluded`. "Drinks and snacks" was
approved three times. Gift cards and a personal care set were declined `item_category_excluded`. Normative text: `check-category-exclusion.md` §"Product words", and
`llm-compiler.md` rail 5c. Found while building it:

- **The model could never write an exclusion.** Its output schema listed none of
  `item_category_not_in`, `merchant_category_not_in` or `item_keywords_none` under
  `additionalProperties: false`. So a `category_exclusion` arrived with an empty `require`
  and was dropped. It now carries all three.
- **"The allow-list already rules out gift cards" holds only for single-kind baskets.**
  `item_matches_request` passes when any line matches, so groceries plus a gift card only
  stepped up, as an unrequested add-on. The explicit exclusion now declines it.
- **The baseline lost exclusions.** It stopped at the first "no X" and did not know the
  phrase "gift cards". It now keeps every exclusion in one facet, quoting each phrase.
- **A multi-word entry is a phrase.** A model's "wine and spirits", cut into single words,
  would have excluded "and", and so "Drinks and snacks" and "Bakery and dairy order".

> *"Groceries and everyday household items only, maximum CHF 100 per order, from the shops I
> use. No alcohol, no gift cards, no cosmetics. When in doubt, ask."*

**Today.** The model returns `item_identity` with `item_category_in: [groceries, household]`.
That allow-list already rules out gift cards (`gift_card`) and cosmetics, because they are
other categories. The baseline adds `category_exclusion: cosmetics`. **Nobody excludes
alcohol**, and nothing in the vocabulary could: *"Wine and spirits"* (IT0168) is filed under
`groceries`. A wine order passes as groceries, against *"No alcohol"*.

The model does not use `category_exclusion` at all. The output schema offers the kind, but the
system prompt never explains it.

**Proposal.**

- A new `category_exclusion` key, `item_keywords_none`: product words the cart must not
  contain, read with the same tokenizer as `item_keywords_all`. *"No alcohol"* becomes
  `["wine", "spirits", "beer"]`.
- The customer's word ("alcohol") is not a product word, so the translation is an
  interpretation. Guard: every excluded keyword must match at least one catalogue product
  (the rail 5b machinery, inverted), or it is inert and dropped with a question. The facet is
  `confidence: medium`, so the review screen asks: *"No alcohol — this excludes 'Wine and
  spirits'. Right?"*
- Baseline: a small, written-down lexicon for the concepts customers name, starting with
  `alcohol → wine, spirits, beer`, so the deterministic floor excludes it too.
- Prompt: explain `category_exclusion`, and ask the model to state an explicit exclusion even
  when an allow-list already implies it. It costs nothing to enforce, and it puts the
  customer's own words on the review screen.

**Decisions for Nakya**

1. Keyword exclusion as a new require key: yes or no?
2. A baseline lexicon (auditable, small, grows by review), or leave concept words to the model
   alone? Recommended: the lexicon, so "no alcohol" never depends on a model being reachable.

**Tests.** A cart with *"Wine and spirits"* is declined under SCEN0117, citing "No alcohol". A
grocery cart without it is unaffected. A keyword matching no product is dropped with a
question, not enforced.

---

## C. How often, and on which days — SCEN0113 — C2 `IMPLEMENTED`, C1 deferred

**Implemented 2026-09-24** on Nakya's answers: the weekend now, in Swiss time; the count cap
later. **Verified live 2026-09-25** (`run_354b397611f9f67c`): the Saturday delivery was declined
`outside_spending_days`. Monday 23:15 UTC, a Tuesday in Zurich, was approved. Normative text: `check-spending-hours.md` §"Days". Decisions as taken:

1. "A day", for C1 when it comes, is a calendar day in local time.
2. Local time applies to **days** (`Europe/Zurich`, computed in `domain/localtime.py`). Hours
   keep their UTC reading for now, so no saved preference changes meaning.
3. `order_count` is deferred with C1. On the wire it is a legal rule (the `field` is free).
   But the safety floor matches rules by value whatever the field, so a count rule of 1 would
   "cover" any money cap. The floor has to compare like with like first.

Found while building it: the model's output schema carried no `hours_from`, `hours_to` or
`weekdays`, so the model could never write this facet at all. It now can. *"Dinners"* stays a
question: the baseline asks it, and the model may only propose hours at low confidence.

> *"Weeknight dinners only: one delivery a day, CHF 40 maximum including the delivery fee,
> from my usual services. Never at the weekend. Ask me if something doesn't fit."*

**Today.** The model reads the CHF 40 cap, "usual services" and `food_delivery`. It reads
nothing about *"one delivery a day"*, *"weeknight"* or *"never at the weekend"*. A second
delivery on the same day, and a Saturday order, both pass.

**Proposal.**

- **C1 — a count cap.** A rule on the number of approved orders, `field: order_count`,
  `operator: <=`, `value: 1`, `scope: period`, `period_days: 1`, enforced from the run's
  ledger the way the spend windows are.
- **C2 — days of the week.** `spending_hours` gains `weekdays: [mon, tue, wed, thu, fri]`,
  alongside `hours_from`/`hours_to`. It is one facet because "when may the agent buy" is one
  question, and `compose` already intersects windows.
- *"dinners"* implies hours, but the words do not state them. The model may propose an evening
  window only at `confidence: low`, which by rail 8 becomes a question, never a rule: *"What
  time counts as dinner?"*

**Timezone.** `spending_hours` reads the purchase clock **in UTC** today (the setting's help
text says so). *"Never at the weekend"* from a customer in Winterthur means Swiss local time. A
Friday 23:30 UTC order is Saturday 01:30 in Zurich. So a day or hour the customer wrote should
be read in `Europe/Zurich`, and the zone recorded on the facet.

**Decisions for Nakya**

1. "A day": a calendar day in local time, or a rolling 24 hours? Recommended: the calendar day,
   since that is what "one a day" means to a person.
2. Local time (`Europe/Zurich`) for customer-written days and hours: yes or no? This changes an
   existing setting's semantics, so standing hours preferences saved in UTC would need
   converting.
3. `order_count` as a new rule field: yes or no? It is the first rule that is not money.

**Tests.** The second approved delivery on one day is declined, and the next day's first is
approved. A Saturday 10:00 Zurich order is declined. A Friday 23:30 UTC order is a Saturday in
Zurich, and is declined. Dinner hours stay a question, never a rule.

---

## D. Categories the live pack added — `photography`, `travel` — `FIXED`

**Fixed 2026-09-24**, as proposed below. `DataPack.load` registers the pack's categories
(`domain/settings.py` `register_categories`, add-only), and the guard, the model's output schema,
`/v1/config` and the preferences validator read the vocabulary in force. The baseline maps
`camera`/`lens` and `flight`/`insurance` onto `photography` and `travel` only while the loaded
pack has them. Verified against the model with the live pack: SCEN0122 compiles to
`item_category_in: [photography]`, and the real lens passes item identity.

**Found while writing this spec.** The engine's category vocabularies are constants copied from
the repo pack (`domain/settings.py` `ITEM_CATEGORIES`). The live pack adds two item categories:

| Category | Items | Needed by |
|---|---|---|
| `photography` | IT0123 *Camera lens* | SCEN0122 *"Buy the camera lens I chose"* |
| `travel` | IT0132 *Economy flight ticket*, IT0136 *Travel insurance add-on* | SCEN0124 *"No flights, no insurance"* |

The model cannot propose a category that is not in the output schema, and rail 5 drops one it
does not know. So the model compiled SCEN0122 as `item_identity: electronics, keywords camera,
lens`, and **the real lens, filed under `photography`, fails its own identity check.** The
legitimate purchase is declined along with the attack the scenario tests. SCEN0124 cannot
express its exclusion at all.

**Proposal.** The vocabularies are read from the loaded pack (`items.csv`, `merchants.csv`), so
the live pack's categories exist exactly when the live pack is loaded. The constants stay as
the offline default. `tests/unit/test_compile_contract.py` already asserts that the repo pack
equals them. The same list feeds the model's output schema, rail 5, `compose`, and
`GET /v1/config` (the UI's options), which is the single-table rule `domain/settings.py`
already follows.

**Decisions for Nakya.** None of substance. It is a bug with one fix, and it is recommended as
the **next** change, before §A to §C, because it declines a flagship scenario.

**Tests.** With the live pack loaded, `photography` and `travel` are accepted by rail 5 and
offered in `/v1/config`, and the SCEN0122 lens passes item identity. With the repo pack, the
vocabulary is unchanged.
