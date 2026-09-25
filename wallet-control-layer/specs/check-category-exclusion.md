# Spec: category_exclusion

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `categories_permitted`, `item_category_excluded`, `merchant_category_excluded`,
`item_keyword_excluded`

## Motivation

The pack's own customer profiles state things the five instructions never do:

> CU0001, Alex Meier — *"Practical groceries, children's essentials, and durable household
> products; **avoids gift vouchers**."*

This is a **standing** preference, not an errand: it is true of the person on every purchase,
and it is the shape `customer-settings.md` §2 exists to carry. Nothing in the six facets could
express it before. `merchant_type` is an **allow-list**, and the complement of "no gift cards"
is the other twenty-one categories — which is both absurd to write and wrong the moment the
vocabulary grows a category the customer never considered.

So a deny-list is not a convenience over an allow-list. It is the only shape under which
*"everything except this"* stays correct as the world changes.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `authorization.items[].item_category` | `str` | Empty or no items ⇒ the item side cannot be established |
| `authorization.merchant.merchant_category` | `str` | Empty ⇒ the merchant side cannot be established |
| `policy.intent_facets[kind=category_exclusion]` | object \| absent | Absent ⇒ the customer excluded nothing |

`facet.require` carries either or both of:

- `item_category_not_in` — categories from `items.csv`'s vocabulary the cart may not contain
- `merchant_category_not_in` — categories from `merchants.csv`'s vocabulary the agent may not buy from

Enrichment required: none. Both fields are on the event and both are **trusted** — they come
from the platform's own records, not from merchant free text (`decision-rules.md` §3).

## Rule

Two independent dimensions, so **one `CheckResult` per dimension the facet states**. A cart
that is both from an excluded shop *and* contains an excluded item is two findings, and
reporting them as one would hide half of it. Same shape as `period_limit` over several
windows (§9).

Results are emitted **merchant first, then item**, so the list is deterministic whatever order
the `require` keys arrived in (`decision-rules.md` §7).

| Condition | Verdict | Reason code | Evidence |
|---|---|---|---|
| no `category_exclusion` facet | `not_applicable` | — | — |
| facet present, both lists empty or missing | `unknown` | `insufficient_evidence` | — |
| `merchant_category_not_in` stated, `merchant_category` empty | `unknown` | `insufficient_evidence` | merchant_id |
| `merchant_category` ∈ `merchant_category_not_in` | `violation` | `merchant_category_excluded` | category, excluded list |
| `merchant_category` ∉ `merchant_category_not_in` | `pass` | `categories_permitted` | category, excluded list |
| `item_category_not_in` stated, cart has no categorised line | `unknown` | `insufficient_evidence` | — |
| any line's `item_category` ∈ `item_category_not_in` | `violation` | `item_category_excluded` | the offending lines, excluded list |
| no line's `item_category` ∈ `item_category_not_in` | `pass` | `categories_permitted` | categories seen, excluded list |

**Boundary — one excluded line among many is a violation.** The customer said they do not buy
gift vouchers; a basket that is nine-tenths groceries and one-tenth gift voucher still
contains a gift voucher. This mirrors `unrequested_addon`, which also fires on one line.

**Why `violation` and not `concern`.** The customer stated it. A stated rule that only raises
a concern would be overridden by the concern threshold on its own, which is the opposite of
what "I do not buy these" means. `unrequested_addon` carries the same distinction: stated ⇒
violation, merely implied ⇒ concern.

**Why `unknown` and not `pass` when a category is missing.** An unestablished fact never
resolves in the agent's favour (`decision-rules.md` §5). A blank `merchant_category` with an
exclusion in force is exactly the case where approving would be the guess.

## Product words — `AGREED 2026-09-24`

> Live SCEN0117 — "Groceries and everyday household items only, maximum CHF 100 per order,
> from the shops I use. **No alcohol**, no gift cards, no cosmetics. When in doubt, ask."

