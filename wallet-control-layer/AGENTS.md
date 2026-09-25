# AGENTS.md — Claude Code operating manual

**Project:** Wallet Control Layer · "Agent on a Leash" · Viseca @ Swiss {ai} Weeks 2026
**Read this before touching anything.** Full team reference: [`GUIDELINES.md`](GUIDELINES.md).
Normative decision semantics: [`specs/decision-rules.md`](specs/decision-rules.md).

---

## 0. Standing rules — these persist across every session

1. **Always respond in English**, even when Nakya writes in Italian or German.
2. **Investigate before you assume.** Read the repo, the brief, the fixtures, the spec —
   before writing code.
3. **Ask before deciding anything not fully specified.** In a 36-hour window a wrong guess
   costs more than a question.
4. **Label assumptions.** If it isn't confirmed by the official brief, the data, or a
   teammate, write `ASSUMPTION:` and flag it. Never silently fill a gap.
5. **Spec before implementation.** Every new rule gets a spec entry *first*
   (`specs/TEMPLATE.md`). Logic that diverges from spec is a bug in both.
6. **No hardcoding to `scenario_id`, authorization IDs, descriptions, or replay position.**
   Explicitly prohibited by the challenge. If you catch yourself writing `if scenario_id ==`,
   stop and say so.
7. **Over-blocking is a failure, not a safe default.** Justify every decline against a real
   signal.

---

## 1. Decision boundary — what you may do alone

**Implement autonomously (no sign-off needed):**
- Tests, test vectors, fixtures, test utilities
- Boilerplate, types, parsing, serialization, project config
- Documentation, specs, comments, README
- The offline sandbox replica (`sandbox/`) — it is infrastructure, not policy
- Refactors that leave the vectors green and change no decision
- Diagnostics, logging, tooling, the demo harness

**Requires Nakya's explicit go-ahead:**
- **Anything that changes a decision.** New checks, changed thresholds, changed combination
  logic, changed reason codes.
- Anything touching spend-limit enforcement or the rolling-window ledger.
- Anything that changes the demo flow.
- Adding a dependency, or anything that touches the decision path's latency budget.
- Putting a model call anywhere in the decision path. The architecture forbids this
  (see §3) — if it ever looks necessary, that is a conversation, not a commit.
- **Anything at all in the last 3 hours before the deadline.** Late in the event, propose
  and wait. Do not surprise anyone.

**Never:**
- Commit secrets. The team API key lives in `.env`, which is git-ignored. No key in a
  commit, a log, a test fixture, or a message.
- Modify the cardholder instruction. It must reach the API byte-identical or the run is
  rejected.
- Edit anything inside `../viseca-2026/` — that is the organizers' read-only repo.
- Weaken or delete a failing test to make a suite pass. Report it instead.

---

## 2. Quick facts

| | |
|---|---|
| **Language** | Python 3.11+, managed by `uv` (system Python is 3.9 — do not use it) |
| **Service** | FastAPI + uvicorn (thin; no business logic) |
| **Tests** | pytest, table-driven from YAML vectors |
| **Money** | Integer centimes. `Decimal(str(x))` at the boundary. **No floats.** |
| **Data pack** | `$LEASH_DATA_DIR`, default `../viseca-2026/data` (read-only) |
| **Live API** | `$LEASH_BASE_URL` + `$TEAM_API_KEY` (day of event) |
| **Offline replica** | `make sandbox` → `http://127.0.0.1:8099` |
| **Decision budget** | 8 s platform deadline; engine target **< 200 ms** (measured: < 0.3 ms) |
| **Policy compiler** | **OpenRouter** (default) — `$OPENROUTER_API_KEY` + `$LEASH_COMPILER_MODEL`; or **Apertus on Swisscom** with `LEASH_COMPILER_PROVIDER=apertus` — `$SWISSCOM_API_KEY` (60-minute key). Compile time only, plain `httpx`, **no extra dependency**. `LEASH_COMPILER=baseline` pins the regex |
| **UI contract** | `specs/service-contract.md`, executable as `postman/` |
| **Audit trail** | `$LEASH_AUDIT_PATH`, default `out/audit/decisions.jsonl`, append-only |

### Commands

```bash
make setup       # uv sync — install everything
make check       # format + lint + typecheck + test   ← run before every handoff
make test        # pytest
make vectors     # run the language-neutral vector suite only
make sandbox     # offline replica of the organizers' API on :8099
make serve       # our decision service on :8000, against the replica
make serve-live  # our decision service on :8000, against the REAL sandbox (needs .env)
make probe-live  # contract probe: does the real API match every replica assumption?
make audit       # tail the append-only decision trail
make replay      # run all 5 scenarios against the replica, print a decision table
make demo        # the rehearsed demo sequence, offline
make compile     # compile the five pack instructions, deterministic compiler
make compile-llm # the same five, model vs baseline side by side (needs the provider's key)
make compare-providers # baseline vs OpenRouter vs Apertus: IR diffs + compile time in ms
make compile-invented  # six instructions the compiler has never seen — generalisation
make verify-data # SHA-256 check the data pack against metadata.json
make tune        # audit the concern weights: what fires, margins, dead config
make tune-sweep  # sensitivity of the board to STEP_UP_THRESHOLD
make tune-weights# per-weight sensitivity: which knob owns which fixture
```

