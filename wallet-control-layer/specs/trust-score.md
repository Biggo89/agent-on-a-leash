# Spec: trust score

**Status:** agreed   **Owner:** Nakya   **Module:** `domain/score.py`
**Not a decision.** `approve` / `decline` / `step_up` are the decision. This is a rendering of
the evidence behind one.

## Motivation

> The brief: *"Judges should be able to understand what the system permitted, what evidence it
> considered, why it acted, and how customer retained control."*

A customer opening the app reads a verdict and a sentence. That is the right thing to read
first, and it answers *what happened*. It does not answer *how close was this* — and on a
phone, at a glance, before the sentence, that is the question a number answers better than
prose.

## What it must not be

This is the part worth reading twice, because a "trust score" is exactly the kind of feature
that quietly becomes a second, unaccountable opinion.

1. **No new judgement.** The engine's opinion about what matters lives in
   `evaluator.CONCERN_WEIGHTS` and `STEP_UP_THRESHOLD`. This module has no second table of
   importances. `CONCERN_UNIT` converts a weight into points at a fixed rate, so a tuned
   weight moves the score with it and `tools/tune.py` still owns the dial.
2. **No learned weights, no model.** Every deduction can be pointed at a `CheckResult` that is
   already on the record, and the record is already in the audit trail.
3. **It cannot contradict the decision.** The decision picks the band *before* any arithmetic
   runs, and the tail is capped so it cannot leave it. A "trusted-looking" decline is not a
   state this can reach.
4. **It never reaches the platform.** The score travels in `decision_summary` — the service
   response and the audit record — and **not** in `DecisionRecord.to_payload()`. The body we
   POST to the organizers' API carries exactly the fields their contract names. A presentation
   feature must not be able to affect a submission.

## The rule

```
score = BAND_TOP[decision] − Σ (cost of every finding that is not the driving one), capped
```

| Decision | Band top | Range after the cap |
|---|---|---|
| `approve` | 100 | 75 – 100 |
| `step_up` | 60 | 35 – 60 |
| `decline` | 30 | 5 – 30 |

**The driving finding is not deducted.** It is the reason the band is what it is, and charging
for it twice would mean a purchase declined for one clear breach scored worse than the band
implies for no stated reason. `_driving` picks the first result, in evaluation order, whose
verdict carries the decision *and* whose reason code the record actually cites — so the
finding the customer was told about is the finding the band is priced for.

| Finding | Cost | Why that one |
|---|---|---|
| another `violation` | 5 | a second broken rule is worse than one, but the first already set the band |
| an `unknown` | 10 | an unestablished fact is a real hole in the evidence |
| a `concern` | `5 × weight` | 10 for the adversarial tier, 5 for the ambient one — the same 2:1 the weights already use |
| `pass` / `not_applicable` | 0 | nothing happened |

**The cap** (`TAIL = 25`) is applied while the list is built, so a capped line is still
*listed*, at zero points. The customer can see that something was noticed and that it cost
nothing, which is a different statement from not showing it.

## Coverage

A score over two checks and a score over fifteen are not the same claim, and only one of them
deserves to be read as *"we looked"*. Every score therefore carries:

| Field | Meaning |
|---|---|
| `checks` | how many ran |
| `evaluated` | gave a verdict of their own |
| `not_applicable` | stood down because the customer stated no such rule |
| `failed` | raised, and defaulted (`engine_error_defaulted`) |
| `share` | `evaluated ÷ applicable`, or **null** when nothing applied |

`share` is over the checks that *applied*. A check standing down because the instruction never
mentioned returns is not a gap — it is the gating working, and counting it as one would punish
a narrow instruction for being narrow.

**Measured: `share` is 1.0 on all 45.** Every check that applies, runs, on every decision.
That is a property of the design — `evaluator.CHECKS` has no placeholders and the complete
evidence set *is* the audit trail — and it is worth stating plainly, because a coverage figure
is only impressive if it is ever anything other than 1.

**`not_assessed`.** A record with nothing evaluated has **no score**, not a low one. The
deadline guard's record is the case: it carries no check results at all, by design, because
none ran. A number there would read as a verdict on the purchase when the truth is that
nothing reached it.

## What the board looks like

| Score | Count | What it is |
|---|---|---|
| 100 | 17 | every approval — nothing was noticed on any of them |
| 60 | 8 | every step-up |
| 30 | 12 | a decline on one clear breach |
| 25 | 2 | a decline plus a second finding (`AU0010`, `AU0041`) |
| 20 | 2 | a decline plus an adversarial concern (`AU0037` injection, `AU0039` lookalike) |
| 15 | 2 | the SCEN0003 burst beginning (`AU0027`, `AU0028`) |
| 10 | 2 | the worst events on the board (`AU0029`, `AU0030`) — a breach plus three session signals |

**The step-ups are all 60, and that is the fixtures rather than the scale.** Every step-up on
this board carries exactly one finding: six sit precisely at the concern threshold and two are
a single `unknown`. A purchase carrying two adversarial concerns scores 50, which
`tests/unit/test_trust_score.py` demonstrates on a synthetic record rather than leaving as a
claim. Inventing differences between eight purchases that genuinely have one finding each
would be exactly the added judgement this spec forbids.

## Failure mode

`score()` wraps `_score()` and returns the `not_assessed` shape on any exception. It runs on
the audit path, where a serializer that throws is worse than no score: the decision is already
made and already correct, and losing the record of it to a presentation bug would be absurd.

Pure — no I/O, no clock, no globals.

## Open questions

- [ ] The bands are a presentation choice (75/35 and the 5-point gaps between them). They are
      not derived from anything, and nothing in the pack could derive them. If Viseca has a
      house scale for customer-facing confidence, use theirs — it is four constants.
- [ ] Should an approval that consumed most of a rolling window score below 100? Today the
      window is a `pass` and costs nothing, so a CHF 299.50-of-300 approval and a CHF 20 one
      look identical. Arguably "how much room is left" is a different number, not this one.