The live pack files *"Wine and spirits"* (IT0168, *"Alcoholic beverages sold by a licensed
retailer"*) under `groceries`. A groceries allow-list admits it, and no category can exclude
it. So until now a wine order was approved against "No alcohol".

**The key.** `category_exclusion.require.item_keywords_none`: a list of phrases in lowercase
product-name words. It is a third dimension beside the two category lists, with a result of its
own, reported after them.

| Condition | Verdict | Reason code |
|---|---|---|
| no cart line has an `item_name` | `unknown` | `insufficient_evidence` |
| some line's name tokens contain **every** word of some phrase | `violation` — one line is enough, as for categories | `item_keyword_excluded` |
| otherwise | `pass` | `categories_permitted` |

- **Name tokens, never details.** Words are compared with the cart line's `item_name`, cut by
  the same tokenizer as `item_keywords_all` (`name_tokens`). The data dictionary guarantees the
  name matches its catalogue row. `item_details` is merchant text and is never read.
- **A phrase, not a bag of words.** Each entry's words must *all* appear in one name. So
  "wine spirits" is safe, while a model's *"wine and spirits"* split into three single words
  would put "and" in the list, and "and" is in "Drinks and snacks".
- **Soft drinks are not alcohol.** *"Drinks and snacks"* (IT0166, *"Soft drinks, snacks, and
  party supplies"*) matches nothing. That is why `drinks` is not in the lexicon.

**Rail 5c** (`llm-compiler.md`). The guard cuts each entry with `name_tokens` and drops
function words (`and`, `or`, `the`, …). It then keeps an entry only if some catalogue product
carries every one of its words. An entry that names nothing is dropped
(`excluded_word_matches_no_product`), because it excludes nothing and would only read as
enforced. If that empties the facet, the customer is asked which products should never be
bought. With no catalogue passed, entries are kept, which is the rail 5b rule.

**The lexicon.** The baseline reads concept words through a small written-down map in
`compile/baseline.py` (`EXCLUSION_LEXICON`): *alcohol*, *alcoholic* → `wine`, `spirits`,
`beer`, `liquor`. It is reviewed and grown by hand, never by a model, so *"no alcohol"* is
enforced even when no model is reachable. Words no product carries (`beer`, `liquor` in
today's catalogue) match nothing and cost nothing.

**Edits keep it.** `item_keywords_none` is an instruction-only key
(`settings.INSTRUCTION_ONLY_KEYS`). The "Things I never buy" control edits the two category
lists, so `preferences.apply_settings` carries the words through an edit of that control.
`amend.classify` compares them as a superset (fewer words widens), and `merchant_category_not_in`
is now compared too: an edit that removed an excluded kind of shop used to go unnoticed.
`compose` unions them across layers.

## Compiler

`compile/baseline.py` emits the facet for *"no X"*, *"never buy X"* and *"avoids X"*, and
only when X names a category in the pack's vocabulary (the loaded pack's, so live categories
count) or a lexicon concept. A negation with nothing behind it emits nothing: *"no more than
CHF 200"* is not an exclusion. Every such phrase goes into **one** facet, because the checks
read the first facet of a kind. The facet quotes each phrase. Until 2026-09-24 the loop stopped
at the first one, and live SCEN0117's *"No alcohol, no gift cards, no cosmetics"* kept only
cosmetics. *"gift card(s)"*, *"gift voucher(s)"* and *"gift certificate(s)"* are read as one
word before the category map, so `gift_card` is found but the bare word *card* or *gift* never
names a category.

The model's output schema now carries `item_category_not_in`, `merchant_category_not_in` and
`item_keywords_none`. Before, it listed none of them under `additionalProperties: false`, so a
`category_exclusion` from the model always arrived empty and was dropped. The prompt asks it to
state every exclusion the customer writes, even when an allow-list implies it. A mixed basket
of groceries plus a gift card passes the allow-list, because `item_matches_request` passes when
any line matches. It then only reaches the customer as an `unrequested_addon` concern. The
explicit exclusion declines it, citing their words.

Guard rails, as for every other facet: category values must be in the data pack's closed
vocabulary, or they are dropped with `unknown_category`. Product words pass rail 5c.

## Failure mode

Required input missing or malformed → the facet is absent or its lists are empty, and the
check is `not_applicable` or `unknown` per the table. It never raises: a category that is not
a string is compared as one and simply does not match.

## Fixtures

- **Exercised by:** none of the 45. No pack instruction excludes a category, so this check is
  `not_applicable` across the board.
- **Must NOT change:** **all 45.** This is additive by construction — the check stands down
  unless the customer stated an exclusion. `make diff-decisions` confirms.

## Vectors

`tests/vectors/category_exclusion.yaml` — includes the no-facet case, the empty-list case,
the missing-category case, one excluded line among many, and both dimensions firing together.

## Open questions

- [ ] Should an exclusion also cover `purchase_description`? No: it is untrusted merchant text
      (`decision-rules.md` §3), and a deny-list driven by the seller's own words is a deny-list
      the seller can evade by rewording. Categories come from the platform's records.
- [ ] `item_category_not_in` and `merchant_type`'s `merchant_category_in` can contradict each
      other — allow `sporting_goods`, exclude `sporting_goods`. `compose` does not detect it
      today; the result is that everything declines, which is safe but unexplained. Worth a
      conflict like the empty-intersection one in `customer-settings.md` §3.2.
