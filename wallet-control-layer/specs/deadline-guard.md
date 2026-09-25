# Spec: deadline guard

**Status:** agreed   **Owner:** Nakya   **Reason codes:** `deadline_risk`

Normative parent: [`decision-rules.md`](decision-rules.md) §7 —
*"if elapsed time approaches the deadline, emit the `uncertainty_policy` outcome with
`deadline_risk` rather than missing it."*

## Motivation

The platform assigns a decision deadline **when the attempt is queued**, not when we receive
it (`technical_details.md`, step 4): *"a request that waits undelivered past its deadline is
declined before delivery."* So the budget we get is whatever is left of the 8 seconds after
the platform's own queueing and the long-poll hand-off — it is not 8 seconds, and on a bad
venue network it can be much less.

A missed deadline is the worst of the three failure modes: the platform declines, the
cardholder sees a decline we never explained, and the audit record says nothing. A late
`step_up` we chose deliberately is strictly better than a silent platform decline.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `data.deadline_at` | ISO 8601 UTC instant | Required by the event schema. Absent ⇒ no guard (unbounded budget), logged. |
| `runtime.received_at` | ISO 8601 UTC instant | Informational; not used by the guard. |
| real clock `now` | timezone-aware UTC | Supplied by the caller — the guard itself never reads a clock. |
| `mandate.uncertainty_policy` | `ask` \| `decline` \| `approve` | Missing ⇒ `ask`. |

This is the **only** place in the engine where the real clock affects an outcome. Everything
else uses the simulated `authorization.timestamp` (`decision-rules.md` §1.2).

## Rule

`budget_ms = (deadline_at − now) × 1000`, measured **before** evaluation begins.

| Condition | Behaviour | Reason code |
|---|---|---|
| `budget_ms > RESERVE_MS` | Evaluate normally. | — |
| `budget_ms == RESERVE_MS` | **Evaluate normally.** The reserve is sized to cover a submit; spending it on the evaluation we were asked for is the better use. Boundary is `>=`. | — |
| `budget_ms < RESERVE_MS` | Skip evaluation. Emit the `uncertainty_policy` outcome immediately. | `deadline_risk` |
| `budget_ms <= 0` | Same as above — still submit. A late decision is refused with HTTP 408, which is recorded, not an approval. | `deadline_risk` |
| `deadline_at` absent | Evaluate normally, no guard. | — |

`RESERVE_MS = 1500` — `DESIGN`. Sized to cover one HTTPS submit on venue Wi-Fi (~500 ms
p99) plus the engine's own p99 (measured < 5 ms across all 45 fixtures, `make replay`) plus
margin. Tunable through `LEASH_DEADLINE_RESERVE_MS`; the engine's real cost is negligible, so
the reserve is almost entirely a network allowance.

### Outcome mapping

Shares `combine()`'s resolver, `domain/types.py::resolve_uncertainty` — but **not** its
`approve` branch, and the difference is deliberate.

| `uncertainty_policy` | Decision | |
|---|---|---|
| `ask` | `step_up` | |
| `decline` | `decline` | the stricter choice is never overridden |
| `approve` | **`step_up`** | floored — see below |

An earlier version of this spec mapped `approve → approve`, reasoning that a spent deadline is
"the same statement as an `unknown`: we could not establish the facts." That was wrong, and
the wrongness is worth stating because it is the same error a reviewer would make:

> *"Approve when unsure"* is the customer resolving **a doubt about the purchase** in the
> agent's favour. It is not consent to pay for a purchase **nobody looked at**.

A missing return window is a doubt: the check ran, read the seller's text, and found no
figure. A spent deadline is the absence of an opinion. Reporting it as an approval tells the
customer their rules were applied when none of them were. `deadline_risk` and
`engine_error_defaulted` are therefore both in `types.NOT_EVALUATED`, and both floor at
`step_up` — still the customer's decision, which is what they always had.

This moved no decision on the 45: the guard trips on none of them, and all five pack mandates
say `ask`. The compiler will not even produce `approve` from an instruction
(`tests/unit/test_uncertainty_policy.py`), so this path is only reachable through a customer
setting — which is exactly the kind of rarely-walked path that should not hide a hole.

A guard-tripped decision emits exactly one reason code, `deadline_risk`, and carries the
observed budget as evidence (`field: "deadline_budget_ms"`). It records **no** check results,
because none ran — a judge reading the record must not think checks passed.

### One bound, not two — considered and declined

A second bound was considered: cap the engine at *(arrival + N ms)* as well as at
*(deadline − reserve)*, so a message that arrives late gets only what is left rather than the
full allowance. It is the more obviously careful design, and it is the wrong one here.

`domain/` is pure. No check performs I/O, reads a clock or waits on anything, and the measured
p99 across all 45 fixtures is **under 5 ms** against a 1,500 ms reserve. An engine budget
would therefore be a bound that cannot fire — and `domain/settings.py` states the rule this
repo applies to exactly that situation:

> *"A setting no check reads is not 'unsupported', it is inert."*

Inert config is worse than absent config, because it reads on a slide as protection that is
not there. If a check ever acquires I/O, the second bound becomes real and should be added
with it; until then the reserve is the whole guard, and it is a network allowance, which is
what the latency risk actually is.

## Failure mode

Unparseable `deadline_at` → treated as absent, evaluate normally, log with the
`authorization_id`. The guard is a safety net; it must never itself become the thing that
breaks a decision.

## Fixtures

- **Exercised by:** none of the 45 — the replica issues a full 8-second deadline and the
  engine answers in single-digit milliseconds. This is deliberate: the guard is insurance
  against event-day conditions, not a fixture behaviour.
- **Must NOT change:** all 45. `make diff-decisions` must report no change after this lands.

Forced in tests by handing the guard a `now` past the deadline, never by slowing the engine.

## Vectors

`tests/vectors/deadline_guard.yaml` — over budget, exactly at the reserve boundary, under the
reserve for each of the three uncertainty policies, past the deadline, and `deadline_at`
absent.

## Open questions

- [ ] `ASSUMPTION:` the real API's `deadline_at` is 8 s from **queueing**, as documented, so a
      long-polled request can arrive with < 8 s left. Confirm the observed budget on the day
      with `make probe-live` and re-size `RESERVE_MS` from the measurement.
