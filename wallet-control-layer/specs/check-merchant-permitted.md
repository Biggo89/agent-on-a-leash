# Spec: merchant_permitted

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `merchant_familiar`, `merchant_not_permitted`, `merchant_familiarity_ambiguous`,
`merchant_history_unavailable`

## Motivation

Three of the five instructions restrict *who* the agent may buy from, by prior relationship:

> SCEN0000 — "…from a shop I use regularly."
> SCEN0003 — "…from shops I have used before."
> SCEN0004 — "…from a seller I have bought from before."

SCEN0001 states no merchant requirement. **SCEN0002 states a different one** — *"only from a
specialist sports retailer"* — which is about merchant **type**, not familiarity, and belongs
to a separate `merchant_type` check. This distinction is the whole ballgame: fixture `AU0023`
(Summit Thread, `sporting_goods`, **0** prior approvals) is *unfamiliar but fully compliant*
with SCEN0002's actual instruction. Declining it is over-blocking, which the brief names as a
failure mode three times.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `enrichment.merchant_prior_approvals` | `int` | Never null; `0` means no approved history for this `(card_id, merchant_id)` |
| `policy.intent_facets[kind=merchant_familiarity]` | object \| absent | Absent ⇒ the instruction states no familiarity requirement |
| `authorization.merchant.merchant_id` | `str` | Join key. **Never** `merchant_name` |

Enrichment required: `merchant_prior_approvals`, counted from `authorization_history.csv` over
**approved** rows only (declines are attempts, not a relationship). Familiarity is *not*
carried on the live event — the engine loads and indexes the history itself.

### Why a facet and not a `hard_rule`

The API's rule format could express `merchant_id in [...]`, but the compiler cannot produce
that list: `POST /v1/mandates` carries no identity, because a mandate is bound to a scenario
only when a run starts. The card — and therefore its merchant history — is unknown at compile
time. So the requirement compiles to a facet (`prior_approvals_min`) and is resolved against
the card in the event at decision time.

## Rule

`min` = `facet.require.prior_approvals_min`, default `1`.

| Condition | Verdict | Reason code | Evidence |
|---|---|---|---|
| no `merchant_familiarity` facet | `not_applicable` | — | — |
| `prior_approvals > min` | `pass` | `merchant_familiar` | prior count, merchant_id |
| `prior_approvals == min` (boundary) | **`pass`** — the requirement is "used before", so meeting the minimum satisfies it | `merchant_familiar` | prior count, merchant_id |
| `prior_approvals < min` **and** `prior_approvals_customer >= min` | `unknown` | `merchant_familiarity_ambiguous` | both counts, merchant_id, merchant_name |
| `prior_approvals < min` **and** `prior_approvals_customer < min` | `violation` | `merchant_not_permitted` | prior count, merchant_id, merchant_name |
| card has **no history at all**, run count `>= min` | `pass` | `merchant_familiar` | run count, merchant_id, `familiarity_basis=run` |
| card has **no history at all**, run count `< min` | `unknown` | `merchant_history_unavailable` | run count, merchant_id, merchant_name, `familiarity_basis=run` |

The last two rows replace the four above them for a card with no history at all. The counts
are then this run's own approvals (§"No history at all").

### Card or person

`AU0044` is the case. `CA0039` has **zero** approved purchases at `ME0023`; the same
cardholder's other card, `CA0038`, has **two**. Seventeen of the twenty pack customers hold
more than one card, so this is not an edge case in the data — it is the ordinary shape of it.

The cardholder wrote *"a seller I have bought from before"* about **themselves**. The card is
the unit we can *count* on; the person is the unit the sentence is *about*. When the two
disagree, neither answer is established:

- **Not a pass.** Reading the person's history as permission widens a rule the customer
  stated. Uncertainty widens, and widening costs a confirmation — the same asymmetry as
  `domain/amend.py` invariant I3 and `policy-ir.md` rule 3.
- **Not a violation.** *"You have not bought from Circuit and Pine before"* is a sentence the
  customer would read as simply untrue, and a control layer that tells a cardholder something
  they know to be false about their own shopping has lost the argument before it starts.
- **`unknown`**, which routes through `uncertainty_policy` exactly like every other
  unestablished fact. All five pack mandates say *"Ask me when uncertain"*, so `AU0044`
  becomes a `step_up` and the customer settles it in one tap. No new branch reaches
  `combine()`.

`AU0033` is the control: **neither** of `CU0012`'s cards has ever bought at `ME0026`, so
there is no ambiguity to resolve and the stated rule is simply not met. It stays a decline.
This is the line between the two fixtures, and it is why the rule tests the person's count
rather than softening every unfamiliar merchant.

The person-level figure comes from `customer_id` on `authorization_history.csv`, counted in
the same pass as the card-level one (`adapters/history.py`) — the same source as the counts it
is compared against, so the two can never disagree.

