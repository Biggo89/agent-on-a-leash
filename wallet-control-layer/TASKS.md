# Objectives and tasks — Nakya (technical implementation lead)

> **Ownership caveat.** The frontend/backend split across the three developers is **not yet
> agreed** (`GUIDELINES.md` §12). This breakdown assumes Nakya owns the **decision engine and
> the sandbox integration**. Settle the boundary in the first 30 minutes and adjust — an
> unowned step-up surface at hour 30 is the classic way this challenge goes wrong.

---

## The objectives, in priority order

**O1 — A decision path that works end to end.** `authorization.request` in, an explained
`approve`/`decline`/`step_up` out, inside the 8-second deadline, submitted to the sandbox.
Without this nothing else counts. *Success: SCEN0000 approves cleanly against the live API.*

**O2 — Enforce the whole instruction, not just the amount.** All five instructions carry
requirements beyond a price cap: merchant familiarity, retailer type, item identity and
attributes, return terms, no unrequested additions. *Success: each requirement is a named
check with its own reason code and vectors.*

**O3 — Correct state over time.** A rolling window over final approvals, idempotency under
at-least-once delivery, duplicates distinguished from legitimate re-quotes.
*Success: AU0011 approves at 223/300; a redelivered authorization returns the identical
decision; AU0042 is not penalised for retrying.*

**O4 — Hold up under manipulation.** Merchant text never changes a limit or a verdict.
*Success: the injection invariant test passes across all 45 fixtures, and the lookalike
merchant is caught by id-and-history, not by name.*

**O5 — Explain every decision.** Reason codes, evidence, and a plain-language customer
message. *Success: a judge reads one audit record and can say what we permitted, what
evidence we used, and why.*

**O6 — Don't over-block.** *Success: AU0038 (450 USD, US seller, 21 prior approvals) and
AU0023 (unfamiliar but fully compliant) both approve, and the session relaxes after the
SCEN0003 burst ends.*

---

## Task breakdown

Status: `✅ done` (already built and tested) · `⬜ hackathon work`

### Phase 0 — Before the event (done)

| | Task |
|---|---|
| ✅ | Read the brief, technical contract, data dictionary, all three JSON schemas |
| ✅ | Analyse all 45 fixtures: familiarity, device novelty, lookalikes, injections, FX, windows |
| ✅ | `GUIDELINES.md`, `AGENTS.md`, `CLAUDE.md`, normative specs |
| ✅ | Offline sandbox replica — full protocol, all 45 events schema-valid |
| ✅ | Money (integer centimes), rolling-window ledger, untrusted-text handling, history index |
| ✅ | Deterministic instruction compiler (the no-model fallback) |
| ✅ | Test kit: 176 tests, language-neutral vectors, injection invariant, protocol suite |
| ✅ | Replay/demo harness with regression diffing |

### Phase 1 — Hours 0–2: align and connect

| | Task | Why it's first |
|---|---|---|
| ⬜ | **Get the judging criteria from Viseca.** They are not in the repo (`technical_details.md:25` links a missing file). | Drives every prioritisation call below |
| ⬜ | Agree the ownership split (`GUIDELINES.md` §12 checklist) | Parallel work without it produces gaps or collisions |
| ⬜ | Team key → `.env`; `LEASH_BASE_URL` → the real sandbox; `make replay-live --scenario SCEN0000` | Proves the whole path on real infrastructure |
| ⬜ | Diff live event shapes against the replica; fix any `REPLICA-ASSUMPTION` that was wrong | The replica is a model, not the truth |
| ⬜ | Confirm: private scenarios at judging? reset protocol? step-up window? | Changes how much generalisation matters |

### Phase 2 — Hours 2–12: the checks (O2, O3, O4)

One spec + vectors + implementation each. **Spec first, every time.**

| | Check | Notes |
|---|---|---|
| ✅ | `merchant_permitted` | Done — gated on the `merchant_familiarity` facet, so SCEN0002 is `not_applicable` and AU0023 stays approved. Spec: `specs/check-merchant-permitted.md` |
| ✅ | `merchant_lookalike` | Done — concern at weight 2.0, names both shops in the message. Spec: `specs/check-merchant-lookalike.md` |
| ✅ | `merchant_type` | Done — matches `merchant_category`, carries MCC as evidence. AU0022 declines, AU0023 holds. Spec: `specs/check-merchant-type.md` |
| ✅ | `item_matches_request` | Done — one matching line is enough, so add-on carts stay this check's business no further. Spec: `specs/check-item-matches-request.md` |
| ✅ | `item_attributes` | Done — scoped to identity-matching lines via a shared matcher. AU0013 declines. Spec: `specs/check-item-attributes.md` |
| ✅ | `order_terms` | Done — trusted field decides *whether*, untrusted text supplies *how long*. AU0016 is the pack's first step_up. Spec: `specs/check-order-terms.md` |
| ✅ | `unrequested_addon` | Done — violation when the rule is stated (SCEN0004), concern→step_up when the scope is only implied (AU0018). Spec: `specs/check-unrequested-addon.md` |
| ✅ | `duplicate_order` | Done — concern→step_up on AU0036; AU0042 earns an explicit `legitimate_requote` pass. Spec: `specs/check-duplicate-order.md` |
| ✅ | `session_integrity` | Done — composite score, no sticky state so recovery is automatic. AU0026 escalates. Spec: `specs/check-session-integrity.md` |
| ✅ | `manipulation_detected` | Done — concern at weight 2.0, so AU0040 escalates. Lever is `MANIPULATION_WEIGHT`. Spec: `specs/check-manipulation-detected.md` |
| ✅ | Tune weights + `STEP_UP_THRESHOLD` | Done — `make tune` / `tune-sweep` / `tune-weights`. Removed 6 dead weights, unified the two scoring layers, accumulation is now real. Board unchanged. |

### Phase 3 — Hours 12–20: integration (O1, O5) — **built, needs the live API**

