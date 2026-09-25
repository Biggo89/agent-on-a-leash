# Spec: session_integrity

**Status:** agreed   **Owner:** Nakya
**Reason codes:** `device_novel`, `velocity_elevated`, `unusual_hour` (concerns)

## Motivation

> SCEN0003 — "The agent may buy clothing for me, up to CHF 250 per order, from shops I have
> used before. **Pause anything that looks like someone other than me is driving the session.**"

The verb is **pause**, not decline. The customer is asking for a step-up, and the scenario's
control question is: *"Does the concept escalate when session signals deteriorate, and relax
again when they recover?"*

The run tells that story in three acts:

```
AU0024  08-14 18:20  DVC-B73E47 known  Loom and Pine   16 prior  vel 0   ordinary
AU0025  08-15 12:05  DVC-B73E47 known  Milano Weave    15 prior  vel 0   ordinary (IT)
AU0026  08-17 19:40  DVC-4C0E9B NOVEL  Loom and Pine   16 prior  vel 0   ← first anomaly
AU0027  08-18 02:14  DVC-4C0E9B NOVEL  RainThread       0 prior  vel 0   ┐
AU0028  08-18 02:17  DVC-4C0E9B NOVEL  Cobalt Coatworks 0 prior  vel 1   │ 02:14–02:24 burst
AU0029  08-18 02:21  DVC-4C0E9B NOVEL  Thames Weave     0 prior  vel 2   │
AU0030  08-18 02:24  DVC-4C0E9B NOVEL  Cobalt Coatworks 0 prior  vel 3   ┘
AU0031  08-19 17:30  DVC-B73E47 known  Loom and Pine   16 prior  vel 0   ← recovered
AU0032  08-20 11:15  DVC-B73E47 known  Milano Weave    15 prior  vel 0   recovered (IT)
AU0033  08-21 16:00  DVC-B73E47 known  RainThread       0 prior  vel 0   recovered
AU0034  08-22 15:20  DVC-B73E47 known  Loom and Pine   16 prior  vel 0   recovered
```

`AU0026` is the one this check exists for. Novel device, but a **familiar** merchant, **under**
the cap, at 19:40 — every other check passes it. It is the first sign that someone else may be
driving, and it is exactly what "pause anything that looks like someone other than me" names.

## Recovery is automatic, by design

The check reads **only the current event's signals**. It holds no sticky "session is
compromised" state, so once the run returns to `DVC-B73E47` the score falls to zero on its own
and `AU0031`–`AU0034` are judged on their own merits.

Sticky state would be the over-blocking failure in its purest form: one anomalous night would
poison every later purchase. The scenario tests for this explicitly — *"relax again when they
recover"*.

## Signals — and one deliberate omission

| Signal | Weight | Basis |
|---|---:| --- |
| `device_novel` | **2.0** | `device_prior_approvals == 0` for this `(card_id, customer_device_id)`. A device never seen on this card is the canonical "someone other than me". |
| `velocity_elevated` | 1.0 | `recent_attempt_count_10m >= 2` — the platform's own field. |
| `unusual_hour` | 1.0 | Simulated timestamp in `[00:00, 05:00)` UTC. Weak alone; corroborating in combination. |

Weights live in `evaluator.CONCERN_WEIGHTS`; `STEP_UP_THRESHOLD` is 2.0, so **a novel device
alone escalates** while velocity or an unusual hour needs company. Deliberate: the
customer asked for a pause, and a pause is cheap and reversible — they approve in the app in
seconds. Requiring corroboration would let the *first* anomalous transaction through, which is
the one worth catching.

### Cross-border is NOT a signal here

The concern vocabulary contains `cross_border`, and using it would be wrong for these
cardholders. Approved history by merchant country:

| Card | Countries |
|---|---|
| `CA0023` (SCEN0003, Giulia Rossi — *"Regular short trips to Italy"*) | CH 132, **IT 23** |
| `CA0039` (SCEN0004) | CH 82, **US 21**, GB 15, NL 11 |
| `CA0011` (SCEN0002) | CH 99, AT 6 |

A foreign merchant is ordinary for all three. Scoring it would penalise `AU0025` and `AU0032`
(Milano Weave, Italy — both legitimate) and reinforce the wrong answer on `AU0038` (HarborByte,
US, 21 prior approvals), which the brief already names as looking alarming while being fine.
Foreignness relative to a persona's own history could be a signal; foreignness alone is not.

## Not gated on a facet

Unlike the merchant and item checks, this one always runs. Session integrity is an issuer-level
duty rather than a preference a customer has to remember to state, and it is the same reasoning
as `merchant_lookalike`. Verified safe: across the pack **only** `AU0026`–`AU0030` carry any
session signal at all, so an always-on check changes nothing outside SCEN0003.

