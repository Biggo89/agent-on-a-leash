# Spec: LLM policy compiler

**Status:** implemented   **Owner:** Nakya   **Reason codes:** none — this component emits no
decisions. It produces a Policy IR that a human confirms before anything is enforceable.

Companion to [`policy-ir.md`](policy-ir.md), which defines the IR itself. This file defines
**who may write into it, and what is done to what a model writes.**

---

## Motivation

> *"Buy the 27-inch monitor I chose, from a seller I have bought from before, for CHF 400 or
> less. Do not add anything I did not ask for."* — SCEN0004

The regex baseline (`compile/baseline.py`) handles the five pack instructions because we wrote
it while looking at them. It is a floor, not a solution: a cardholder writing *"nothing from
the big marketplaces, and only if I can send it back"* gets an empty policy and five open
questions. The model closes that gap — that is the whole of O2's *"enforce the whole
instruction"* once the instruction is no longer one of five we have read.

**This is the only place a model runs.** `AGENTS.md` §3.1. There is no deadline here, no
merchant text, and a human reads the output before it binds.

**Provider: OpenRouter by default, or Swisscom's Apertus**, over plain HTTP with `httpx`, which
is already a core dependency — the compiler adds none. We use no SDK features (one JSON POST, no
streaming, no tool loop), so the port to TypeScript is a `fetch` call rather than a second SDK,
and swapping model or provider is an environment variable rather than a rewrite. OpenRouter's
own model routing is used as a first fallback tier: see §"Fallback".

### Providers

`LEASH_COMPILER_PROVIDER` picks one; unset or unknown is `openrouter`. Both are OpenAI-compatible
chat completions, and the request, the guard, the safety floor and the fallback are identical.
Only what the table says differs, and each provider reads **only its own key**, so an OpenRouter
key can never be sent to Swisscom or the other way round.

| | `openrouter` (default) | `apertus` |
|---|---|---|
| Endpoint | `https://openrouter.ai/api/v1` | `https://api.swisscom.com/products/swiss-ai-weeks/apertus-1.5-70b/v1` (`APERTUS_BASE_URL`) |
| Key | `OPENROUTER_API_KEY` | `SWISSCOM_API_KEY`, from keymaker.ai-weeks.ch, **valid 60 minutes** |
| Model | `LEASH_COMPILER_MODEL`, default `google/gemini-3.7-flash` | `APERTUS_MODEL`, default `swiss-ai/Apertus-v1.5-70B` |
| Routing tier | yes, `LEASH_COMPILER_FALLBACK_MODELS` | **none** — one model per endpoint, so Apertus or the baseline |
| Schema as `response_format` (strict) | yes | **no** — see below |
| Schema in the system prompt | no — structured outputs show it to the model | **yes**, plus which `require` keys each facet kind reads |
| `max_tokens` | 32 000 (reasoning models think inside it) | **3 000** — see below |
| Extra headers | `HTTP-Referer`, `X-Title` (OpenRouter rankings) | none |

**Why Apertus is configured differently — measured 2026-09-24, not assumed:**

| Observation | Consequence |
|---|---|
| With the strict schema as `response_format`, Apertus opened the JSON correctly and then emitted whitespace until `max_tokens`: 4 000 tokens, 66 s, unparseable. Grammar-constrained decoding permits unlimited whitespace, and the model took it | `structured_output=False`. The schema goes in the prompt; the guard re-checks every field regardless, so nothing is enforced unchecked |
| Read from the schema rather than constrained by it, the model filled every `require` key in every facet — strict mode lists them all as required | the prompt appendix names the keys each kind reads (from `FACET_REQUIRE_KEYS`) |
| The gateway reserves `max_tokens` **up front** against **12 500 output tokens per minute** (`x-ratelimit-limit-otpm`; input: 50 000/min). 13 000 → 429 before the model runs; 12 000 → 200 with 500 left | `max_tokens` is throughput: every token asked for is a token of the minute spent. Test-pinned at ≤ 12 500 |
| The gateway times out at ~55 s (HTTP 504); Apertus generates ~62 tokens/s with ~1.6 s to first token | nothing past ~3 400 tokens can arrive anyway; an IR is ~750. 3 000 gives 4× headroom and about four compiles a minute |

**Measured result, 2026-09-24 — why `openrouter` stays the default.** Same five pack
instructions ×3 and six invented ×2, through the real compile path (`make compare-providers`):

