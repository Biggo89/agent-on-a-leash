# Spec: spending_hours

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `within_spending_hours`, `outside_spending_hours`, `within_spending_days`,
`outside_spending_days`

## Motivation

> *"Do not buy anything between eleven at night and seven in the morning."*

A standing preference about **when** an agent may spend, which is the one dimension of a
mandate nothing else covers. It is the natural companion to the rolling window: a period cap
bounds how much, this bounds when.

It is deliberately **not** the same thing as the `unusual_hour` signal the session check
already emits, and the difference is the whole reason this exists as its own check:

| | `unusual_hour` (session) | `spending_hours` (this) |
|---|---|---|
| Comes from | our inference about risk | the customer's stated rule |
| Window | fixed 00:00–05:00 UTC | whatever the customer set |
| Verdict | `concern`, weight 1.0 — needs corroboration | `violation` — a stated rule was broken |
| Applies | always | only when the customer stated it |

Conflating them would be wrong in both directions. Turning the ambient signal into a violation
would decline every legitimate late-night order — `CU0004`, Noor Haddad, *"works rotating
hospital shifts and often buys meals and essentials outside standard office hours"*, with
**legitimate late-night activity** called out in the data pack itself. And turning a stated
rule into a weight-1.0 concern would let it be outvoted by the threshold, which is not what
"do not buy at night" means.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `authorization.timestamp` | ISO-8601, UTC | Schema guarantees present |
| `policy.intent_facets[kind=spending_hours]` | object \| absent | Absent ⇒ no time restriction |

`facet.require`:

- `hours_from` — integer 0–23, **inclusive**
- `hours_to` — integer 0–23, **exclusive**

### Which clock — `NORMATIVE`

The **simulated** clock (`authorization.timestamp`), never `runtime.received_at`.
`decision-rules.md` §1.2 assigns the simulated clock to everything about the purchase and the
real clock to the response deadline alone. An hours rule is a fact about when the customer's
money moved, so replaying a scenario must produce the same verdict in the afternoon as at
midnight — which is only true of the simulated clock.

All comparisons in **UTC**, as everywhere else in this engine. A production version needs the
cardholder's own timezone; the pack states none and inventing one would be a rule the customer
never wrote. Recorded as an open question rather than guessed.

## Rule

`h` = `timestamp.hour` (UTC, 0–23).

| Condition | Verdict | Reason code | Evidence |
|---|---|---|---|
| no `spending_hours` facet | `not_applicable` | — | — |
| `hours_from` or `hours_to` missing, not an integer, or outside 0–23 | `unknown` | `insufficient_evidence` | whatever was stated |
| `hours_from == hours_to` | `unknown` | `insufficient_evidence` | both bounds |
| `from < to` and `from <= h < to` | `pass` | `within_spending_hours` | hour, window |
| `from < to` and not the above | `violation` | `outside_spending_hours` | hour, window |
| `from > to` (wraps midnight) and `h >= from or h < to` | `pass` | `within_spending_hours` | hour, window |
| `from > to` and not the above | `violation` | `outside_spending_hours` | hour, window |

**Boundaries, stated explicitly.** `hours_from` is inclusive and `hours_to` is exclusive, so
`7–22` permits 07:00:00 and refuses 22:00:00. This is the same half-open convention as the
rolling window (§2.1), and picking the other one for either would make the two rules disagree
about what "until" means.

**A wrapping window is a first-class case, not an error.** "23:00 to 07:00" is how a person
says quiet hours, and requiring them to write two windows would be an interface leaking an
implementation.

**`hours_from == hours_to` is `unknown`, not "all day".** It could mean a zero-length window
or a whole one, and choosing either is a guess about a restriction. Asking is the honest
answer, and the editor cannot produce the case.

## Days — `AGREED 2026-09-24`

> Live SCEN0113 — "Weeknight dinners only: one delivery a day, CHF 40 maximum including the
> delivery fee, from my usual services. **Never at the weekend.**"

**The key.** `spending_hours.require.weekdays`: the days buying is allowed, as `mon` … `sun`.
*"Never at the weekend"* is `[mon, tue, wed, thu, fri]`. It sits in the same facet as the
hours because "when may the agent buy" is one question. It is judged as its own result,
reported after the hours: a facet stating only days is not judged on hours, and a facet stating
neither keeps the old `unknown`.

