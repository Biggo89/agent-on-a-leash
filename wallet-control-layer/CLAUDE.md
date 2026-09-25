# Wallet Control Layer — "Agent on a Leash" (Viseca, Swiss {ai} Weeks 2026)

**Start here → [`AGENTS.md`](AGENTS.md)** — operating manual, decision boundary, conventions.
Team reference → [`GUIDELINES.md`](GUIDELINES.md) · Semantics → [`specs/decision-rules.md`](specs/decision-rules.md)

We build the **Wallet Control Layer** — the component that decides whether an AI shopping
agent may spend a customer's money. **We do not build the shopping agent.**

## Standing rules

- **Always respond in English**, even when Nakya writes in Italian or German.
- **Ask before deciding anything unspecified.** Label unconfirmed things `ASSUMPTION:`.
- **Spec before implementation** for every rule that affects a decision.
- **Never hardcode to `scenario_id`, authorization IDs, or replay position** — prohibited by
  the challenge.
- **Over-blocking is a failure mode, not a safe default.**
- **No LLM in the decision path.** Models compile the policy; deterministic code enforces it.
- Decisions are `approve` / `decline` / `step_up` — exact lowercase strings.
- Money is integer centimes. Compare `billing_amount_chf`, never `amount`.
- `../viseca-2026/` is the organizers' read-only repo. Never edit it.

## Commands

`make setup` · `make check` · `make test` · `make sandbox` · `make replay` · `make demo` ·
`make compile` · `make compile-llm`

Demo: **[`PLAYBOOK.md`](PLAYBOOK.md)** (what to type) · [`DEMO.md`](DEMO.md) (what it means) ·
`GUIDELINES.md` §11 (what to say)
