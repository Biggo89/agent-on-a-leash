# Wallet Control Layer — "Agent on a Leash"

**Viseca challenge · Swiss {ai} Weeks 2026 · Hack Zurich · 24–25 Sept 2026**

The component that decides whether an AI shopping agent may spend a customer's money.
**We build the control layer, not the shopping agent.**

Every proposed transaction gets one of three answers — `approve`, `decline`, or `step_up`
(ask the customer) — with a plain-language explanation and an audit trail.

## Start here

| Document | What it's for |
|---|---|
| [`GUIDELINES.md`](GUIDELINES.md) | Team reference: scope, architecture, data model, demo script, pitfalls |
| [`AGENTS.md`](AGENTS.md) | Claude Code operating manual: decision boundary, conventions, verified domain facts |
| [`TASKS.md`](TASKS.md) | Objectives and hour-by-hour task breakdown |
| [`PLAYBOOK.md`](PLAYBOOK.md) | **Running the demo** — every command, commented, with its verified output |
| [`DEMO.md`](DEMO.md) | The five beats, the evidence behind each, and the judge answers |
| [`specs/decision-rules.md`](specs/decision-rules.md) | **Normative** decision semantics — the source of truth |
| [`specs/policy-ir.md`](specs/policy-ir.md) | How a natural-language instruction becomes executable permissions |
| [`specs/service-contract.md`](specs/service-contract.md) | The HTTP contract between the UI and the engine |
| [`postman/README.md`](postman/README.md) | **Run it without writing code** — written for non-developers |
| [`specs/TEMPLATE.md`](specs/TEMPLATE.md) | Spec template — fill one in before writing any rule |

## Setup

```bash
make setup        # uv installs Python 3.12 + deps (system Python 3.9 is not used)
make verify-data  # SHA-256 check the organizers' data pack
make check        # format + lint + typecheck + 176 tests
```

The read-only data pack is resolved from `$LEASH_DATA_DIR`, then `../viseca-2026/data`,
then `./data`. Copy `.env.example` to `.env` for the team key on the day — **never commit it.**

## Run it

```bash
make sandbox      # offline replica of the organizers' API  → :8099
make replay       # all 5 scenarios, 45 decisions, printed as a table
make demo         # the rehearsed demo sequence, fully offline
make impact       # what the control layer adds over a plain spending limit
```

`make replay` needs `make sandbox` running in another terminal. Nothing here touches the
network, so the demo works if the venue Wi-Fi dies.

### The service the UI and Postman talk to

```bash
make serve        # decision service → :8000, against the offline replica
make serve-live   # decision service → :8000, against the REAL sandbox (needs .env)
make probe-live   # does the real API match every assumption the replica makes?
make audit        # tail the append-only decision trail
```

The UI never holds the team API key and never calls the organizers' API — it talks only to
this service, which owns the key, the compiled policy, the rolling-window ledger and the audit
trail. The contract is [`specs/service-contract.md`](specs/service-contract.md); the
[Postman collection](postman/) is the same contract, clickable, for the people on the team who
do not write code.

**On the day:** `make probe-live` first. It checks every place the offline replica had to
guess against the real API and prints what would need changing — rather than discovering it
mid-demo.

## Using the LLM compiler

The model runs in exactly one place — turning the customer's instruction into a reviewable
Policy IR, once, at authoring time. It is **optional**: with no key configured, a regex
compiler produces a usable mandate and every decision is unchanged.

```bash
# 1. Put an OpenRouter key in .env (LEASH_COMPILER_MODEL has a measured default).
cp .env.example .env && $EDITOR .env      # set OPENROUTER_API_KEY
#    …or use Swisscom's Apertus: LEASH_COMPILER_PROVIDER=apertus + SWISSCOM_API_KEY

# 2. See what the model adds, beside the deterministic compiler.
make compile-llm

# 3. See what it cannot change: all 45 decisions, every mandate model-compiled.
make replay-llm                            # → "no decisions changed"
```

