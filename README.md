# Agent on a Leash — Wallet Control Layer

**Viseca challenge · Swiss {ai} Weeks 2026 · Hack Zurich · 24–25 Sept 2026**

The component that decides whether an AI shopping agent may spend a customer's money.
Every proposed purchase gets `approve`, `decline` or `step_up` (ask the customer), with a
plain-language reason and an audit trail. We build the control layer, not the shopping agent.

## Folders

| Folder | What it is |
|---|---|
| [`wallet-control-layer/`](wallet-control-layer/) | **The engine.** Python decision service: the policy compiler, the fifteen checks, the three policy layers, the rolling-window ledger, the audit log, an offline replica of the organizers' API, and the test suite. |
| [`leash-demo/`](leash-demo/) | **The live demo UI.** Plays the agent lifecycle end to end: the agent shops, the Leash checks each purchase, the cardholder answers step-ups — and edits any rule, with the blast radius shown before they commit. |
| [`playground/`](playground/) | **The explainer.** An interactive walkthrough of how the system works, with one view for non-developers and one for engineers, plus a Decision Explorer over all 45 purchases. |
| [`viseca-2026/`](viseca-2026/) | **The organizers' materials** (read-only). Challenge brief, sandbox API details and the synthetic data pack the engine runs on. |

## Quick start

```bash
(cd wallet-control-layer && make setup && make check)   # install and run the tests
(cd leash-demo && ./serve.py)                           # demo UI → http://127.0.0.1:8771
(cd wallet-control-layer && make serve-live)            # engine on the organizers' sandbox → flip "Live sandbox"
open playground/index.html                              # explainer, no server needed
```

Each folder has its own README with the details. The engine reads the data pack from
`../viseca-2026/data`. Copy `wallet-control-layer/.env.example` to `.env` for API keys and
never commit it.