| | gemini-3.7-flash via OpenRouter | Apertus 1.5 70B via Swisscom |
|---|---|---|
| Compiled without fallback | 27/27 | 27/27 |
| Median compile time, pack / invented | 11 978 ms / 10 220 ms | **9 149 ms / 7 681 ms** |
| Same policy on every repeat | yes | yes (temperature 0 is deterministic here) |
| Invented-instruction expectation findings | 0 | 6 — 3 invented `no_additions`, 1 invented `order_terms`, 2 missed facets |
| **`make replay-llm` — decisions moved of 45** | **0** | **15** — 14 over-blocks, 1 under-block |
| … after keyword rails 5a/5b (§"Keywords"), live re-run | **0** | **2** — AU0007, AU0013 |

The 15 have three causes. The two keyword causes are the same class of error — a keyword no product name can contain, which prompt rule 5 names and Apertus made anyway:

| Scenario | Apertus compiled | Effect |
|---|---|---|
| SCEN0004 (7) | `item_keywords_all: ["27-inch", "monitor"]` — the name matcher tokenises on `[a-z0-9]+`, so `27-inch` is never a token | every monitor `item_not_requested` |
| SCEN0001 (7) | `item_keywords_all: ["groceries", "household"]` — in no grocery product name | every basket `item_not_requested` |
| SCEN0002 AU0013 (1) | no `item_attribute` — size 43 dropped on every run, and absent from the guidance | a wrong-size pair **approved** |

Rails 5a and 5b remove the two keyword causes for any model (13 decisions; the same 13 when
the recorded IRs are re-guarded offline). What remains is not a keyword problem:

- **AU0013** — the dropped size 43, out of reach of any rail (§"Keywords", last list).
- **AU0007** — hidden behind the keyword defect until it was fixed: on SCEN0001 Apertus also
  emits `no_additions`, quoting *"Order our household groceries for delivery."*. The
  instruction never asks for nothing extra, so the groceries-plus-gift basket the baseline
  steps up is declined as `unrequested_addon`. Verbatim provenance, unjustified inference —
  §"What provenance does not prove", exactly.

Apertus is faster and fully functional as a provider, so it stays selectable. It is not the
default, because a compiler that moves the board is not a better compiler however fast it is.

A reply wrapped in one Markdown code fence is unwrapped before parsing, for either provider. That
is packaging, not policy: the content still goes through the whole guard.

`make compare-providers` compiles the pack instructions with the baseline and each provider and
prints the enforced-policy differences and the compile time in ms (`tools/compare_providers.py`,
`--invented` for the six unseen instructions, `--runs N` for stability). The decisive check for a
provider is still `LEASH_COMPILER_PROVIDER=<name> make replay-llm`.

---

## The trust argument

Three properties, in the order a judge will ask about them.

1. **The model's only input is the cardholder's own words.** `compile(instruction: str)` takes
   one string plus static vocabularies read from the data pack. No event, no cart, no merchant
   name, no `item_details` is reachable from this module — enforced by the signature and by
   the dependency direction (`compile/` imports `domain/` and `adapters/datapack`, never
   `runtime/`). Merchant-controlled text has no path to a prompt.
2. **The model proposes; it does not decide.** Every field it returns passes the guard in
   §"Guard rails" before it reaches an IR. The guard is deterministic, pure, and tested
   without a network.
3. **The model is optional.** Any failure — no key, no package, timeout, malformed output,
   empty result — falls back to the deterministic baseline, and the resulting mandate still
   works. §"Fallback".

---

## Inputs

| Input | Source | Notes |
|---|---|---|
| `instruction` | the cardholder, via the UI | verbatim, never modified — **the only variable input** |
| item categories | `items.csv` → `item_category` | closed vocabulary, 16 values |
| merchant categories | `merchants.csv` → `merchant_category` | closed vocabulary, 22 values |
| facet kinds | the checks in `domain/checks/` | closed vocabulary, 6 values |
| item catalogue | `items.csv` → `item_name` per `item_category` | read by rail 5b only; the caller passes it in, the guard never reads a file |

The vocabularies are passed to the model in the system prompt and re-checked by the guard
after. A value the pack does not contain cannot enter an IR even if the model returns it.

---

## Output

A Policy IR exactly as `policy-ir.md` defines it, plus three compile-time fields:

| Field | Meaning |
|---|---|
| `compiler` | `"llm:<model id>"`, or `"baseline-deterministic"` when the model did not run. The id is the model that **answered**, not the one we asked for — OpenRouter may have routed past a rate-limited primary, and the audit trail should say what actually compiled the policy |
| `instruction_sha256` | hash of `source_instruction`, taken at compile time (§"Round trip") |
| `compiler_notes` | every guard action taken, in order — what was dropped and why |

`compiler_notes` is the audit trail of the compile step. It is what lets a customer — or a
judge — see the difference between *what the model proposed* and *what we accepted*.

---

## Guard rails