**Why `violation` and not `concern`:** the customer wrote this as a requirement, not a
preference. Per `decision-rules.md` §5 a definitely-breached stated requirement declines.
Treating it as a concern (weight 1.0, below the step-up threshold) would silently **approve**
`AU0033` — an unfamiliar shop in a clean session — which is precisely what the customer asked
us not to do. See Open questions for the alternative.

### No history at all — `AGREED 2026-09-24`

Since 2026-09-24 the organizers' live API runs ten scenarios on ten new cardholders (team17:
Omar Chen, `CA1331`). None of their cards has a single row in `authorization_history.csv`, which
the API serves byte-identical to the repo's. Four of those instructions still say *"a shop I use
regularly"*, *"a seller I have bought from before"*, *"the shops I use"*, *"my usual services"*.
Read against an empty file, every shop was "never used", and every order was declined:
the first live SCEN0122 run declined all ten camera lenses under CHF 900 as
`merchant_not_permitted`.

That silence is not evidence. A card with **no approved row at all** (any merchant, any
transaction type) is a card we know nothing about. It is not a card that has never shopped. So
*"you have not bought from Silver Works before"* is a claim we cannot make. It is the same
reason `merchant_familiarity_ambiguous` exists: never tell a cardholder something about their
own shopping that the data does not show. For such a card:

- **The counts come from this run.** `merchant_prior_approvals` counts the orders approved
  earlier in this run at this merchant. Those are engine approvals, plus step-ups the customer
  approved, which enter the ledger when they are resolved. The enrichment records
  `familiarity_basis: run`. The customer's first "yes" at a shop is real evidence that the shop
  is theirs, so the next order there passes.
- **A shop not yet approved in this run is `unknown`**, with reason code
  `merchant_history_unavailable`, never a violation. It routes through `uncertainty_policy`.
  All four live instructions say "ask", so the customer is asked once per shop. It stays
  `unknown` even after other shops were approved in the run. A run's approvals show *some* of
  the shops a person uses, never all of them, so they cannot prove "never" either.

*"Once the customer has confirmed one shop, may a different one be declined?"* No. That would
turn an absence of data into an accusation again. Asking stays the useful intervention
(challenge.md: *"one ambiguous, unsafe, or manipulated transaction receiving a useful
intervention"*). In SCEN0122 the manipulated agent's switch to another seller still reaches the
customer as a question, and the decline is theirs to give.

**Scope, and why the board cannot move.** "No history at all" is decided per card from the
history file (`HistoryIndex.has_history`). Every card in the repo pack has at least 38 approved
rows (checked 2026-09-24), so the branch cannot fire on any of the 45 fixtures. The person-level
count plays no part here: the history file is also the only source of a card's owner, so a card
with no rows has no person to count for.

**Where the run's approvals come from.** They come from `seen_in_run` joined with the ledger's
approvals, the same join the duplicate, split-order and goal checks use. It is therefore subject
to the same limit: after a service restart `resume()` rebuilds the ledger but not the events,
so orders approved before the restart are no longer counted. That fails towards asking.

## Failure mode

The check cannot distinguish "0 because the merchant is genuinely new" from "0 because the
history file failed to load". Before 2026-09-24 the second declined every familiarity-gated
purchase. Since the no-history rule, it asks instead (every card would read as having no
history), which is the safer failure but still a silent one.
Guarded by `tests/unit/test_data_pack_invariants.py::test_history_index_is_populated`, which
asserts non-trivial history for all four scenario cards. Prefer a loud startup failure over a
silent index.

## Fixtures

**Exercised by** (all have 0 prior approvals on their card):

| Fixture | Scenario | Merchant | Expected |
|---|---|---|---|
| `AU0027`, `AU0033` | SCEN0003 | RainThread | `violation` — clean session does not suspend the rule |
| `AU0028`, `AU0030` | SCEN0003 | Cobalt Coatworks | `violation` |
| `AU0029` | SCEN0003 | Thames Weave (GB) | `violation` |
| `AU0039` | SCEN0004 | PixelHarbour | `violation` — also a lookalike |
| `AU0044` | SCEN0004 | Circuit and Pine | `violation` |

**Must NOT change** — no familiarity facet in SCEN0002, so the check is `not_applicable`:

| Fixture | Why |
|---|---|
| `AU0023` | Summit Thread, 0 prior, **fully compliant** with "specialist sports retailer" |
| `AU0022` | GreenLoop, 0 prior — fails on *retailer type*, which is a different check |
| `AU0038` | HarborByte, **21** prior approvals — cross-border and USD, but familiar. Must approve |
| All of SCEN0001 | Alpine Basket, 26 prior; no familiarity facet anyway |

## Vectors

`tests/vectors/merchant_permitted.yaml` — includes the `min` boundary and the
no-facet case.

## Open questions

- [ ] **`violation` vs `step_up` for an unfamiliar merchant.** Declining is faithful
      enforcement; stepping up ("is RainThread a shop you meant to use?") is arguably the more
      *useful intervention* the brief asks for, and lets the customer widen their own policy.
      Flipping it is a one-line change in this check. Decide with the PM before the demo.
