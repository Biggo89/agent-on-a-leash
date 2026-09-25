# Spec: customer_message

**Status:** agreed   **Owner:** Nakya   **Reason codes:** none of its own — it renders others'

## Motivation

> The brief: the cardholder must be able to see "what was permitted, what evidence was used,
> and why". `decision-rules.md` §8 makes `customer_message` the **notification body**: the one
> field written for a person rather than for an auditor.

`decision-rules.md` §8 states six `DESIGN` rules for this field. Five were already met at the
end of Phase 4. The sixth — *"for `decline`, say what would make it acceptable if anything
would"* — was not met by a single one of the declines on the board. That gap, plus two
consistency defects found by dumping all 45 messages side by side, is what this spec closes.

This is Phase 5 work: no decision changes, no new checks. Only the text.

## The three defects this spec fixes

### 1. The message cited reasons the record did not

`combine()` returns violation codes only on a `decline`, but `explain()` joined the detail of
**every** violation, unknown *and* concern with semicolons, as co-equal causes. Seven of the
45 fixtures shipped a message that named a reason absent from `reason_codes`:

| Fixture | `reason_codes` | Also named in the message |
|---|---|---|
| `AU0007` | `period_limit_exceeded` | `unrequested_addon` |
| `AU0027`–`AU0028` | `merchant_not_permitted` | `device_novel`, `unusual_hour` |
| `AU0029`–`AU0030` | `merchant_not_permitted` | `device_novel`, `velocity_elevated`, `unusual_hour` |
| `AU0037` | `per_order_limit_exceeded` | `merchant_text_manipulation` |
| `AU0039` | `merchant_not_permitted` | `merchant_lookalike` |

The naive fix — drop every non-driving concern — is **wrong**, and the table shows why: the
last two rows are demo beat 4. `AU0037` is the fixture that proves an injection *did not work*
and `AU0039` is the lookalike. Silencing them to satisfy a consistency rule would delete the
two most valuable sentences in the build.

**Resolved: separate the two, rather than merging or dropping.** A driving reason and an
observation that changed nothing are different claims and are stated as different sentences.
See §2.

### 2. Ambient risk signals read as an accusation

`AU0027` said, of a CHF 232 order: *"You have not bought from RainThread before; this order
came from a device you have not used before; it was placed during the night."*

Only the first clause declined it. The other two are `velocity_elevated` and `unusual_hour` —
weight 1.0 signals that did not reach the threshold and did not contribute. Telling a customer
their purchase was refused and then listing the hour they shopped at is how a control layer
acquires a reputation for over-blocking that its own decision table does not deserve. It costs
nothing to omit and the audit record keeps every one of them.

### 3. The amount was stated three times

> *"Approved: CHF 44.50 at Alpine Basket. CHF 44.50 is within the CHF 120.00 per-order limit;
> CHF 44.50 of CHF 300.00 across 7 days; …"*

## Rule — the message has four parts, always in this order

| # | Part | When | Content |
|---|---|---|---|
| 1 | **Verdict** | always | decision, amount, merchant |
| 2 | **Cause** | always | the detail of exactly the results whose reason code is in `reason_codes` |
| 3 | **Next step** | `decline`, `step_up` | the cited checks' follow-ups (§4), then how to answer |
| 4 | **Also noticed** | `decline`, `step_up` | adversarial signals that did **not** drive it (§3) |

Part 2 is the invariant that fixes defect 1: **the customer message explains the reason codes
on the record, no more and no less.** Anything else on the message is in a sentence of its own
that says it changed nothing.

### 1. Verdict line

    Approved: CHF 391.50 (USD 450.00) at HarborByte.

Amount is always the CHF billing amount, because that is the figure every limit was compared
against. **When the order was not placed in CHF, the original follows in parentheses.** Four
fixtures are affected — `AU0025` EUR, `AU0029` GBP, `AU0032` EUR, `AU0038` USD — and `AU0038`
is the answer to *"doesn't it just block everything?"*: 450 USD looks like it breaches a CHF
400 cap and does not. A message that hides the conversion makes the strongest
non-over-blocking case in the pack look like an arithmetic error.

Merchant name is untrusted text and passes through `safe_display`. It always has.

### 2. Cause

Joined with `; `, capitalised, terminated. Selected by reason code, not by verdict:

```
codes  = record.reason_codes
cause  = [r.detail for r in results if r.reason_code in codes and r.detail]
```

The invariant is a **subset** rule, not equality: the cause never names anything that is not
in `reason_codes`. On `decline` it is the violations; on `step_up`, whichever set `combine()`
cited — the unknowns if any existed, otherwise the concerns that crossed the threshold.

On `approve` it is restricted further, to the cited results whose verdict is `pass`. A concern
can reach `reason_codes` on an approval (`combine()` appends the sub-threshold ones), and
naming it would put a warning on a purchase we did not think was worth stopping — friction
with no decision behind it, against §3 and against beat 2. No fixture on the board does this
today; the restriction is there so that the first one to do it behaves correctly.

### 3. "Also noticed" — non-driving concerns

A concern that did not drive the decision appears **only if it would have escalated on its
own**: `concern_weight(code) >= STEP_UP_THRESHOLD`. That is not a second hard-coded list; it
reads the same `CONCERN_WEIGHTS` table the decision uses, so a new weight is classified
correctly the day it is added, and `tests/unit/test_concern_config.py` already guards it.

The 2.0 tier is *evidence of an adversary* — manipulation, lookalike, an unfamiliar device, an
add-on nobody asked for, a duplicated order. The 1.0 tier is ambient — hour of day, velocity.
The customer needs to hear the first and does not need to be told the second.