Applied in this order to the model's raw output. Each rail states what it does when it fires.
**Every rail is a drop or a repair; none can add an enforced restriction the model did not
propose, and none can widen one it did** — with two bounded exceptions. Rails 5a, 5b and 5d
repair a keyword only when, as written, it matches **no product the facet admits** and so
declines every purchase of the kind the customer asked for (§"Keywords"). Rail 5d always says
so in an open question. Rail 11 never widens: a merged facet is the tighter of the two. Rail 12 drops a cap
stated in a currency the pack has no rate for, because no CHF amount equals it, and raises an
open question in its place (§"Foreign-currency caps").

| # | Rail | On failure |
|---|---|---|
| 1 | `source_instruction` is the caller's string | model's value discarded, always, silently |
| 2 | `provenance` must be a **verbatim substring** of the instruction, comparing on collapsed whitespace | item dropped; note `provenance_not_in_instruction` |
| 3 | facet `kind` ∈ {`item_identity`, `item_attribute`, `merchant_familiarity`, `merchant_type`, `order_terms`, `no_additions`, `category_exclusion`, `spending_hours`} | facet dropped; note `unknown_facet_kind`; **open question raised** |
| 4 | each `require` key ∈ the per-kind allowlist below | key dropped; note `unknown_requirement` |
| 5 | category values ∈ the data pack vocabulary | value dropped; note `unknown_category`; **open question raised** if that empties the facet |
| 5a | each `item_keywords_all` entry is replaced by the item check's own name tokens | split; note `keyword_tokenised` when more than case changed; an entry with no tokens is dropped |
| 5b | a word of the item-category vocabulary, in a facet that already sets `item_category_in`, that no catalogue product in those categories has among its name tokens, is not a keyword | word dropped; note `keyword_matches_no_product` |
| 5c | each `item_keywords_none` entry is cut into name tokens, function words dropped, and kept only if some catalogue product's name carries all of them (check-category-exclusion.md §"Product words") | entry dropped; note `excluded_word_matches_no_product`; **open question raised** if that empties the facet |
| 5d | an `item_keywords_all` word that is a word of a shop city in the pack (`merchants.csv`, 3+ letters) and that no catalogue product carries is a place, not a product word | word dropped; note `keyword_is_a_place`; **open question raised**: the place is not enforced |
| 6 | rule `field` = `billing_amount_chf`, `operator` ∈ {`<=`, `<`}, `value` > 0, `scope` ∈ {`purchase`, `period`} | rule dropped; note `invalid_rule` |
| 7 | `confidence` ∈ {`high`, `medium`, `low`}; missing ⇒ `low` | — |
| 8 | `confidence: low` ⇒ see §"Low confidence" | facet dropped, question raised |
| 9 | spend **safety floor** vs the baseline: see §"Safety floor" | baseline rule adopted; note `safety_floor_applied` |
| 10 | `uncertainty_policy` ∈ {`ask`, `decline`, `approve`}; anything else ⇒ `ask` | note `invalid_uncertainty_policy` |
| 11 | facets deduplicated by `kind` — the checks read the **first** of a kind (`domain/policy.py`). `category_exclusion`, `order_terms` and `spending_hours` only ever add restrictions, so a second one is **merged** tightest-wins with composition's own rules (`compose.merge_facets`), quoting both spans; any other kind, or a merge that conflicts, keeps the first | merged: note `facet_merged`. Otherwise later duplicate dropped; note `duplicate_facet` |
| 12 | a rule whose `provenance` names an amount in another currency is **converted to CHF** at the pack's fixed rate, rounded down to the centime; `value` becomes the lower of the model's value and the converted amount, and the rule records `stated`. A currency the pack has no rate for cannot be enforced honestly — see §"Foreign-currency caps" | converted: note `currency_converted`. No rate: rule dropped, note `currency_not_convertible`, **open question raised** |

### Per-kind `require` allowlist

Derived from what the checks actually read. A key not in this table is inert — the danger is
not that it misbehaves but that it *looks* enforced on the review screen and is not.

| `kind` | allowed `require` keys | read by |
|---|---|---|
| `item_identity` | `item_category_in`, `item_keywords_all`, `item_description` | `check_item_matches_request`, `matching_lines` |
| `item_attribute` | `size` | `check_item_attributes` |
| `merchant_familiarity` | `prior_approvals_min` | `check_merchant_permitted` |
| `merchant_type` | `merchant_category_in` | `check_merchant_type` |
| `order_terms` | `return_window_days_min`, `cancellable` (only `true`; any other value is dropped) | `check_order_terms` |
| `no_additions` | *(none — presence is the requirement)* | `check_unrequested_addon` |
| `category_exclusion` | `item_category_not_in`, `merchant_category_not_in`, `item_keywords_none` | `check_category_exclusion` |
| `spending_hours` | `hours_from`, `hours_to` (whole hours 0-23), `weekdays` (`mon`…`sun`; one unreadable day drops the list and asks) | `check_spending_hours` |