Built and tested against the replica; **405 tests green**, `make diff-decisions` reports
**no decision changed**. Reviewed 2026-09-07 — one defect found and fixed (below). What
remains is pointing it at the real sandbox on the day.

| | Task |
|---|---|
| ✅ | Run loop against the live API — `runtime/runner.py`. Long-poll, decide, submit; redelivery replays the stored decision with `idempotent_replay` and never re-evaluates; 409 and 408 are recorded, not fought; `resume()` rebuilds the decided set *and the ledger* from `/v1/authorizations` and re-reads the event cursor. A cross-run dispatch keeps the team-global queue from mixing two runs' windows. **Review fix:** an authorization for a run this process does not know is no longer judged with *this* run's `intent_facets` — it routes to the event's own `uncertainty_policy` and is audited as `unowned`. See `specs/service-contract.md` §7. |
| ✅ | Deadline guard — `domain/deadline.py`, spec `specs/deadline-guard.md`, 9 vectors. `RESERVE_MS = 1500`; under it the guard emits the `uncertainty_policy` outcome with `deadline_risk` and **no check results**, because none ran. The one place the real clock changes an outcome. |
| ✅ | Audit trail — `audit/log.py`, spec `specs/audit-record.md`. Append-only JSONL; a resolution and a redelivery each append their *own* line, so nothing is ever rewritten. `GET /v1/audit/{id}` folds them on read. A malformed line is skipped and counted; a write failure never reaches the decision path. |
| ✅ | Decision service — `service/app.py`, contract in **`specs/service-contract.md`**. `POST /v1/decide` (what-if, submits nothing), `GET /v1/audit/{id}`, run control, config. **⬜ Still needs the frontend devs' sign-off**, but it is no longer unexercised: `../leash-demo` is a working client written against this contract, and its three open questions now have a worked answer rather than a preference — polling over SSE, `open_questions` shown at confirmation as a warning that does not block, one run at a time. Take those to the conversation as a proposal. |
| ✅ | Step-up surface — `GET /v1/step-ups` is the notification list with a live countdown; `POST /v1/step-ups/{id}/resolve` calls the sandbox's `/resolve`, moves the amount into approved spend *at resolution*, and appends a `resolution` record. |
| ✅ | Mandate lifecycle — compile (review screen, nothing saved) → create → confirm → **tighten** → **revoke**, all through the service. Loosening is refused with `422 policy_loosened`; a revoked mandate makes the platform refuse the run. |
| ⬜ | **Point it at the real sandbox.** `make probe-live` checks every replica assumption against the real API and prints what would need changing. Then `make serve-live`. |
| ✅ | Postman collection — 29 requests in 7 narrative folders, valid v2.1, key never baked into the shared file (tested). `postman/README.md` is written for a non-developer. |
| ⬜ | Walk one teammate through the Postman README end to end and fix whatever they trip on. Nothing substitutes for watching someone else use it. |

### Phase 4 — Hours 20–28: LLM compiler (O2) — **built, needs a key**

Built and tested with **no API key present**, which is the point: the fallback path is the one
every `make check` exercises. **497 tests green**, `make diff-decisions` reports **no decision
changed**. Spec: **`specs/llm-compiler.md`**.