Rendered as its own sentence, explicitly non-causal:

    Also noticed, though it did not change this decision: <detail>.

So `AU0037` keeps the injection sentence, `AU0039` keeps the lookalike, `AU0007` keeps the
cosmetics line, and `AU0027`–`AU0030` lose the hour and the velocity while keeping the device.

**Not shown on `approve`.** An approval that lectures the customer about what we noticed is
friction with no decision behind it, and beat 2 is *"ordinary shopping is not interrupted"*.

### 4. Follow-up — the check's own closing sentence

New field on `CheckResult`:

```python
follow_up: str = ""   # one closing sentence for the customer, after the cause. "" = none.
```

One field, two roles, and they are the same concept seen from the two sides of a verdict:

| Verdict | Role | Says |
|---|---|---|
| `violation` | **remedy** | what would have satisfied the rule that was broken |
| `concern` | **reassurance** | what the finding does *not* mean for this purchase |

Owned by the check that raised the result, because only that check knows its own threshold —
and rendered after the cause, for any **cited** result that supplies one. A follow-up on a
result that did *not* drive the decision is never rendered: the "also noticed" wrapper (§3) is
already the closing sentence for those.

The only reassurance on the board is `merchant_text_manipulation`, and it is required by
`check-manipulation-detected.md`: that message "has to do two things at once — say what
happened, and make clear the purchase itself was fine". So the check's `detail` shrinks to the
fact and its second half moves to `follow_up`. That is what stops it stuttering against the §3
wrapper on `AU0037` while keeping it on `AU0040`, where the finding is the driver.

**Remedies.**

**Phrasing rule, and it is load-bearing: a remedy says what would satisfy _the rule that was
broken_ — never that the purchase would have been approved.** Eleven other checks also ran. A
message promising approval on a hypothetical this engine did not evaluate is a claim we cannot
support, and the first judge to construct a counter-example is right.

    ✅ "An order of CHF 400.00 or less would be within your limit."
    ❌ "An order of CHF 400.00 or less would be approved."

| Reason code | Follow-up | Fixtures |
|---|---|---|
| `per_order_limit_exceeded` | `An order of CHF {limit} or less would be within your limit.` | AU0004, AU0010, AU0021, AU0034, AU0037, AU0041 |
| `period_limit_exceeded` | `CHF {limit − spent} of your CHF {limit} is still available in this {days}-day window, and it rises again as earlier orders age out.` | AU0007–AU0010 |
| `order_not_returnable`, `return_window_too_short` | `A seller offering {min_days} days or more would meet your terms.` | AU0014, AU0015 |
| `item_attribute_mismatch` | `An order in size {wanted} would match.` | AU0013 |
| `merchant_type_not_permitted` | `A {allowed} shop would meet your instruction.` | AU0022 |

The remaining violation codes deliberately supply **no** follow-up:

- `merchant_not_permitted` — nothing about *this* order fixes it, and a decline is final: there
  is no "approve this shop" path in the service contract. Inventing one would describe a
  product that does not exist.
- `item_not_requested`, `cart_contradicts_purpose` — "buy the thing you asked for instead" is
  not a remedy, it is a restatement.
- `unrequested_addon` — the remedy is the seller's to apply, not the customer's.

The period-limit follow-up is beat 3 told from the decline side: it is the only sentence in the
build that says the window *rolls*, which is exactly the mechanism a naive cumulative counter
would get wrong.

## Failure mode

Every part is optional and degrades to nothing. A decision with no details still yields
`"Declined: CHF 126.00 at Alpine Basket."` — the verdict line cannot fail, because it reads
only parsed event fields. `explain()` stays pure and total: it is called from inside
`evaluate()`, which must never raise.

## Fixtures

- **Exercised by:** all 45. `AU0002`, `AU0011` (beats 2–3), `AU0037`, `AU0039` (beat 4),
  `AU0016`, `AU0018`, `AU0026`, `AU0036`, `AU0040` (beat 5), `AU0038`, `AU0023`, `AU0042`
  (the over-blocking answer).
- **Must NOT change:** the decision on any of the 45. This spec touches text only —
  `make diff-decisions` must report no decision changed, and that is the acceptance test.

## Vectors

`tests/vectors/customer_message.yaml` — assembled records rather than single checks, since the
behaviour under spec is the *composition*. Covers: the four-part order; a non-CHF verdict line;
cause-equals-reason-codes on all three decisions; a 2.0 concern surviving into "also noticed"
on a decline; a 1.0 concern suppressed; no "also noticed" on an approve; every follow-up above;
and the no-detail degenerate case.

Two invariants are asserted over all 45 fixtures in `tests/unit/test_customer_message.py`
rather than as vectors, because they are properties of the whole board:

1. No message contains any string in `reason_codes` — the existing `decision-rules.md` §8 rule
   against leaking codes, previously asserted on one service response only.
2. No message contains a manipulation excerpt verbatim — untrusted text never round-trips to
   the customer, which the `merchant_text_manipulation` detail describes rather than quotes.

## Open questions

- [ ] A compliant SCEN0002 approval names five satisfied requirements and runs to ~40 words.
      Right for a demo and for the audit panel; long for a phone notification. If the frontend
      wants a short form, the split is `customer_message` (part 1) + `check_results` (the
      rest) — the data is already there and needs no engine change. Raise at UI sign-off.
- [ ] `step_up` says "Approve or decline in the app." The step-up surface is unowned
      (`GUIDELINES.md` §12) and if it lands as something other than an app, this string is
      wrong. One constant, `service/app.py` is not involved.