SCEN0003 happens to ask for it explicitly, which would make it a stated rule — but both
readings produce the same outcome (`step_up`), so nothing turns on the distinction today.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `enrichment.device_prior_approvals` | `int` | Never null; `0` ⇒ novel |
| `authorization.customer_device_id` | `str` | **Empty ⇒ no device signal.** History carries empty ids on `in_store`/`atm` rows |
| `authorization.recent_attempt_count_10m` | `int` | Platform-supplied velocity |
| `enrichment.night_hours` | `bool` | From the **simulated** timestamp |

## Rule

The check returns **one concern per signal present**, or a single `pass` when none are:

| Signal | Emitted when | Reason code |
|---|---|---|
| Novel device | `customer_device_id` non-empty and `device_prior_approvals == 0` — on the `run` basis only once this run has approved an order (§"A card with no history") | `device_novel` |
| Elevated velocity | `recent_attempt_count_10m >= 2` | `velocity_elevated` |
| Unusual hour | simulated timestamp in `[00:00, 05:00)` UTC | `unusual_hour` |
| none of the above | — | `pass`, **no reason code** |

A clean session emits no code, so it does not lengthen every approval in the pack.

### A card with no history — `AGREED 2026-09-24`

"A device never seen on this card" needs a record of what the card has been seen on. The live
API's cardholders have none: their cards have no row in `authorization_history.csv`. So every
device was novel, and every live order in the first runs stepped up for `device_novel`: 19 of
19 step-ups, with 0 approvals in 37 decisions. That is not a session signal. It is the absence
of a baseline, and it asked the customer about their own phone on every purchase.

For a card with **no history at all** (`familiarity_basis: run`, the same test as
`check-merchant-permitted.md` §"No history at all"), the baseline is this run:

| Situation | `device_novel` |
|---|---|
| nothing approved yet in this run | **not emitted**. There is nothing to compare against, so the first device is the baseline |
| an order already approved in this run from this device | not emitted |
| orders approved in this run, none from this device | emitted, detail *"…a device not used for any order approved earlier in this session"* |

So SCEN0106's *"a new device"* still pauses the session: once the customer's own orders have
gone through, an order from a different device asks. Velocity and unusual hour are unchanged:
they never depended on history. On a card that has history the rule is exactly as before, and
the repo pack's `AU0026` stays a step-up (`CA0023` has 159 approved rows).

### Why separate concerns rather than one composite

The weights then live in `evaluator.CONCERN_WEIGHTS` with every other weight, which is what
`decision-rules.md` §5 promises — *"never scattered through the checks"*. A composite score
computed inside the check would be a second, hidden scoring layer, and the audit record would
show one opaque total instead of naming which signal fired.

`combine()` accepts a check returning several results for exactly this reason.

## Interaction with `merchant_permitted`

`AU0027`–`AU0030` already decline on `merchant_not_permitted`, and a violation outranks a
concern — so this check does not change their outcome, only enriches their evidence. **This is
worth knowing before the demo:** the burst is currently declined for the *merchant* reason, not
the *session* reason. If the PM wants the session story to drive the burst, the lever is the
open question in `check-merchant-permitted.md` (unfamiliar merchant → `step_up` instead of
`decline`); with that flipped, the burst would escalate on session grounds and `AU0033` — an
unfamiliar merchant in a **clean** session — would visibly differ from `AU0027`.

## Fixtures

- **Exercised by:** `AU0026` — novel device alone → `concern` → **`step_up`**. The only
  decision change.
- **Enriched but unchanged:** `AU0027`–`AU0030` — already declined on merchant grounds.
- **Must NOT change:**
  - `AU0031`–`AU0034` — known device, day hours, no velocity. Recovery must be automatic.
  - `AU0025`, `AU0032` (Italy) and `AU0038` (US) — cross-border is not scored.
  - Every attempt in SCEN0000, SCEN0001, SCEN0002 and SCEN0004 — no session signals in the pack.

Expected regression: **exactly 1** decision changes — `AU0026` approve → `step_up`.

## Vectors

`tests/vectors/session_integrity.yaml` — includes the lone-novel-device escalation, the full
burst, the recovered session, the empty-device guard, and the cross-border non-signal.

## Open questions

- [ ] Novel device alone escalates. If the team finds this too eager for a real customer who
      just bought a new phone, the lever is `CONCERN_WEIGHTS["device_novel"]`;
      `tools/tune.py --weights` confirms it moves `AU0026` and nothing else.
- [ ] Foreignness **relative to a persona's own history** (a country never seen on this card)
      would be a defensible signal. It needs a per-card country index, which is a small
      addition to `HistoryIndex`, and no fixture requires it.