`LEASH_COMPILER_PROVIDER` selects who serves the model — `openrouter` (default) or `apertus`
(Swisscom's hosted Apertus 1.5 70B); `make compare-providers` compiles the same instructions
with both and prints the IR differences and compile time in ms.
`LEASH_COMPILER` selects the compiler — `auto` (model when a key is present, default),
`baseline` (never call a model), `llm` (must try). The service takes the same choice per
request as `"mode"` on `POST /v1/mandates/compile`, which is how a UI shows the two
side by side.

Every compiled IR names its own compiler in `compiler` and records every guard action in
`compiler_notes`, so a mandate never hides how it was produced. Full runbook:
[`PLAYBOOK.md`](PLAYBOOK.md) Part 3b. Normative spec: [`specs/llm-compiler.md`](specs/llm-compiler.md).

## Plugging an agent in — the connector

An agent host (Claude, ChatGPT, Claude Code) is let onto the leash the way Gmail or GitHub
are: **one URL, an OAuth consent, five tools.** The connector is MCP over HTTP at `/mcp` on
this service; the tokens come from a mock Payment App — the LEASH onboarding, whose last step
is the consent. The agent can propose a mandate, ask what is allowed, request a payment, check
one, and list activity — and never confirm, answer, amend or revoke, because no such scope
exists for an agent.

```bash
make sandbox            # terminal 1: the replica
make connect            # terminal 2: service + Payment App under /app, one port (:8010)
make probe-connector    # terminal 3: the official MCP client runs the whole flow (optional)
claude mcp add --transport http Leash-Wallet http://127.0.0.1:8010/mcp   # or paste the URL into Claude
```

Phone: `http://127.0.0.1:8010/app/`. The demo has its own port so it can run beside `make serve`
or `make serve-live`. For claude.ai, which runs in the cloud, put one tunnel in front of it
(`cloudflared tunnel --url http://localhost:8010`) and paste `…/mcp`. The connector is named
**Leash Wallet** (`Leash-Wallet` in Claude Code). Setup in five minutes:
[`../connector/SETUP-WITH-CLAUDE.md`](../connector/SETUP-WITH-CLAUDE.md). Every command, verified
with Claude Code: [`PLAYBOOK.md`](PLAYBOOK.md) Part 10.
Design note: [`../connector/README.md`](../connector/README.md) · normative:
[`specs/connector.md`](specs/connector.md) · the app: [`payment_app/README.md`](payment_app/README.md).

## Architecture in one line

An LLM compiles the customer's instruction into reviewable rules **once**, at authoring time;
a **deterministic, LLM-free engine** enforces them on every transaction.

That is what makes the system structurally immune to prompt injection (there is no prompt in
the decision path to inject into), predictable when models fail, fast enough for the 8-second
deadline, and explainable to a card issuer.

```
instruction → [compiler: LLM ok] → Policy IR → customer confirms → mandate
                                                                      ↓
   authorization.request → parse → enrich → checks → combine → approve/decline/step_up
                                  ── no LLM past this line ──          ↓
                                                            audit record + explanation
                                                                       ↓
                                              step_up → the customer answers → /resolve
```

Current board across the 45 public attempts: **17 approve · 20 decline · 8 step_up**, every
decision in under a millisecond. `make diff-decisions` fails the build if any of them moves.

Every decision also carries a **trust score** — one number, 0–100, with every point pointing
at a check already on the record. It is a rendering of the concern arithmetic the engine
already does, never a second opinion: no learned weights, no model, and it cannot contradict
its own decision because the decision picks the band before the arithmetic runs. Coverage
travels with it, and it is **1.0 on all 45** — every check that applies, runs.
[`specs/trust-score.md`](specs/trust-score.md).

**What that buys over a spending limit** — `make impact`, same 45 attempts, no figure of ours
in it: every limit the customer wrote, enforced perfectly, still approves **19 of the 28**
attempts this engine holds back, **CHF 3,836.28** of spend. None of the nineteen is about the
amount. And limits alone approve only 16 of 17 ordinary purchases against our 17, because a
cap that cannot see a basket spends the weekly budget on the wrong thing and then refuses the
groceries.

## Project layout

```
src/leash/domain/    PURE decision core — no I/O, no framework   ← the part that ports
src/leash/compile/   instruction → Policy IR (LLM allowed here only)
src/leash/adapters/  API client, event parsing, history index
sandbox/             offline replica of the organizers' API
tests/vectors/       language-neutral YAML conformance suite
specs/               normative semantics
```

## Status

Sixteen checks implemented against the 45 public fixtures — per-order, rolling-period and
split-order limits, merchant permission, merchant type, merchant lookalike, order terms, item
identity, item attributes, unrequested add-ons, a goal already fulfilled, duplicate vs
re-quote, category exclusion, spending hours, session integrity, and merchant-text
manipulation. Each has a spec in
[`specs/`](specs/), language-neutral vectors, and a recorded regression.

Current board: **17 approve · 20 decline · 8 step_up**. **1238 tests**, plus 45 live
assertions against the model (`make compile-live`).

Merchant text is read for manipulation in **German, French, Italian and English** — 37
patterns against ten language-neutral labels, measured at **0 false positives** over a corpus
of ordinary merchant copy in all four ([`specs/check-manipulation-detected.md`](specs/check-manipulation-detected.md)).

Phases 0–5 are complete in [`TASKS.md`](TASKS.md): the checks, live-API integration, the
audit trail and service, the LLM compiler, and the demo runbooks. What remains is Phase 6 —
feature freeze and rehearsal — plus two operational items: point it at the real sandbox with
the team key on the day (`make probe-live`), and keep an eye on the OpenRouter balance, since
an exhausted account degrades to the regex compiler silently and correctly.
