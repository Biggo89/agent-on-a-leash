# Spec: merchant_lookalike

**Status:** agreed   **Owner:** Nakya   **Reason codes:** `merchant_lookalike`

## Motivation

SCEN0004 exercises *"a lookalike seller"*. The data dictionary warns:

> join `merchant_id` to `merchants.merchant_id` — on the identifier, never on `merchant_name`,
> because at least one pair of merchants has deliberately similar names.

That pair is **`ME0022 PixelHarbor`** (Zurich, electronics, 6 prior approvals on card `CA0039`)
and **`ME0059 PixelHarbour`** (Zurich, electronics, **0** prior). One letter apart. A control
layer that matches merchants by name treats the impostor as familiar and waves it through.

Unlike `merchant_permitted`, this is **not** a stated customer requirement — the cardholder
never wrote "avoid lookalikes". It is an inferred risk signal, so it is a `concern`, not a
`violation`.

## Inputs

| Field | Type | Null semantics |
|---|---|---|
| `enrichment.merchant_lookalike_of` | `str \| None` | `None` ⇒ no confusable known merchant found |
| `enrichment.merchant_lookalike_name` | `str \| None` | Name of the impersonated merchant, for the customer message |
| `authorization.merchant.merchant_name` | `str` | **Untrusted** merchant-supplied text |
| `authorization.merchant.merchant_id` | `str` | The trusted identity |

Enrichment (`adapters/parse.py`) sets `merchant_lookalike_of` only when **all three** hold:

1. this card has **zero** approved history at this `merchant_id`;
2. some merchant the card **has** used has a normalized name within Levenshtein distance ≤ 2;
3. that merchant's `merchant_id` differs from this one.

Normalization folds case, accents and punctuation before comparing, so `Pixel-Harbour` and
`pixelharbor` collapse together.

The name is untrusted input, but comparing it is a permitted operation: we extract the *fact*
"this name resembles one you trust" and never act on anything the name asks for. Consistent
with `decision-rules.md` §3.

## Rule

| Condition | Verdict | Reason code | Evidence |
|---|---|---|---|
| `merchant_lookalike_of` is `None` | `pass`, **no reason code** | — | — |
| `merchant_lookalike_of` is set | `concern` (weight **2.0**) | `merchant_lookalike` | `merchant_id`, `merchant_name`, `resembles_merchant_id`, `resembles_merchant_name`, prior count |

A clean merchant emits `pass` with no reason code so it adds nothing to an approval's
`reason_codes`; the audit trail still records that the check ran, via `check_results`.

**Weight 2.0 equals `STEP_UP_THRESHOLD`**, so a lookalike **alone** escalates to `step_up`.
That is deliberate: asking *"this seller's name closely resembles PixelHarbor, which you have
used — did you mean this one?"* is a useful intervention, whereas a flat decline on a name
similarity would be over-blocking if the customer genuinely chose a new shop.

The check is never gated on a facet: impersonation is worth flagging whatever the instruction
said.

### Showing the name safely

The customer cannot spot a lookalike without seeing both names, so the message names them:

> *"the seller \"PixelHarbour\" is not one you have used — its name closely resembles
> \"PixelHarbor\", which you have"*

Both are untrusted merchant text, so both are rendered through `sanitize.safe_display()`,
which strips control characters and newlines and caps the length. A merchant cannot use its
own name to inject a line into a customer-facing alert.

## Failure mode

Distance ≤ 2 on short names could in principle collide. Swept empirically: across all 1,653
merchant pairs in the pack, **exactly one** falls within distance 2 — the intended
`ME0022`/`ME0059` pair. Locked by
`tests/unit/test_data_pack_invariants.py::test_lookalike_merchant_pair_exists`. Raising the
threshold above 2 would need a fresh sweep.

## Fixtures

- **Exercised by:** `AU0039` (PixelHarbour ~ PixelHarbor, distance 1) — the only attempt in
  the pack that triggers it.
- **Must NOT change:** `AU0044` (Circuit and Pine — genuinely different name, unfamiliar but
  not confusable), `AU0029` (Thames Weave vs the card's familiar Milano Weave — distance 6),
  and every attempt at a familiar merchant.

`AU0039` breaches SCEN0004's familiarity requirement as well, so `merchant_permitted`
declines it and the decline outranks this concern. The lookalike detail still reaches the
customer message and the audit record — which is what makes it demonstrable.

## Vectors

`tests/vectors/merchant_lookalike.yaml`, plus the name-similarity cases already in
`untrusted_text.yaml`.

## Open questions

- [ ] Should `merchant_lookalike` appear in `reason_codes` when the decision is a decline
      driven by another check? Today `combine()` returns only violation codes on a decline, so
      it does not — the signal survives in `evidence`, `customer_message` and `check_results`.
      Changing that is a change to the combination logic and needs its own decision.
