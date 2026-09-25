# Spec: split_order

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `split_order_suspected` (**concern**, weight 2.0)

## Motivation

> The brief: "Track relevant state over time so rolling limits, retries, duplicate requests,
> and prior decisions are handled correctly without blocking ordinary purchases unnecessarily."

A per-order cap is trivially walked past by placing two orders. SCEN0001 ships the case:

| | `AU0005` | `AU0006` |
|---|---|---|
| Merchant | `ME0001` | `ME0001` — **same** |
| Time | `2026-08-13T17:20:00Z` | `2026-08-13T17:26:00Z` — **six minutes later** |
| Amount | CHF 70.00 | CHF 65.00 |
| Together | | **CHF 135.00 against a CHF 120.00 per-order cap** |

Each order is inside the cap on its own facts. Together they are not. Before this check the
engine approved both, and the cap the customer wrote was worth nothing to anyone willing to
press the button twice.

### Why the existing checks do not cover it

`check_duplicate_order` deliberately does not, and says so
([`check-duplicate-order.md`](check-duplicate-order.md), "Condition 4 is what keeps order
splitting legal"):

> "Deliberate order splitting is a rolling-limit question, not a duplicate one, and this check
> must stay out of it."

That deferral was right about the mechanism and wrong about the coverage. **The rolling limit
only bounds splitting when the mandate states a period rule**, and only SCEN0001 does.
SCEN0002, SCEN0003 and SCEN0004 carry a per-order cap and no period cap, so in three of the
five scenarios nothing at all watches the total.

And even in SCEN0001 the period limit is the wrong instrument: it caught the consequence three
orders later (`AU0007`, `AU0008`, `AU0009` all declined on `period_limit_exceeded`) while
never naming the cause, and it declined `AU0008` — an ordinary grocery order — because the
ledger was carrying CHF 65.00 that should never have entered it.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `enrichment.same_merchant_recent` | `tuple[tuple[str, Money], ...]` | Committed orders at this merchant inside the split window. Empty ⇒ none, never "unknown" |
| `enrichment.is_duplicate_of` | `str \| None` | Set ⇒ this check stands down |
| `authorization.billing_amount_chf` | `Money` | The current order |
| `hard_rules` scope `purchase` | via `binding_cap` | Absent ⇒ `not_applicable` |

### Enrichment rule (`adapters/parse.py`)

`same_merchant_recent` lists `(authorization_id, billing_amount_chf)` for every prior attempt
in the same run where all of:

1. same `merchant_id`;
2. inside the split window — default **30 minutes** of simulated scenario time, `0 <= gap`;
3. **committed** — approved, or stepped-up and awaiting the customer, read from the ledger's
   `approvals ∪ pending`.

Condition 3 is the same committed-set reasoning as the duplicate check. A *declined* prior
order charged nothing, so it cannot contribute to a total; counting it would decline the
retry of a retry forever.

The window is deliberately shorter than the duplicate window's 60 minutes. Splitting is a
burst behaviour: two orders minutes apart at one shop. An hour apart at the same grocer is
ordinary life, and the rolling period limit is the right instrument at that distance.

## Rule

`total = billing_amount_chf + Σ same_merchant_recent`

| # | Condition | Verdict | Reason code |
|---|---|---|---|
| 1 | no `purchase`-scoped cap in `hard_rules` | `not_applicable` | — |
| 2 | `same_merchant_recent` empty | `pass`, **no reason code** | — |
| 3 | `is_duplicate_of` set | `not_applicable` | — |
| 4 | `total < cap` | `pass`, **no reason code** | — |
| 5 | `total == cap` | **`pass`** — see boundary | — |
| 6 | `total > cap` | `concern` (weight 2.0) | `split_order_suspected` |

**Boundary (row 5).** Equality passes, matching `check_per_order_limit`, where "at or below
CHF 120" is `<=`. Two orders that together land exactly on the cap have not exceeded
anything. When the mandate's cap is strict (`operator: "<"`) the comparison follows it, again
matching the per-order check.

**Row 3 — stand down for a duplicate.** `AU0035` + `AU0036` are 25 minutes apart at `ME0022`
for CHF 289.00 each, CHF 578.00 against a CHF 400.00 cap. That pair is already a
`duplicate_order` step-up with a better message — *"you approved this same CHF 289.00 order
25 minutes ago"* — and two reason codes for one event would make the customer read a splitting
accusation about an order they simply placed twice. Same precedent as
`check_unrequested_addon` standing down when `matching_lines` is empty: *"that is
item_matches_request's finding, already made with a better message."*

### Why a concern and not a violation

The customer wrote a per-order cap; they did not write "do not place two orders". Each order
here is compliant on its own facts, and the *pattern* is the finding. That is an inferred
signal, which is the same test applied by `merchant_lookalike`, `duplicate_order` and
`unrequested_addon`.

Weight **2.0** places it in the adversarial tier and it escalates on its own: something is
working around a limit the cardholder set, which is not an ambient signal like the hour or the
velocity. The outcome is `step_up` — *"two orders at Alpine Basket within half an hour come to
CHF 135.00 against your CHF 120.00 per-order limit"* — and the customer decides. A flat decline
would punish a shopper who genuinely forgot the milk.

`CONCERN_WEIGHTS` gains `split_order_suspected: 2.0`; `decision-rules.md` §6 gains the code
under **Concerns**.

## Failure mode

* No cap in the mandate → `not_applicable`. Nothing to split against.
* Window is **simulated** time, never the real clock, like every other check but the deadline
  guard. A split spread over 31 minutes is not caught here; the period limit bounds it when
  the mandate has one, and when it does not, that is the honest limit of a per-order cap.
* `same_merchant_recent` empty is "none", never "unknown" — the run's own memory is always
  available, so there is no unknown branch and this check never escalates on missing data.
* Pure and total: no clock, no I/O, cannot raise.

## Fixtures

- **Exercised by:**
  - `AU0006` — `concern` → **`step_up`**. The one direct change.
- **Changed through the ledger** (CHF 65.00 no longer enters approved spend):
  - `AU0007` — `decline` `period_limit_exceeded` → **`step_up` `unrequested_addon`**. The real
    finding, a CHF 32.00 fragrance gift set on a household-groceries mandate, stops being
    masked by an inflated ledger.
  - `AU0008` — `decline` `period_limit_exceeded` → **`approve`**. CHF 234.50 + 65.50 = exactly
    CHF 300.00, which `<=` approves. This was the only false decline of an ordinary purchase
    on the board.
- **Must NOT change** — every same-merchant pair inside 60 minutes across all 45 was swept;
  there are exactly three, and the other two are inert:
  - `AU0028`/`AU0030` (SCEN0003, 7 min, CHF 493.00 vs a CHF 250.00 cap) — both already
    `decline` on other grounds, and `combine()` drops concerns when a violation is present.
  - `AU0035`/`AU0036` (SCEN0004, 25 min, CHF 578.00 vs a CHF 400.00 cap) — suppressed by row 3.
  - `AU0009` stays `decline`: with the corrected ledger at CHF 300.00 it reaches CHF 324.00,
    genuinely over the period cap.

Expected regression: **exactly 3** decisions change. Board `17 / 23 / 5` → `17 / 21 / 7`.

## Vectors

`tests/vectors/split_order.yaml` — includes the boundary (`total == cap`), the strict-operator
boundary, the duplicate stand-down, the no-cap case and the empty-window case.

## Open questions

- [ ] 30 minutes is a judgement, like the duplicate check's 60. If Viseca has a house figure
      for related-authorization bursts, use theirs — it is one named constant.
- [ ] Should the window be a customer setting? It is the kind of thing a cardholder might
      reasonably want looser. Deferred: `domain/settings.py` requires that every offered
      setting be read by a check, and adding it is a separate, additive change.
- [ ] Splitting **across merchants** (two shops, one intent) is not covered and probably
      cannot be, without reading intent. The period limit is the only instrument there.