**Whose clock.** The customer's. Purchase timestamps are UTC, but the weekend a customer in
Winterthur writes is their own Saturday. A Friday 23:30 UTC order in September is Saturday
01:30 in Zurich. Days are read in **Europe/Zurich**, computed in `domain/localtime.py` from
the EU daylight-saving rule: CEST (UTC+2) from the last Sunday of March 01:00 UTC to the last
Sunday of October 01:00 UTC, CET (UTC+1) otherwise. No timezone database is read, and
`tests/unit/test_localtime.py` checks every half hour of 2024-2027 against `zoneinfo`.
ASSUMPTION: every cardholder keeps Swiss time. The pack states no timezone, and all thirty
customers, repo and live, live in a Swiss region. This settles the open question below for
days, not for hours.

**Hours stay UTC.** An hours window comes from the preferences control, whose help text says
UTC, and switching its clock would move the demo's 07:00-22:00 preference by two hours. That
is a separate decision. The evidence names the zone on every day result (`timezone`,
`order_time_local`), so an audit reader never has to guess which clock a verdict used.

| Condition | Verdict | Reason code |
|---|---|---|
| `weekdays` empty, or naming a day we cannot read | `unknown` — a guessed day would silently narrow or widen the rule | `insufficient_evidence` |
| the order's weekday in Zurich is listed | `pass` | `within_spending_days` |
| it is not | `violation` — a stated rule, like the hours | `outside_spending_days` |

The violation reads *"it was placed on a Saturday (Swiss time), and you only buy on Monday to
Friday"*, and its remedy is *"An order on Monday to Friday would meet your preferences."*

**Friday is a weekday.** "The weekend" is Saturday and Sunday. ASSUMPTION: a customer who
means Friday evening too writes it. Friday night is where *"weeknight"* is ambiguous, and
that word is never compiled into a rule (below).

**Edits and layers.** `weekdays` is an instruction-only key (`settings.INSTRUCTION_ONLY_KEYS`).
The hours control has no days, so `preferences.apply_settings` carries them through an edit of
it. `compose` intersects the days of every layer that names any, and an empty intersection is a
`preference_conflict`. `amend` compares them with "not set" meaning all seven days: fewer days
narrows, more widens. Amend also no longer reports a spurious hours change between two facets
that state no hours.

**Not compiled into a rule.**

- *"one delivery a day"* is a count of orders, which no check reads yet. It is deferred as C1,
  in `live-instructions.md`, and the model's prompt names it as a requirement to raise as an
  open question.
- *"Weeknight dinners"* names a time of day without stating one. The baseline asks *"What hours
  count as 'dinners'? Until you say, the agent does not limit the time of day."* The model may
  propose hours only at `confidence: low`, which by rail 8 becomes a question, never a rule.

## Compiler

`compile/baseline.py` emits `weekdays: [mon … fri]` for *"never/not (on/at) the weekend(s)"*
when it ends a clause, for *"no weekend orders/deliveries/purchases/shopping"*, and for
*"weekdays only"* or *"only on weekdays"*. A bare *"weekend"* is not a rule: *"book a weekend
getaway"* names a trip, and *"no weekend surcharge"* names a fee. It never emits hours: the
phrasings a person uses (*"not at night"*, *"during the day"*, *"dinners"*) need a mapping to
numbers that a cue list gets wrong in exactly the way `llm-compiler.md` §"Degree" describes.
Hours are reachable from `PUT /v1/preferences`, where the customer picks them, and from the
model behind the usual guard. The model's schema now carries `hours_from`, `hours_to` and
`weekdays`. Before, it listed none of them, so it could never write this facet. The guard keeps
an hour only as a whole number 0-23. It reads day names (`mon`, `Monday`) and drops the whole
list, with a question, when one day is unreadable.

## Failure mode

Required input missing or malformed → `unknown`, which routes through `uncertainty_policy`.
A malformed hours rule therefore asks the customer rather than silently permitting or
silently refusing everything.

## Fixtures

- **Exercised by:** none of the 45 — no pack instruction states hours. `AU0027`–`AU0030`
  (the SCEN0003 night burst, 02:00–03:00 UTC) are what a quiet-hours rule *would* catch, and
  they are the vectors' worked example.
- **Must NOT change:** **all 45.** Gated on a facet no pack instruction produces.

## Vectors

`tests/vectors/spending_hours.yaml` — includes both boundaries, the wrapping window, the
equal-bounds case and the malformed case.

## Open questions

- [ ] **Timezone.** UTC today. A cardholder in Zurich writing "not after 23:00" means 23:00
      local, which is 21:00 or 22:00 UTC depending on the season. The pack carries
      `home_region` but no timezone, and deriving one from a region name is the kind of guess
      this codebase refuses elsewhere. Needs a field on the customer, not an inference.
- [ ] Should an out-of-hours purchase be a `step_up` rather than a `violation`? A customer who
      set quiet hours and is awake anyway might want the chance to say yes. Today the rule is
      taken at its word; `uncertainty_policy` is the existing lever for people who want to be
      asked instead.