| | Task |
|---|---|
| ✅ | LLM compiler producing the full Policy IR — `compile/llm.py`, **OpenRouter** (`anthropic/claude-opus-5` by default; compile time only, so there is no deadline here and a human reads the output). Plain `httpx`, **no new dependency**, and swapping model or provider is one env var. Structured outputs pin the shape; `compile/contract.py` then re-checks every field against what the checks actually read. **Provenance must be a verbatim span of the instruction** — a paraphrase costs the model the rule, which is a mechanical anti-fabrication guarantee rather than a prompt instruction. `compiler_notes` records every guard action, so the review screen shows what the model proposed *and* what we refused. |
| ✅ | Hard fallback to `baseline.py` — **two tiers**. OpenRouter first routes past a dead or rate-limited primary to `LEASH_COMPILER_FALLBACK_MODELS` (free redundancy); only then do we degrade. Degradation is **total, not partial**: no key, timeout, 4xx/5xx (including **402, out of credit**), a 200 with an error envelope, a truncated response, HTML from a captive portal, or nothing surviving the guard all return the baseline IR whole. 34 tests, all with no key. One of them imports the decision path in a subprocess and fails if the SDK is even *loaded* — AGENTS.md §3.1 made mechanical. |
| ✅ | `confidence: low` ⇒ `open_question` — resolved **asymmetrically**, and this is a decision worth defending in the demo: a low-confidence *facet* is not enforced (a facet restricts, so enforcing a guess over-blocks — AU0023 is exactly that case), while a low-confidence *cap* is enforced and asked about (a cap permits, so dropping a guess loosens the mandate). *Uncertainty never tightens the intent and never loosens the money.* |
| ✅ | Instruction round-trips byte-identical — `instruction_sha256` taken at compile time, verified before submit (catches a UI that edited the IR it was handed) **and** against what the platform echoes back (`409 instruction_not_echoed`, raised at creation rather than surfacing later as a run that refuses to start). |
| ✅ | Six invented instructions — `tests/fixtures/invented_instructions.yaml`, `make compile-invented`. Safety expectations run offline on every check; model expectations skip without a key. They found two real baseline gaps (below). |
| ✅ | **Safety floor.** Every cap the regex found must be covered by an accepted cap no looser, one for one — matched on *value*, never scope, because scope is the judgement the regex is bad at. So the model may re-scope or tighten a limit, never lose or widen one, without trusting the model at all. |
| ✅ | **Live run done, 2026-09-08.** `make compile-live` (new — `LEASH_LIVE_COMPILER=1` lifts conftest's baseline pin for the one suite that is about the model) plus `make compile-llm` and `make compile-invented`. 44 of 45 live assertions pass; the one failure is a **fixture** question, not a compiler defect — see below. `make check` 754 green, `make diff-decisions` unmoved. `tools/compile.py` now loads `.env` like `serve.py` and `probe.py` did — it never had, so `make compile-llm` had been silently compiling with the baseline. |
| ✅ | **Model chosen by measurement: `google/gemini-3.7-flash`.** Scored on which intent facets each model recovers from the five pack instructions, against what the board needs: gemini-3.7-flash **0 missed / 0 invented**, $0.043, 58 s · claude-opus-5 0/1, $0.209 · claude-sonnet-5 1/2, $0.098 (read *"27-inch"* as a clothing size — would have over-blocked SCEN0004) · gemini-3.5-flash-lite 2/1, $0.009 (lost `merchant_type` on SCEN0002 — the demo scenario). The cheap tier drops facets the board depends on and the expensive tier is not more accurate, so the middle is both best and nearly cheapest. |
| ✅ | **Two live-only defects found and fixed.** (1) Strict structured outputs require *every* schema property in `required`, so a model saying "not stated" can only send `null` — the guard read that as an unrecognised requirement and produced a customer-facing question containing the literal word `'None'`. Nulls are now silence. (2) `max_tokens` was 8 000, which a reasoning model spends entirely on thinking: `qwen/qwen3.8-flash` returned **zero characters**, `google/gemini-3.8-flash` truncated mid-JSON. Now 32 000 — free, since OpenRouter bills tokens generated, not allowed. Both failures fell back to the baseline correctly, which is the design working, but a compiler that never compiles is no use. |
| ✅ | Fixture question settled — `merchant_type` on *"only from a bookshop I have used before"* is a faithful reading, not an invented restriction. Nakya's call, 2026-09-08. **All 45 live assertions now pass.** |
| ⬜ | Watch the OpenRouter credit balance. An exhausted account is an HTTP 402 and degrades to the regex baseline **silently** unless someone reads `compiler_notes` — which is correct behaviour and a bad surprise on stage. |
| ✅ | **Demo documented, 2026-09-09.** `PLAYBOOK.md` Part 3b is the full runbook (side-by-side, review screen, the board-does-not-move beat, the pull-the-plug fallback, generalisation, the four anti-fabrication answers, and cost/timing). `DEMO.md` beat 1b is the stage version. `README.md` has a "Using the LLM compiler" section. New `make replay-llm` compiles all five mandates with the model and replays all 45 fixtures — **verified twice, no decisions changed both times.** |
| ⬜ | Decide whether the demo compiles live or from a cached IR. Live is the better story; cached cannot fail on stage. Recommend: live, with `make compile` (baseline) as the visible fallback if the Wi-Fi dies — which *is* the fallback story. |

**The fixture question, settled 2026-09-08.**
`tests/fixtures/invented_instructions.yaml::books_familiar_no_additions` expected
`no_facets_beyond: [item_identity, merchant_familiarity, no_additions]`, and both
`gemini-3.7-flash` and `gemini-3.5-flash-lite` independently added a fourth: `merchant_type:
books`, provenance `"bookshop"`, confidence `high`. The test failed as *invented restriction*;
it was reported rather than relaxed (`AGENTS.md` §1) and **Nakya allowed it**.

The reasoning: *"only from a bookshop I have used before"* states two properties and `only`
governs both, so reading `"bookshop"` as a shop kind is faithful — SCEN0002's *"only from a
specialist sports retailer"* compiles the same way and nobody calls that over-blocking. The
expectation had been written with no model available, and it was the thing that was wrong.

`merchant_type` is **permitted, not required**: it is in `no_facets_beyond` but deliberately
not in `with_model.facets`, because the opposite reading is defensible too and requiring it
would make the suite fail on a model that takes it.

**Three baseline defects the invented instructions exposed** (all pre-existing, none moves the
board — no pack instruction uses these phrasings):

1. ✅ `"never more than CHF 75 on a single order"` — `_PER_ORDER` matched only *"each order"*
   and *"per order"*, so the cap was scoped as a **period** total and then lost to the looser
   period cap entirely. **Under**-blocking, not the over-blocking recorded here originally —
   see Phase 5 defect 2. **Fixed in Phase 5.**
2. ⬜ `"the pet shop we normally use"` — `_FAMILIAR` matches only *"shops I have used before"*,
   so the familiarity requirement is dropped silently and the constraint weakens to "any pet
   shop" (the `merchant_type` facet still fires, so it is pet shops — just not *the* one).
   Under-blocking. **Still open — and deliberately so; see below.**
3. ✅ `"If you are not sure, decline"` — read as `ask`. Under-blocking. **Fixed 2026-09-09.**

**Re-examined 2026-09-09. Neither was the one-line widening this file used to claim.**

**Gap 3 was in the wrong place, and is now fixed.** `uncertainty_policy` never read the regex:
`baseline.py` had `"ask" if _UNCERTAIN_ASK.search(instruction) else "ask"` — **both branches
identical**, so the pattern was dead and widening it could not have changed any output. It had
been that way since the scaffold commit (`ad000d7`), and every existing test passed throughout,
because all five pack instructions end *"Ask me when uncertain"* and the constant was therefore
right for the pack by luck.

**Fixed 2026-09-09.** `_UNCERTAIN_DECLINE` matches the stated strict policy in either clause
order, requiring the conditional so a bare verb (*"Decline gift wrapping"*) is never read as
one and neither alternative crosses a sentence boundary; the ternary is now load-bearing.
**`approve` is deliberately unreachable from a cue list** — a missed strict cue costs a
question, a wrongly-matched loose cue spends money on a guess. The semantics are now specified
rather than implied: **`specs/policy-ir.md` compiler rule 6**. 19 new tests in
`tests/unit/test_uncertainty_policy.py`, and the invented-instruction expectation moved from
`with_model` to `always`, where the suite can see it. **776 green, board unmoved** — the five
pack instructions still compile to `ask`, so no decision could change.

**Gap 2 costs the demo its best argument.** The phrase the regex misses in the *pack* is
SCEN0000's *"a shop I use regularly"* — the same habitual-present construction as *"the pet
shop we normally use"*, and any widening broad enough for one catches the other.
[`DEMO.md`](DEMO.md) beat 1b is built on exactly that miss: *"the regex finds nothing… the
model produces `merchant_familiarity`, quoting that span. That is the argument for the model,
on a pack instruction, not a contrived one."* `invented_instructions.yaml:55` says the same —
*"the clearest single case for the model."* Close this gap and both sentences stop being true.
**Recommended: leave it open through the demo**, and say so out loud — a known, documented,
one-line-fixable regex gap is a better answer to "why the model?" than a compiler that quietly
handles the case. Fix it after the demo, or never.

### Phase 5 — Hours 28–33: demo (O5, O6) — **done**

Runbooks: **[`PLAYBOOK.md`](PLAYBOOK.md)** — every command with its verified output, the
one-time setup, the reset, going live, and the failure table · **[`DEMO.md`](DEMO.md)** — the
beats, the evidence, and the over-blocking answer. **776 tests green, 29 skipped** (the
skips are the model suites, which need a key), `make diff-decisions` reports **no decision
changed** — re-verified 2026-09-09 after the gap-3 fix.

> **Figures below are as of this phase.** The board was 17 / 23 / 5 here; Phase 5c moved it to
> **17 / 20 / 8**. `DEMO.md` and `README.md` always carry the current numbers — this section
> is the record of what was true when the work landed, and is left as written.

| | Task |
|---|---|
| ✅ | Polish `customer_message` for the five demo beats. Spec: **`specs/customer-message.md`**. The field now has four parts — verdict, cause, next step, and anything adversarial that did *not* change the outcome — and the cause names **exactly the reason codes on the record**, which seven fixtures previously violated. Declines carry a remedy (the sixth `decision-rules.md` §8 rule, met by none of the 23 declines before this); non-CHF orders show the original (`CHF 391.50 (USD 450.00)`, which is the AU0038 answer); ambient risk signals no longer appear under a decline they did not cause. 207 new tests: 16 composition vectors, 4 check-level `expect_follow_up` cases, and a board pass in `tests/unit/test_customer_message.py` that pins the eight demo messages verbatim and asserts no message leaks a reason code or quotes an injection back. |
| ✅ | Rehearse the demo end to end, twice, **offline**. Run with outbound HTTP blocked at the process level (`HTTPS_PROXY=http://127.0.0.1:1`, `NO_PROXY=127.0.0.1`) so an accidental off-machine call fails loudly instead of quietly working on venue Wi-Fi. All five beats, including the full mandate lifecycle and the step-up resolve. 37 s of machine time. **Rehearsal finding:** revoke is terminal, so a tighten demo must come *before* it — afterwards every PATCH is `409 not_active`. |
| ✅ | Prepare the over-blocking answer — [`DEMO.md`](DEMO.md#doesnt-it-just-block-everything). Led by the structural fact, which is stronger than the four fixtures: **all 23 declines are violations of something the cardholder wrote; inferred risk signals produced 0 declines and all 5 step-ups.** Then AU0038 (450 USD → CHF 391.50), AU0023 (Summit Thread, **zero** prior approvals on this card, fully compliant), AU0042 (`legitimate_requote`), AU0032 (EUR 260 → CHF 247), and the SCEN0003 recovery at AU0031 — the very next event after the burst. |
| ✅ | Save the final regression baseline; `make check` green. |
| ✅ | **`make demo` prints the customer message under every decision** (`--demo` implies `--verbose`). Demo-flow change under AGENTS.md §1, **confirmed by Nakya 2026-09-08**. |

**Three defects found while rehearsing beat 5. All three fixed, go-ahead given 2026-09-08 —
754 tests green, `make diff-decisions` reports no decision changed. What they were:**

1. ✅ **There was no way to lower a cap.** `PATCH /v1/mandates/{id}` requires `hard_rules` to be
   preserved and only extended, so `400 → 250` is `422 rules_not_preserved`. Adding a second
   `purchase`-scope cap at 250 does not help either: `_threshold()` in
   `domain/checks/limits.py` returns the **first** matching rule, not the tightest, so the 400
   still wins. Net: "tighten" works only for adding a cap in a scope that had none.
   **Fixed.** `domain/policy.py::binding_cap` returns the tightest matching rule; ties on value
   break toward `<`. Spec: **`decision-rules.md` §9**, which also settles the period case (one
   rolling window exists, so different windows are incomparable and the shortest binds — the
   open question there is enforcing all of them). `period_window_days()` wraps the same
   selection, so the window enrichment computes and the cap the check reads can no longer
   disagree; the five sites that each found "the first period rule" independently now share it.
   13 vectors in `tests/vectors/binding_cap.yaml`, plus `tests/unit/test_tighten.py`.
   Verified end to end: a mandate patched to `[400, 250]` declines AU0038 at CHF 391.50 against
   **250**, remedy included. This also repairs a claim `llm-compiler.md` was already making —
   "the evaluator would enforce the tighter rule regardless" was false while the first match won.

2. ✅ **Phase 4 defect 1 was recorded with the wrong sign, and the wrong sign is the dangerous
   one.** TASKS.md says `"never more than CHF 75 on a single order"` would "cap a fortnight of
   spending at CHF 75 — over-blocking". Because of the first-match behaviour above, what
   actually happens is that the CHF 75 cap is **dropped entirely** and only the CHF 250
   fortnight cap is enforced: a CHF 200 single order is **approved** on an instruction that
   says never more than 75. That is under-blocking — a stated cap silently lost. Verified on
   `tests/fixtures/invented_instructions.yaml::two_caps_reversed_and_decline`, the one invented
   instruction that already compiles to two same-scope caps.
   **Fixed.** `_PER_ORDER` now matches `(each|every|per|one|(a|any) single) (order|purchase|
   transaction)`. Every alternative needs a quantifier, so a bare noun never matches and
   SCEN0000's *"one ordinary grocery item"* still does not read as *"one order"*. Exactly one
   instruction in the pack or the invented set changes — the one with the gap — so the board
   cannot move. The record is corrected in the fixture's `baseline_gap` and in
   `llm-compiler.md`, which used this sentence as its worked example of why the safety floor
   matches on value and never on scope. The point survives: a cue list can always be widened
   and never made complete.

3. ✅ **A malformed `PATCH` reported success.** `PatchMandateRequest` has no `instruction`
   field and Pydantic ignores unknown keys, so `PATCH {"instruction": …}` returned `200` and
   changed nothing — a frontend patching the create-shape would believe its tightening worked.
   **Fixed:** `extra="forbid"`, the one endpoint where unknown keys are refused rather than
   ignored, because every field on it is optional and an unrecognised body degrades to an
   empty patch rather than to a partial one. `service-contract.md` §2 now documents that, and
   that lowering a cap means sending both rules rather than replacing one.

### Phase 5b — The two front-ends (added 2026-09-09)

Neither was mentioned anywhere in this repo until now. Both are **separate git repositories**,
so nothing here depends on them and they can be reset without touching the engine.

| | Surface | What it is |
|---|---|---|
| ✅ | **`../playground`** (`96182f7`) | The *explainer*. Two views — "How it works" for non-developers, "Engineering" for the codebase — sharing a Decision Explorer over all 45 fixtures. `assets/data.js` is generated by its own `tools/generate_data.py`, which drives this engine offline. Re-verified 2026-09-09: **all 45 ids match `out/baseline.json`** on decision, reason codes and billing amount. |
| ✅ | **`../leash-demo`** (`13beaf6`) | The *pitch*. A three-column deck — Agent · The Leash · You — that plays the agent lifecycle: the instruction and the spans that became rules, the agent shopping, twelve checks resolving, the verdict, then a step-up answered on a phone with a live 120-second window, a mid-run tighten, and revoke. Written against `specs/service-contract.md`; `node check.mjs` is 24 assertions that it still matches this engine. |

| | Task |
|---|---|
| ⬜ | **The decision data is duplicated and nothing keeps it in step.** `playground/assets/data.js` and `leash-demo/src/data/engine-output.js` are two copies of the same generated table. `leash-demo/check.mjs` catches its own copy going stale; the playground has no equivalent guard. After any rule change, regenerate **both**. |
| ⬜ | Decide which surface leads the pitch. They answer different questions — *how does it work* versus *watch it work* — and showing both costs time that `DEMO.md` has not budgeted. |

---

### Phase 5c — Two coverage gaps closed (added 2026-09-22)

Found by sweeping the pack for patterns no check watches, rather than by a failing fixture:
both were decisions the board got wrong while every test was green. **997 tests green, 29
skipped.** Board moved **17 / 23 / 5 → 17 / 20 / 8** — six decisions of the 45 re-examined,
four of them moving *away* from a refusal.

| | Task |
|---|---|
| ✅ | **`check_split_order`** — spec: **[`specs/check-split-order.md`](specs/check-split-order.md)**. A per-order cap constrains one order, so `AU0005` (CHF 70.00, 17:20) and `AU0006` (CHF 65.00, 17:26) at the same merchant cleared a CHF 120 cap twice and spent CHF 135. `check_duplicate_order` deliberately stays out of it (identical carts only) and deferred it to the rolling limit — which under-covers, because only SCEN0001 states a period rule. Concern at weight 2.0, so it asks rather than refuses; stands down when the order is already a duplicate, so `AU0035`/`AU0036` keeps its better message. **Three decisions moved:** `AU0006` approve→step_up, and through the ledger `AU0007` decline→step_up (the cosmetics line it was always carrying, no longer masked by an inflated window) and `AU0008` decline→**approve**, which was the only ordinary purchase the board was refusing. Swept all 45 for same-merchant pairs inside 60 minutes: exactly three exist, and the other two are inert. 10 vectors in `tests/vectors/split_order.yaml`. |
| ✅ | **Familiarity reads the person, not the card** — spec: **[`specs/check-merchant-permitted.md`](specs/check-merchant-permitted.md) §"Card or person"**. `AU0044`: `CA0039` has zero approvals at `ME0023`, the same cardholder's `CA0038` has two, and 17 of the 20 pack customers hold more than one card. *"A seller I have bought from before"* is a statement about the person, so the two counts disagreeing is an `unknown`, not a breach — never a `pass`, because reading the person's history as permission would widen a rule the customer stated. `AU0033` is the control and stays a decline: neither of that customer's cards has used the shop. `authorization_history.csv` carries `customer_id`, so both counters come out of one pass and no call site changed. **One decision moved:** `AU0044` decline→step_up. 5 new vectors. |
| ✅ | Front-ends regenerated from the new baseline — `playground/assets/data.js` and `leash-demo/src/data/engine-output.js`, plus the board assertion in `leash-demo/check.mjs`. |

### Phase 5d — Merchant text in four languages (added 2026-09-22)

| | Task |
|---|---|
| ✅ | **DE / FR / IT injection patterns**, closing the open question in [`specs/check-manipulation-detected.md`](specs/check-manipulation-detected.md). 37 patterns against the **same ten labels** — the vocabulary is closed, so `decision-rules.md` §6, the weights and every message are untouched. Accents matched both ways rather than folded, because `Manipulation.span` indexes the original text and the demo highlights inside the seller's own copy. **No decision moved:** the pack is written in English, which is exactly why this could land late. |
| ✅ | **A benign corpus, which is the actual deliverable.** `tests/vectors/injection_corpus.yaml` — 13 attacks and **15 pieces of ordinary merchant copy**, both halves in all four languages. We can now state **0 false positives, 0 missed attacks** instead of hoping for it. Five benign cases are deliberate near misses and each one narrowed a pattern: *"ne pas ignorer les instructions de lavage"*, *"Pflegehinweise nicht missachten"*, a promotion capping quantity per order, an order confirmation by e-mail, and a courier who will not ask for a signature. Two meta-tests keep the corpus from rotting: one fails when a language has attacks but no benign copy, the other when a label is reachable in only one language. |

### Phase 5e — Failure is not uncertainty (added 2026-09-22)

| | Task |
|---|---|
| ✅ | **A crashed check and a spent deadline can no longer approve.** Both became `unknown` and were routed through `uncertainty_policy` like any other, so under `approve` the engine paid for a purchase on which **nothing was evaluated** — and told the customer their rules had been applied. `domain/types.py::resolve_uncertainty` is now the one resolver both `evaluator.combine` and the deadline guard read, and it floors `types.NOT_EVALUATED` (`engine_error_defaulted`, `deadline_risk`) at `step_up`. `decline` is deliberately untouched: a customer who asked for refusals when unsure is not made safer by being asked instead. **No decision moved** — no check raises on the 45, the guard trips on none of them, and all five pack mandates say `ask`. The compiler will not produce `approve` from an instruction at all, so the path is only reachable through a customer setting, which is exactly where a hole hides. Five tests, including one that swaps a raising check into `CHECKS` and asserts the decision rather than the exception. `specs/deadline-guard.md` records the old mapping and why it was wrong. |

### Phase 5f — "No hardcoding" becomes a test (added 2026-09-22)

| | Task |
|---|---|
| ✅ | **`tests/unit/test_no_hardcoding.py`.** The rule was a claim in `CLAUDE.md`, `decision-rules.md` §187 and `tools/tune.py`, enforced by authorial discipline. It now replays all 45 with every identifier replaced by a consistent bijection under two fixed seeds and requires identical decisions, reason codes and messages — facts untouched, because scrambling those would prove only that different inputs give different answers. The bijection matters: `related_authorization_id` links AU0042 to the declined AU0037, and a fresh id per event would break a legitimate re-quote and fail for the wrong reason. A second test walks the AST of `src/` and fails on a fixture id used as a value outside a docstring, with a guard against the scan silently matching nothing. **Verified to have teeth** by injecting `if ev.authorization_id.startswith(...)` into a check: the scramble flagged seven authorizations (the cascade through the ledger) and the AST scan flagged the literal. |
| ✅ | `tests/board.py` — the 45-fixture replay, previously private to `test_customer_message.py`, now shared with the scramble test so the two can never disagree about what the board is. The existing pin against `out/baseline.json` still guards the harness. |

### Phase 5g — What it buys over a spending limit (added 2026-09-22)

| | Task |
|---|---|
| ✅ | **`tools/impact.py` · `make impact`.** The question a card issuer actually asks, answered on the organizers' 45 with **no figure of ours in it**. The pack ships no interchange rate, no dispute probability and no cost of a blocked purchase, so this counts decisions and Swiss francs and nothing else — a model built on numbers we invented would be a weaker claim wearing a stronger costume, and `tests/unit/test_impact.py` fails if the tool ever grows one (AST scan for float literals and the forbidden terms, docstrings excluded so the module can still explain what it omits). Four regimes differing **only** in which checks run, sharing the real `combine()`. Headline: every spending limit the customer wrote, enforced perfectly, still approves **19 of the 28** attempts this engine holds — **CHF 3,836.28**. |
| ✅ | **The over-blocking half, which is the surprise.** Limits alone approve 16 of 17 ordinary purchases against our 17. `AU0007` carries a fragrance gift set on a household-groceries mandate; a cap cannot see a basket, so it spends the rolling weekly budget on it and then refuses `AU0008`, an ordinary grocery order. A limit protects the amount, not the errand. |
| ✅ | **`DEMO.md` answers Viseca's own five `control_question`s** from `scenario_catalogue.csv`, with fixtures — the closest thing the pack has to a marking scheme. Includes the one deliberate divergence, raised rather than left to be found: SCEN0003's rationale names *"unfamiliar merchants and countries"* and we score the merchant but **not** the country, because `CA0023` has 23 approved Italian rows and `CA0039` 21 American ones. |

### Phase 5h — Resynced to the official pack (added 2026-09-22)

Pulled `github.com/Swiss-ai-Weeks/viseca-2026` and diffed it against our copy. **No CSV
changed** — the 45-attempt board is untouched — but five documents did, and two of the changes
matter.

| | Task |
|---|---|
| ✅ | **Pack resynced**, `make verify-data` green on all 18 files against the updated manifest. Only `README.md`, `technical_details.md`, `data/README.md`, `data/data_dictionary.md` and `data/metadata.json` moved; every data file is byte-identical, which is why the board did not shift. |
| ✅ | **Judging criteria resolved** — see the open-tasks table. There was never a separate document; the dangling link has been removed upstream. |
| ✅ | **`data_dictionary.md` now says `initiator_type=human` includes cash withdrawals**, which sent us to look at what else the history file carries. It carries 83 cash withdrawals and 53 refunds alongside 4,565 purchases, and `HistoryIndex` was counting all of them as merchant familiarity — while the customer message says *"(N previous purchases)"*. Merchant counts are now purchases only, so the number means what the sentence says. Device familiarity deliberately still counts everything: it answers *"has this card been used from this device"*, and a refund from the cardholder's own phone is still their phone. **Measured first:** 10 of the 45 attempts shift from 23 prior purchases to 22, all at one merchant, nothing crosses the minimum, **no decision moves**. The two fixtures that actually show a count still read 21 and 6. |
| ✅ | **Noted for the pitch, not acted on:** the rewritten `technical_details.md` opens by explicitly welcoming a model — *"a model could understand customer instructions, assess unusual activity, or detect misleading shop text."* Our architecture already uses one where it is reviewable (the compiler) and refuses it where it would be attackable (the decision path). That is a choice we can now defend against an explicit invitation rather than an assumed prohibition — see [`DEMO.md`](DEMO.md) beat 1b. |

### Phase 5i — The trust score (added 2026-09-22)

| | Task |
|---|---|
| ✅ | **`domain/score.py` · [`specs/trust-score.md`](specs/trust-score.md).** One number per decision with every point accounted for. The hard part was making it defensible rather than decorative, so the spec leads with what it must *not* be: no second table of importances (`CONCERN_UNIT` converts an existing weight at a fixed rate, so `tools/tune.py` still owns the dial), no learned weights, no model, and it cannot contradict its decision because the band is chosen before the arithmetic runs. The driving finding is not charged twice — it is why the band is what it is. The tail is capped at 25, and capped lines are still *listed* at zero, because "we noticed this and it cost nothing" is a different statement from not showing it. |
| ✅ | **Coverage travels with it**, and `share` is **1.0 on all 45** — every check that applies, runs, because `CHECKS` has no placeholders. A record with nothing evaluated (the deadline guard's) has **no** score rather than a low one: a number there would read as a verdict on a purchase nothing reached. |
| ✅ | **The platform boundary is tested per fixture.** The score is in `decision_summary` — the service response and the audit record — and never in `to_payload()`. A presentation feature must not be able to affect a submission. |
| ✅ | Surfaced in both front-ends: a chip beside the verdict in `leash-demo` carrying the full arithmetic in its tooltip, and the whole object in the playground dataset. The mock adapter drops the score when an edit re-decides an event, because a recorded score beside a new verdict would be a lie and it cannot recompute one without reimplementing the scale. |
| ⛔ | **Dual-bound deadline budget — considered and declined.** Capping the engine at *(arrival + N ms)* as well as *(deadline − reserve)* is the more obviously careful design and the wrong one here: `domain/` is pure, no check performs I/O or reads a clock, and the measured p99 is under 5 ms against a 1,500 ms reserve. The second bound cannot fire, and `domain/settings.py` already states this repo's rule for that — *"a setting no check reads is not 'unsupported', it is inert."* Reasoning recorded in [`specs/deadline-guard.md`](specs/deadline-guard.md) so it is a decision rather than an omission. |

### Phase 5j — A goal that was already met (added 2026-09-22)

| | Task |
|---|---|
| ✅ | **`check_goal_fulfilled`** — spec: **[`specs/check-goal-fulfilled.md`](specs/check-goal-fulfilled.md)**. Two instructions name one thing, and the engine was approving **four monitors (CHF 1,430.40)** and **three pairs of the same shoes (CHF 512.00)**. Every one is compliant on its own facts, which is why nothing saw them: `duplicate_order` needs an identical cart, amount *and* seller by design, so a second monitor elsewhere at another price is invisible to it. |
| ✅ | **The gate is `item_keywords_all`, and it is the whole design.** An instruction naming a *thing* compiles keywords; one naming a *kind of thing* compiles a category and none. Only the first can be finished. Without it the check would report the second grocery delivery of the week as a completed goal — the most ordinary purchase in the pack. Verified across all five scenarios: keywords exist for exactly SCEN0002 and SCEN0004. |
| ✅ | **Default `note`, and the reason is evidence rather than convenience.** `scenario_catalogue.csv` describes `AU0023` as *"an unfamiliar but fully compliant seller"* and `AU0042` as *"a legitimate re-quote"* — both characterised by the organizers as purchases to get **right**. Escalating them would contradict their own description of their own fixtures and move our board from 17 approvals to 12, on a brief that names over-blocking as a failure. So the finding is recorded in the evidence, the reason codes and the customer's sentence, and the decision is unchanged. `repeat_purchase_action: "ask"` turns it into a question. The spec says plainly that a real deployment would probably ship `ask`. |
| ✅ | **No decision moved.** Five approval messages gain a closing clause; the first order of each errand carries nothing; `AU0036` stands down because a duplicate has the sharper sentence. Prior carts are tested with `lines_matching`, factored out of `item_matches_request` rather than reimplemented — two ways of deciding what "the thing you asked for" means is one too many. |
| ⬜ | `repeat_purchase_action` is honoured by the check but is not yet a composed customer setting: `settings.py`, `compose.py` and `amend.py` do not know it, so it cannot be set through `PATCH` or the preferences layer. `note → ask` is a tightening. Recorded as an open question in the spec. |

### Phase 5k — Item↔shop consistency: measured, and declined (2026-09-22)

A check was scoped for it: a subscription or membership line billed by a seller whose
`recurring_capable` is false, and a cart whose categories do not fit the shop's own. Measured
against the pack **before** writing it, on the same discipline as every other rule here — and
the measurement is why it does not exist.

| What it would fire on | Already reported by | Better? |
|---|---|---|
| `AU0007` cosmetics in a grocery basket | `unrequested_addon` | yes — names the item and its price |
| `AU0018` protection plan at a sports retailer | `unrequested_addon` | yes |
| `AU0022` sporting goods at a sustainable-goods shop | `merchant_type_not_permitted` | yes — names the rule broken |
| `AU0041` protection plan at an electronics shop | `per_order_limit_exceeded` + `unrequested_addon` | yes |
| `AU0043` a gift voucher where a monitor was asked for | `cart_contradicts_purpose` | yes |

**Five of 45, and all five are already non-approvals.** Not one approval on the board has a
category mismatch, and both recurring lines in the pack sit at sellers that cannot bill
recurring — and both are already caught. The check would add a sixteenth evidence row that
always duplicates a neighbour, and never change a decision or a message.

The second reason is the stronger one. A category mismatch is a **weak heuristic in the real
world** and this repo already says so: *"a supermarket sells cosmetics without ceasing to be a
grocery merchant"* (`GUIDELINES.md`, on `AU0007`). The multilingual injection patterns earned
their place because a benign corpus measures their false-positive rate; there is no such
corpus for this, and nothing in the pack could build one. Shipping an unmeasurable heuristic
that catches nothing new is the trade this project has refused everywhere else.

Neither does the brief ask for it: SCEN0002's rationale names *"retailer type"* (we have
`merchant_type`) and SCEN0004's names *"a cart that contradicts the stated purpose"* (we have
`cart_contradicts_purpose`). Both are built.

**Recorded as a decision, not an omission.** If a future pack contains an approval with a
genuine item↔shop inconsistency, this is the check to write, and the note above is the
starting point.

### Phase 6 — Hours 33–36: freeze

**Rehearsed end to end 2026-09-22, fully offline, outbound HTTP refused at the process
level.** All eight beats, both front-ends, the live board against `out/baseline.json`, and
every beat's sentence checked verbatim against `DEMO.md`. **Whole sequence ≈ 28 s of machine
time** — the HTTP walk 10.7 s, `make replay` 10.4 s, `make demo` 6.3 s, everything else under
0.2 s.

**No engine defect.** Four documentation defects, all fixed in the same pass:

| | Found | Fixed |
|---|---|---|
| 1 | `DEMO.md` had **two beats numbered 6** — introduced when the four-monitors beat was added | the monitors beat is now 7; beats read 1 · 1b · 2 · 3 · 3b · 4 · 5 · 6 · 7 |
| 2 | `PLAYBOOK.md`'s one-script dry run used fixed `sleep 9` / `sleep 10`; the runs complete in **~5.3 s**, so it spent four seconds of stage time doing nothing — and would still have been too short on a slower machine, which is the worse half of the trade | polls `status == "complete"` instead, verified at 5.4 s |
| 3 | `leash-demo/README.md` claimed *"the one outbound request is the Google Fonts stylesheet"* — there are **three** outbound `<link>`s (two `preconnect`, one stylesheet), plus a `GET /healthz` probe against the engine | README now states all four and what each does when refused |
| 4 | `DEMO.md`'s rehearsal note was stale: the 37 s figure, the 0.08 s fallback, and no mention of the beats added since | rewritten with the 2026-09-22 measurements |

**Confirmed working, offline:** the compiler falls back to the baseline in **0.09 s** with a
key present and outbound refused, recording `model_error: ConnectError; fell_back_to_baseline`
· the live board is identical to the saved one (17 · 20 · 8) · every beat sentence verbatim ·
the trust chip renders with its arithmetic · the 120-second step-up countdown, the resolve,
and the append-only trail (a `decision` line then a `resolution` line, nothing rewritten) ·
tighten adds a cap beside the old one, a widening is refused `422 rules_not_preserved`, revoke
is terminal · 16 checks registered · both UIs clean, no unexpected console errors.

| | Task |
|---|---|
| ✅ | **Rehearsed end to end, offline** — see above. |
| ⬜ | **Feature freeze.** No new checks. Rehearse, fix only demo-breaking bugs |
| ⬜ | Tag the demo commit in **all three** repositories — this one, `../playground`, `../leash-demo` — and have a known-good rollback for each |
| ⬜ | **Give the three repositories a remote.** None of them has one (checked 2026-09-09). Everything the team has built lives on one laptop. |

---

## Open tasks: what is doable before the event

Reviewed 2026-09-09. The distinction that matters is whether a task needs **the event** —
the team key, the live sandbox, the judging criteria, or another person.

**Doable now, alone, offline**

| | Task | Where |
|---|---|---|
| ⬜ | Decide live-vs-cached compile for the demo (recommendation already recorded: live) | Phase 4 |
| ⬜ | Tag a demo commit in each of the three repositories | Phase 6 |
| ⬜ | Give the three repositories a remote — currently the whole build is on one laptop | Phase 6 |
| ⬜ | Regenerate both copies of the decision data, or write the playground's staleness guard | Phase 5b |
| ⬜ | Check the OpenRouter credit balance (needs the key already in `.env`, and one outbound call) | Phase 4 |

**Deliberately not doing**

| | Task | Why |
|---|---|---|
| ⬜ | Widen `_FAMILIAR` | It is the demo's argument for the model. Phase 4 gap 2 |

**Blocked until the event**

| | Task | Blocked on |
|---|---|---|
| ✅ | ~~Judging criteria~~ **Resolved 2026-09-22 against the official repo** (`github.com/Swiss-ai-Weeks/viseca-2026`). There is no separate criteria document: `data/README.md` now reads *"Full public challenge brief **and judging criteria**"*, so the criteria are the brief itself — *"Judges should be able to understand what the system permitted, what evidence it considered, why it acted, and how customer retained control"*, plus the three required demonstrations. The `challenge_public.md#judging-criteria` link we were chasing was a dangling reference, **since removed upstream**. Per-scenario, the five `control_question`s remain the closest thing to a mark scheme and are answered in [`DEMO.md`](DEMO.md#visecas-own-five-questions). | — |
| ⬜ | Ownership split (`GUIDELINES.md` §12) | The other two developers |
| ⬜ | Team key, `make probe-live`, `make serve-live`, live-shape diff | The key. `GET /healthz` is unauthenticated, so *connectivity* is testable today; behaviour is not |
| ⬜ | Private scenarios / reset protocol / step-up window | Viseca |
| ⬜ | Walk a teammate through the Postman README | A teammate |
| ⬜ | Frontend sign-off on `service-contract.md` | Them — but `../leash-demo` now supplies the evidence, so this is a review, not a design session |

---

## What is explicitly not yours

The shopping agent · real payment rails · AP2-style signed mandate chains · the policy-authoring
UI (unless §12 assigns it) · anything that puts a model in the decision path.

## Daily discipline

1. `make check` before every handoff.
2. `make diff-decisions` after every rule change — **report every changed decision, including
   the expected ones.**
3. Spec before implementation. No exceptions, however obvious the rule looks.
4. Anything unconfirmed gets labelled `ASSUMPTION` and raised, not assumed away.