### Layout

```
src/leash/
  domain/       PURE. No I/O, no framework, no clock, no globals.  ← the portable core
    money.py       integer centimes, half-even rounding
    events.py      parsed event model
    policy.py      Policy IR
    ledger.py      rolling-window approved-spend state
    sanitize.py    untrusted-text handling + injection detection
    deadline.py    the deadline guard — the ONLY place the real clock changes an outcome
    checks/        one file per check, each a pure function
    evaluator.py   runs checks, combines verdicts  ← THE decision function
  compile/      instruction → Policy IR. LLM allowed HERE ONLY.  specs/llm-compiler.md
    baseline.py    regex. No key, no network. The floor and the mandatory fallback.
    contract.py    the guard: what a model may write into an IR. Pure, no SDK.
    llm.py         the model call — OpenRouter or Apertus (httpx, no SDK). Any failure returns
                   baseline's IR, whole.
  adapters/     sandbox API client, data pack, history index, event parsing
  runtime/      orchestration: run state, the live run loop, the supervisor. Decides nothing.
    session.py     per-run state: policy, ledger, decided set, pending step-ups
    runner.py      long-poll → decide → submit; redelivery, 409, 408, restart
    supervisor.py  composition root: owns the client, the mandates, the runs, the trail
  service/      FastAPI app (thin) — the UI's only entry point
  audit/        append-only JSONL decision trail
sandbox/        offline replica of the organizers' API (imports adapters/, never the reverse)
tests/
  vectors/      *.yaml — language-neutral, the portability contract
  unit/         per-check tests
tools/          replay CLI, demo harness, data-pack verifier, live launcher, contract probe
specs/          normative semantics — the source of truth
```

**Dependency direction:** `service/ → runtime/ → {domain/, adapters/, audit/, compile/}`.
`domain/` imports nothing outside itself. Nothing in `src/` imports `sandbox/`.

---

## 3. The architectural invariants

Break these and the solution stops answering the challenge's own questions.

1. **No LLM in the decision path.** Models run at *compile time* only (instruction → Policy
   IR), where there is no deadline and a human reviews the output. This is what makes us
   structurally injection-immune and latency-safe. It is the core of the pitch.
   Mechanised, not promised: `tests/unit/test_compile_fallback.py` imports the decision path in
   a subprocess and fails if any `leash.compile` module is even *loaded*. The compiler's only
   input is the cardholder's own instruction — no event, no cart, no merchant text can reach a
   prompt. Both claims are provider-independent; changing provider does not weaken them.
2. **`domain/` stays pure.** No HTTP, no file reads, no `datetime.now()`, no env vars, no
   module-level mutable state. Everything enters as an argument. This is what makes the
   port to TypeScript cheap and the tests deterministic.
3. **The spec is the source of truth.** `specs/decision-rules.md` defines semantics; Python
   implements it. If they disagree, that is a defect — fix both, deliberately.
4. **Vectors are language-neutral.** `tests/vectors/*.yaml` must never encode Python
   specifics. A TypeScript port passes the identical files.