### Keywords

`item_keywords_all` is the one requirement whose effect depends on a fact outside the
instruction: what product names look like. `check_item_matches_request` passes a cart line when
every keyword is one of that line's **name tokens** — lowercase runs of `[a-z0-9]`
(`name_tokens` in `domain/checks/item.py`). A keyword that can never be such a token, or that no
product of the required kind carries, does not narrow the permission. It declines every
purchase of the thing the customer asked for, which is over-blocking with a reason code that
reads as the agent's fault.

Observed 2026-09-24 with Apertus 1.5 70B, identical on each of three runs (§"Providers"):

| Scenario | Model compiled | Why it can never match | Decisions moved |
|---|---|---|---|
| SCEN0004 | `item_keywords_all: ["27-inch", "monitor"]` | `27-inch computer monitor` tokenises to `27`, `inch`, `computer`, `monitor`; no token contains a hyphen | 7, all `item_not_requested` |
| SCEN0001 | `item_category_in: [groceries]`, `item_keywords_all: ["groceries", "household"]` | no groceries product has either word in its name — `Weekly grocery basket` is the nearest | 7, all `item_not_requested` |

**Rail 5a — tokenise.** Each keyword is replaced by its name tokens, computed by the same
function the check uses, so the two cannot drift: `27-inch` → `27`, `inch`; `Road Running` →
`road`, `running`. An entry with no tokens at all is dropped. The note `keyword_tokenised:
'27-inch' → 27, inch` is written when the split changed more than case.

Why this is safe: a keyword containing anything but `[a-z0-9]` is by construction not a token of
any product name, so as written it matched nothing, ever. The split requires exactly the words
the model wrote, at the granularity the matcher reads — the form the baseline compiles itself
(`27`, `inch`).

**Rail 5b — a kind word the kind already covers.** A keyword is dropped when all three hold:

1. it is a word of the item-category vocabulary — the tokens of the 16 category names
   (`groceries`, `household`, `sporting`, `goods`, …);
2. the same facet sets `item_category_in`, so the kind is already required by the field whose
   job that is (prompt rule 5: the category already covers it);
3. no product in the catalogue within those categories has it among its name tokens.

Note: `keyword_matches_no_product: 'household' (no groceries product has it in its name)`. No
open question: the category still enforces the kind, and the dropped word was the model's
choice of keyword, not a requirement the customer can answer.

Each condition exists because of a catalogue row that would otherwise go wrong:

| Without condition | Counter-example | What would go wrong |
|---|---|---|
| 1 | `kayak` for `sporting_goods` — no product has it either | the customer asked for a thing the catalogue does not sell; dropping the word would let the agent buy any sporting good. Declining is **correct** there |
| 2 | `groceries` as the only statement of kind, with no `item_category_in` | dropping it would leave an identity with no kind at all |
| 3 | `fuel` (`Fuel purchase`), `hotel` (`Hotel room`), `household` in `household` (`Household essentials`) | inside their own kind these words do separate products — `fuel` excludes the EV charging session. Dropping them widens |

The catalogue is `items.csv`, `item_name` per `item_category`. The data dictionary guarantees
that a cart line's `item_name` matches its catalogue row, so "no product has this token" is a
fact about every cart the engine can see, not a sample. `compile/llm.py` loads it through
`adapters/datapack` and passes it to `accept()`; `contract.py` stays pure. **No catalogue, no
rail 5b:** condition 3 cannot be proven, so the keyword stays and declines — the behaviour
before this rail.