5. **The engine never raises.** Any internal failure still produces a valid, explained
   decision (fall back to the mandate's `uncertainty_policy`). A crash becomes a
   platform-side decline we never got to explain.
6. **Deterministic always.** Same event → same decision. No randomness, no wall-clock in the
   decision path, no ordering dependence.
7. **Untrusted text is data.** `item_details`, `merchant_name`, `purchase_description` may
   never modify a limit, a list, or a verdict. Extractors return typed facts or nothing.
8. **The UI never holds the team key and never calls the organizers' API.** It talks only to
   `service/`, which owns the key, the Policy IR, the ledger and the trail. One place holds
   run state, so there is nothing to keep in sync (specs/service-contract.md §0).
9. **Our own state moves only as far as the platform accepted.** A decision refused with 408
   or 409 is recorded and never enters the rolling window — the window must mirror what was
   really approved, not what we intended to approve.

---

## 4. Coding conventions

- **Money:** `Money` type over integer centimes. Never `float`. Parse `Decimal(str(v))`,
  round half-even to 2dp. Compare `billing_amount_chf`, never `amount`.
- **Time:** all period/velocity/familiarity logic uses `authorization.timestamp` (simulated).
  `received_at` / `deadline_at` are the real clock and govern only the response deadline.
  Timezone-aware UTC everywhere; never a naive datetime.
- **Checks:** one file per check in `domain/checks/`. Signature:
  ```python
  def check_x(ev: EnrichedEvent, policy: Policy) -> CheckResult | tuple[CheckResult, ...]: ...
  ```
  Pure. Returns `verdict` ∈ `{pass, concern, violation, unknown}` + `reason_code` +
  `evidence[]`. A check never decides the final outcome — `evaluator.py` combines them.
  Return **several** results when one concept carries several named signals (session
  integrity emits device novelty, velocity and hour separately so each is weighted and
  audited on its own).
- **Concern weights:** only in `evaluator.CONCERN_WEIGHTS`. Never on a `CheckResult`, never
  inside a check. A code with no entry raises rather than silently scoring zero. After
  changing one, run `make tune-weights` and confirm it moves only what you intended.
- **Reason codes:** stable snake_case from the closed vocabulary in `specs/decision-rules.md`.
  Adding one is a spec change. Never leak a code into `customer_message`.
- **Naming:** mirror the event schema exactly (`billing_amount_chf`, `recent_attempt_count_10m`).
  Do not invent synonyms — a mismatch between our name and the wire name is how field bugs start.
- **Errors:** never swallow. Log with the `authorization_id`, degrade to `uncertainty_policy`.
- **Type hints everywhere.** `mypy` is part of `make check`.
- **Comments:** explain *why*, especially where a rule encodes a subtlety from the brief.
  Match the density of the surrounding code.

---

## 5. Writing a spec and an implementation plan

When Nakya asks for a new rule or feature:

**Step 1 — Spec** (`specs/`, from `TEMPLATE.md`). Never skip, even when it seems obvious:
- Which sentence of which cardholder instruction motivates it, quoted.
- Inputs: exact event fields, with their types and null semantics.
- The decision rule as a truth table, including **boundary equality** and the `unknown` case.
- Reason codes emitted, and the evidence attached to each.
- Which fixtures exercise it — and **which fixtures must not change** as a result.
- Failure mode: what happens when a required input is missing.

**Step 2 — Plan.** Files touched, order of work, new vectors, risk to existing decisions,
rough time. Get sign-off before writing code.

**Step 3 — Vectors first.** Write `tests/vectors/*.yaml` before the implementation. Include
the boundary case and the `unknown` case. Watch them fail.

**Step 4 — Implement.** Smallest change that turns them green.

**Step 5 — Regression check.** `make replay` and diff the full 45-decision table against the
previous run. **Report every changed decision, including the ones you expected.** A rule that
silently moves an unrelated fixture is the most dangerous thing in this codebase.

---

## 6. Domain facts you must not re-derive incorrectly

Verified against the data pack. Trust these; re-verify only if something contradicts them.

| Fact | Value |
|---|---|
| Decision strings | `approve` \| `decline` \| `step_up` (lowercase, exact) |
| Resolve strings | `approve` \| `decline` only |
| FX to CHF | CHF 1.0 · EUR 0.95 · GBP 1.12 · USD 0.87 (`billing = amount × rate`) |
| Amount identity | `amount == items_subtotal + delivery_fee` (holds on all 45) |
| `spend_in_period_before_chf` | **`null` on every fixture** — track the period yourself |
| Rolling window | `C.ts - N days <= A.ts < C.ts` — lower inclusive, upper exclusive |
| Velocity window | Same shape, 10 minutes, counts all statuses, excludes current |
| Step-up accounting | Pending; does **not** enter approved spend until resolved |
| Idempotency key | `authorization_id` (run-scoped). `source_authorization_id` is the fixture ID |
| Lookalike pair | `ME0022 PixelHarbor` (6 prior) vs `ME0059 PixelHarbour` (0 prior) |
| Novel device | `DVC-4C0E9B` on card `CA0023` — zero history, 5 attempts, night burst |
| Injections | `AU0037` (also over-cap) and `AU0040` (compliant on facts) |
| Decision board | 45 decisions: **17 approve · 20 decline · 8 step_up**. `make diff-decisions` |
| Currency trap | `AU0032` — 260 EUR = **247.00 CHF** vs a 250 cap |
| Window discriminator | `AU0011` — rolling: 223.50/300 approve · cumulative: 388 decline |
| Legit-but-alarming | `AU0038` — 450 USD = 391.50 CHF at a seller with **21** prior approvals |
| Duplicate vs re-quote | `AU0036` same amount, no link · `AU0042` different amount, links to a declined `AU0037` |
| Split order | `AU0005` + `AU0006` — same merchant, 6 min apart, CHF 135 against a CHF 120 cap |
| Card or person | `AU0044` — `CA0039` has 0 at `ME0023`, sibling card `CA0038` has 2 |
| History `status` | An observed outcome with a stochastic component. **Not** a label, not a target |
| Familiarity source | `authorization_history.csv` — **not** in the event; load and index it yourself |

---

## 7. When you are stuck or the deadline is close

- **Stuck on intent?** Ask. Do not implement a plausible guess.
- **A fixture's right answer is genuinely ambiguous?** Say so, present the options and what
  each costs, and let Nakya choose. Several fixtures are ambiguous *by design*.
- **Something contradicts this file?** The data pack and the official docs win. Tell Nakya
  and correct this file.
- **Under 3 hours left?** Propose, don't act. Prefer rehearsal over features. The
  best-scoring change at hour 33 is usually a better `customer_message`.