**Rail 5d — a place is not a product word.** Observed 2026-09-24 on live SCEN0124, *"Book me a
hotel in Munich for 3 nights…"*. The model returned `item_category_in: [hotel]` and
`item_keywords_all: [munich]`. No hotel room is named after a city, so every booking would
have failed its own identity check (`item_not_requested`), the over-block the derived cap had
just removed. A keyword is dropped when it is a word of a shop city in the loaded pack and no
catalogue product carries it. The customer is told the place is not enforced (*"Where a shop
is cannot be checked yet, so 'Munich' is not enforced"*). Where a shop is has no check yet
(`merchant_city` is read by none). So the honest outcome is a stated gap, not a rule that
declines everything. `kayak` is not a place and stays, as rail 5b's counter-example says. No
cities, no rail: `llm.places()` returns None without a pack, and the keyword is kept.

What neither rail does, stated so nobody assumes otherwise:

- A kind word that *is* in some product's name passes. `grocery` appears in `Weekly grocery
  basket`, so a keyword `grocery` for `groceries` survives 5b and narrows the errand to that one
  basket. No syntactic rule can tell a distinguishing word from a descriptive one that happens
  to be in a name; prompt rule 5 and `make replay-llm` stand behind that case.
- A facet the model never proposed stays missing. SCEN0002's dropped size 43 — the one
  under-block in the measurement above — is out of reach of any rail; only the review screen
  shows it, by its absence.
- The baseline is untouched: its keywords are already tokens, and it does not pass the guard.

### Low confidence

`policy-ir.md` rule 3: *"`confidence: low` becomes an `open_question`, not a silent rule."*
Resolved asymmetrically, because the two directions of error are not symmetric:

| Item | `confidence: low` behaviour | Why |
|---|---|---|
| **intent facet** | **not enforced**; becomes an open question | A facet *restricts*. Enforcing a guess over-blocks, and over-blocking is a failure mode (`AGENTS.md` §0.7). AU0023 — unfamiliar shop, fully compliant — is exactly what a guessed `merchant_type` would wrongly decline. |
| **spend rule** | **enforced** *and* an open question is raised | A cap *permits*. Dropping a guessed cap loosens the mandate below what the customer wrote, which is the worse failure. The safety floor backs it up regardless. |

One sentence: **uncertainty never tightens the intent and never loosens the money.**

### What provenance does not prove

Rail 2 checks that a quoted span **exists** in the instruction. It cannot check that the span
**justifies** the rule, and no purely syntactic rail could.

Observed 2026-09-09 on SCEN0000 — *"Buy one ordinary grocery item for CHF 20 or less…"* — the
model emitted `no_additions` on four of six consecutive runs, quoting *"one ordinary grocery
item"*, and once quoting the single word *"one"*. Both are verbatim, so both are accepted. The
inference is defensible; the one-word citation is thin.

This is deliberate and it is the honest boundary of the mechanism. Provenance makes the
model's reasoning **inspectable** — the customer sees the facet next to the words it came from
and can reject it — but it does not make it **correct**. The claim to make in front of judges
is "a rule that cannot quote the customer's own words does not survive", not "every rule that
survives is well-founded". The second claim is false, and the review screen plus
`open_questions` are what stand behind it. Same family as §"Degree" below: the guard bounds
*form*, a human bounds *judgement*.

### Degree

The guard constrains *whether* a requirement exists and *what vocabulary* it may use. It does
not constrain **how strict** the requirement is, and nothing downstream does either: a facet
with `prior_approvals_min: 2` is as well-formed as one with `1`, and a two-category
`item_category_in` is as valid as a one-category one. Provenance does not help — the same
quoted span justifies either.

That gap is closed in the prompt (rule 7), not in code, because it is a judgement about
faithfulness rather than a property that can be checked against the instruction:

| Observed 2026-09-09, before the rule | Why it is wrong |
|---|---|
| *"a shop I use regularly"* → `prior_approvals_min: 2` | Over-blocking. Declines a shop used once, which the customer did not ask for. Nondeterministic across runs — the same instruction also compiled to `1`. |
| *"road-running shoes"* → `item_category_in: [clothing, sporting_goods]` | Under-blocking. Widens the permission rather than describing it. |

Neither moved the 45-decision board, but only because this data set happened to be kind.
After the rule, both compiled identically across three consecutive runs.

**This is a real residual risk, and the honest statement of it is:** the compiler cannot be
made deterministic, so `make replay-llm` is the check that matters — it compiles every mandate
with the model and fails if any of the 45 decisions moves.

### Guidance coverage

`guidance` is the plain-language account the customer reads before consenting. Every other
rail in this spec checks that nothing **unjustified** is enforced; this is the only one
checking the other direction — that everything enforced is **explained**.

Observed 2026-09-09: a SCEN0002 mandate enforcing five requirements whose guidance named only
the spend cap, varying between one and three lines across runs on identical input. Nothing in
the decision path notices, because guidance is not a decision — so `make replay-llm` cannot
catch it, and before this it was caught by nobody.

Closed in the prompt (rule 6 now asks for a line per rule and per facet) and reported by the
guard as `guidance_thinner_than_policy: <n> enforced, <m> explained`. The note is
**informational, not a rejection**: one good sentence can legitimately cover two related
requirements, so a short count is a reason to read the review screen rather than a defect. The
live assertion in `test_invented_instructions.py` allows that latitude and fails only when the
account is clearly incomplete.

### Safety floor

The baseline compiler runs on every compile, model or not. **Every cap the baseline extracted
must be covered by an accepted cap of no more than that amount, one for one.** An uncovered
cap is adopted from the baseline verbatim.

The match is on **value, not scope**, and that is the whole point. What plain-text extraction
proves is the *magnitude* — "CHF 120 is a cap in this instruction". It does not prove the
scope, which the baseline infers from cue words near the amount: whether *"never more than CHF
75 on a single order"* caps the order or the window is precisely the judgement a cue list is
bad at and the model is good at. A scope-keyed floor would import whatever the regex made of
it.

That sentence is the worked example, and its history is the argument. `_PER_ORDER` originally
matched only *"each order"* and *"per order"*, so the CHF 75 took the nearest **period** cue
instead and was scoped as a fourteen-day total — and then, under `decision-rules.md` §9, lost
to the looser CHF 250 rule on the same window and was **dropped entirely**: a CHF 200 single
order approved on an instruction that says never more than 75. The cue list has since been
widened to cover it. The point survives the fix: a cue list can always be widened and can
never be made complete, so the floor must not depend on the scope it produced.

One for one matters too. Baseline `{120 purchase, 300 period}` against a model that returned
only the CHF 120 cap must not pass: a single accepted rule cannot cover two baseline caps, so
the missing weekly total is adopted rather than silently swallowed by `120 ≤ 300`. Both sides
are matched in ascending order, which is optimal on a number line.

When a floor fires, any cap the model proposed that covered nothing *and* is looser than the
adopted floor is removed with the note `unsupported_cap_removed`. The evaluator would enforce
the tighter rule regardless — which is true because `decision-rules.md` §9 makes the tightest
cap binding, and was **not** true while the engine read the first matching rule instead. So
this is not about safety — it is about the review screen. A
customer shown *"each order stays at or below CHF 2000"* directly above *"…at or below CHF
200"* cannot check either one, and a mandate the customer cannot check is not consent.

So the model may re-scope a cap, tighten it, or split it — but a model that misreads *"CHF
120"* as 1200, or drops a limit entirely, cannot loosen the mandate past what plain text
already proved was written. The claim: **every limit written in the instruction is enforced at
no more than the amount written** — converted to CHF when it was written in another currency
(§"Foreign-currency caps"), and multiplied by the count when it was written per unit and the
count is written too (§"Derived caps") — and it holds without trusting the model at all.

### Derived caps — `AGREED 2026-09-24`

> Live SCEN0124 — "Book me a hotel in Munich for **3 nights** from 10 September to 13 September,
> **at most CHF 200 per night**, refundable rate only."

**What went wrong.** The baseline found "CHF 200" and, with no per-order cue near it, scoped it
per order. The model proposed CHF 600 (3 × 200), which the guard accepted: no rail checks a
CHF value against the text. But the floor then held the baseline's 200, adopted it, and
removed the model's 600 as `unsupported_cap_removed`. A three-night stay at CHF 190 a night
bills CHF 570, so every booking the customer asked for was declined. The guard was never the
problem. The floor faithfully enforced a misreading, because a price *per night* is not a
magnitude an order is bounded by.

**The fix is in the baseline, so the floor is right again.** An amount followed by a per-unit
cue (*"per night"*, *"a night"*, *"each night"*) is a price per unit. When the instruction
quotes exactly one count for that unit (*"3 nights"*, *"three nights"*), the baseline emits the
per-order cap `count × price`:

```yaml
- field: billing_amount_chf
  operator: "<="
  value: 600.00
  scope: purchase
  provenance:                     # both spans, highlighted in the customer's sentence
    - {source: instruction, quote: "CHF 200 per night"}
    - {source: instruction, quote: "3 nights"}
  derived: {unit: night, count: 3, unit_amount: 200.00, unit_currency: CHF}
```

The floor then holds 600, so the model's 600 covers it and survives. A model that drops the cap
gets the derived 600, never the nightly 200. The review screen reads *"Each order stays at or
below CHF 600.00 (3 nights × CHF 200.00 per night)"*, and the per-order check names the
arithmetic in every message (*"within your CHF 600.00 (3 nights × CHF 200.00) per-order
limit"*). `derived` never reaches the platform: the wire carries field, operator, value,
currency, scope and period_days only, and the platform's schema forbids anything else.

**Bounds, stated.**

- **No count, no derivation.** *"at most CHF 200 per night"* alone keeps the per-order reading
  (CHF 200) and asks *"Your limit is per night. How many nights may one order cover?"*. Two
  different counts in one instruction do the same. A guess at the number of nights would be a
  limit nobody wrote.
- **Nights only.** It is the one unit a live instruction uses. Another unit (per person, per
  day) is a spec change first, because each needs its own count cue.
- **Looser in one case, by design.** The cap bounds the stay, not each night. One night at
  CHF 590 fits under 600. Enforcing the nightly price per line needs the cart's line shape
  (quantity 3 at the nightly rate, or one line for the stay), which no live SCEN0124 order has
  shown yet. That is proposal A1 in `live-instructions.md`, deferred. On the wire it would be a
  new `field`, never a new `scope`, which the platform's rule schema limits to
  `purchase`/`period`.
- **A foreign nightly price** is multiplied first and converted once (`EUR 150 a night` × 2 →
  EUR 300 → CHF 285.00), like the total the platform bills. Rail 12, which converts a model
  rule from its own quote, knows no count: a model rule quoting *"EUR 150 a night"* at EUR 300
  is lowered to CHF 142.50. That over-blocks, never loosens, and no live instruction is
  affected. It is recorded here rather than fixed.
- **The model must also read it.** Prompt rule 4 now says a price per unit is not an order cap.
  A model that still returns the nightly price as a per-order cap is tightening, which the
  floor allows, so the over-block would return with it. `make compile-llm` on the live
  instruction is the check.

### Foreign-currency caps — `NORMATIVE`

Every cap is enforced against `billing_amount_chf`, so every rule's `value` is CHF. A limit the
customer writes in another currency is converted **once, at compile time**, at the pack's
fixed rate (`fx_rates.csv`, mirrored in `domain/money.py` `FX_TO_CHF`), **rounded down to the
centime**, and the rule keeps what was written:

```yaml
- field: billing_amount_chf
  operator: "<="
  value: 190.00                                  # EUR 200 × 0.95
  currency: CHF
  scope: purchase
  stated: {amount: 200.00, currency: EUR, rate: 0.95}
  provenance: "Pay no more than EUR 200"
```

**Why it matters.** The live API's SCEN0104 (2026-09-24) reads *"Pay no more than EUR 200"*.
The baseline recognised only `CHF` amounts, so it found no cap and the safety floor had nothing
to hold. The model returned `value: 200, currency: CHF`, and rail 6 accepted it, because the
guard stamps every rule `CHF` and the quote does contain "200". So a limit of EUR 200 (CHF
190.00) was enforced as CHF 200: an order of EUR 210 (CHF 199.50) was approved against words
that forbid it. This is the one gap of the four found that **loosens** a mandate.

**Why compile time, and why it is exact.** The platform bills a foreign order at `amount ×
rate`, rounded to the centime, from the same fixed table. Measured 2026-09-24 on all 4 non-CHF
repo attempts and all 726 non-CHF history rows, with no exception. So `amount ≤ EUR 200` holds
exactly when `billing_amount_chf ≤ CHF 190.00`, and a cap converted once cannot drift from the
charge it limits. It could drift only if the table carried a moving rate, and it does not
(`source: synthetic_fixed`, one `rate_date`). Rounding **down** keeps the converted cap from
ever being looser than the words. With every pack rate at or above 0.5 the boundary is also
exact, not merely safe: one cent over the stated amount bills at least half a centime over the
converted cap, which rounds above it.

**Recognised.** An ISO code the pack has a rate for (`CHF`, `EUR`, `GBP`, `USD`, any case)
before or after the number, and `€`, `£` or `$` before it. `$` is read as USD, the only dollar
the pack has a rate for. Both compilers recognise the same set: the baseline converts what it
finds, which is what lets the safety floor hold a foreign cap exactly as it holds a CHF one.

**How the guard converts a model rule (rail 12).** It looks for amounts in the rule's own
`provenance`, the span the model chose as the cap's source. If none is foreign, nothing
changes. Otherwise the stated amount is the foreign mention whose number equals the model's
`value`. That is the model copying the number, as it did with EUR 200. If no number matches, it
is the only foreign mention. The enforced value is the **lower** of the model's value and the
converted amount. A model that already converted (CHF 190) keeps its rule. A model that copied
the number (200) gets 190, with the note `currency_converted: EUR 200.00 → CHF 190.00 at 0.95`.
Taking the lower value never loosens, and a model that tightened on purpose keeps its
tightening, as rail 9 allows. When the provenance holds foreign amounts and none of them can be
singled out, the rule is dropped and an open question raised, rather than guessed at.

**A currency with no rate** (*"no more than SEK 500"*; a fixed list of common ISO 4217 codes,
not any three capitals, because *"USB 3 cable"* is not a limit in USB) cannot be enforced
honestly. There is no
CHF amount it equals. The rule is dropped (`currency_not_convertible`) and the customer is
asked: *"Your limit is in SEK, which this wallet cannot convert — what is it in CHF?"* The
baseline asks the same whenever an unconverted three-letter code sits beside a number. Dropping
here removes a restriction, which rails normally never do on their own. It is acceptable only
because the question blocks nothing silently: the mandate the customer confirms shows the
limit as unanswered.

**What the customer reads.** The per-order and period checks name the limit in the currency it
was written in, with the CHF figure after it: *"CHF 199.50 exceeds your EUR 200.00 per-order
limit (CHF 190.00)"*. Otherwise the customer reads a CHF 190 limit they never wrote, and
concludes the engine is wrong.

---

## Fallback

Two tiers, and the first one is free.

**Tier 1 — OpenRouter routing.** `LEASH_COMPILER_FALLBACK_MODELS` is sent as OpenRouter's
`models` array, so a primary that is down, rate-limited or out of credit is routed past to the
next model before we degrade at all. Set it to `""` for the strictest reading: this model or
the baseline, nothing in between. Every model listed must support the structured-output mode
in §"Output" — check before adding one.

**Tier 2 — the deterministic baseline.**

| Condition | Behaviour |
|---|---|
| the active provider's key unset (`OPENROUTER_API_KEY` / `SWISSCOM_API_KEY`) | baseline; note `model_unavailable: <that variable> is not set`. **No request is made** |
| connection error, timeout, DNS | baseline; note `model_error: <class>` |
| HTTP 4xx/5xx — including **402, out of credit**, and **401, an expired Apertus key** | baseline; note `model_error: http_<status>` |
| a 200 carrying an error envelope — OpenRouter's `{"error": {"code"}}` or Swisscom's `{"code", "error": "<string>"}` | baseline; note `model_error: <provider>_<code>` |
| no choices, or `finish_reason: length` (truncated) | baseline; note `model_output_invalid` |
| body or content is not JSON — a captive portal answering 200 with HTML | baseline; note `model_output_invalid` |
| every rule and facet dropped by the guard | baseline; note `model_output_empty` |
| `LEASH_COMPILER=baseline` | baseline; the model is never called |

Fallback is **total, not partial**: on any of these the returned IR is the baseline's, whole.
A half-model / half-regex IR would be the one artefact nobody can reason about.

Timeout: `LEASH_COMPILER_TIMEOUT_S`, default 60 s. Generous on purpose —
this runs once, at mandate creation, with a human waiting on a review screen and no platform
deadline anywhere near it.

**The decision path never calls this module.** `runtime/` reads a compiled IR held on the
session; it has no code path back to `compile/`. Pulling the key out of the environment
changes nothing about a running scenario — and this is mechanical, not a promise: importing
`domain.evaluator` and `runtime.runner` in a subprocess loads **no** `leash.compile` module at
all, which `test_compile_fallback.py` asserts. The claim is provider-independent, so switching
provider again does not weaken it.

---

## Round trip

`policy-ir.md` rule 1 — the API rejects an altered instruction and the run is lost with it.
Three checks, and each catches a different mutation:

| Where | Check | Catches |
|---|---|---|
| compile | `instruction_sha256 = sha256(instruction)` recorded in the IR | — |
| before submit | recompute over `payload["instruction"]`, compare to the IR's hash | a UI that edited the IR and posted it back; a compiler that touched the text |
| after submit | hash the `instruction` the API echoes on the draft | the platform normalising our text — the failure that would otherwise surface as an unexplained `instruction_mismatch` at `start_run` |

A mismatch is `422 instruction_altered`, refused before anything is created. The post-submit
mismatch is a `409 instruction_not_echoed`: the draft exists but is not usable, and saying so
at creation is better than discovering it when a run refuses to start.

---

## Failure mode

Required input missing or malformed → the instruction is empty or whitespace: `422
invalid_instruction`, no model call. Everything else degrades to the baseline; there is no
input for which this module raises.

---

## Fixtures

- **Exercised by:** all five pack instructions compile identically-or-better; the six invented
  instructions in `tests/fixtures/invented_instructions.yaml` prove generalisation past them.
- **Must NOT change:** the 45-decision board. The compiler produces the same *enforced*
  policy for the five pack instructions whether or not a model ran — the model's contribution
  on those five is better wording and better open questions, not different rules. `make
  diff-decisions` must report nothing after any change here.
- **Rails 5a/5b:** the recorded Apertus IRs of 2026-09-24, re-guarded, must no longer lose
  SCEN0001 and SCEN0004 to `item_not_requested`; every provider's `make replay-llm` must not
  get worse. Unit coverage: `tests/unit/test_compile_contract.py`, §"rails 5a-5b".

## Vectors

Not vector-suite material: this is compile-time, not decision-time, and the vectors are the
decision portability contract. Covered by `tests/unit/test_compile_contract.py` (the guard,
no network) and `tests/unit/test_compile_fallback.py` (every fallback branch, no key).

## Open questions

- [ ] Does the judging rubric reward the compile step at all, or only decisions? If only
      decisions, this stays as-is and the demo time goes to `customer_message`.
- [ ] Whose OpenRouter key runs it at the event (`GUIDELINES.md` §12, question 2) — the
      compiler runs in the engine process today, so the engine holds it. Budget matters:
      an exhausted account is an HTTP 402, which degrades to the baseline silently unless
      somebody is watching `compiler_notes`.
