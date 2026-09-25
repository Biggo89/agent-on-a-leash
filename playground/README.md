# Wallet Control Layer — Playground

An interactive walkthrough of the Wallet Control Layer, in two views.

**How it works** — for non-developers. What the system decides, the one architectural idea
behind it, all fifteen checks in plain language, and four instruments you can actually
operate: the policy compiler, the escalation meter, the rolling window, and an injection
sandbox.

**Engineering** — for people working on the codebase. Repo map, the nine invariants and how
each is mechanised, the type system, how to add a check, the decision algebra, the compiler's
eleven guard rails, the HTTP contract, runtime failure modes, the testing strategy, and a
task → file map.

Both views share the **Decision Explorer**: all 45 proposed purchases from the organizers'
data pack, with every check that ran, the evidence each one read, the concern score against
the threshold, and the message the customer would see.

> **This is the explainer, not the demo.** It answers *how does it work*; `../leash-demo`
> answers *watch it work*. They are deliberately separate and should stay that way — the
> editable policy, the preferences screen and the step-up all live over there. What belongs
> here is the account of **why** the rules are shaped the way they are.

## Open it

```bash
open index.html
```

No build step, no dependencies, no server. Everything is relative paths and classic scripts,
so it works straight off the filesystem. Fonts come from Google Fonts when there is a network
and fall back cleanly when there isn't — which matters, because the demo this documents is
designed to run with the Wi-Fi unplugged.

If you would rather serve it:

```bash
python3 -m http.server 8765
```

## The data is real

Nothing on this page is illustrative. Every decision, customer message, verdict, evidence line
and latency figure is output from the actual engine in `../wallet-control-layer`, replayed over
the organizers' data pack.

The 45 decisions are verified identical to the project's own regression baseline
(`wallet-control-layer/out/baseline.json`) — **17 approve · 20 decline · 8 step-up**. If the
engine's behaviour changes, this page is wrong until it is regenerated.

The board moves only when a rule changes, and every move is recorded. The customer-settings
work added four checks' worth of new surface and moved **no decision**, because every one of
them stands down unless the customer stated the rule. The split-order and card-or-person work
moved four, and all four moved *away* from a refusal — including `AU0008`, an ordinary grocery
order the board had been declining on a ledger inflated by an order it should have questioned.

## Regenerate after an engine change

```bash
uv run --project ../wallet-control-layer python tools/generate_data.py
```

This runs the real engine offline — no sandbox server, no network — and rewrites
`assets/data.js`. It mirrors `wallet-control-layer/tools/replay.py`, but drives events straight
from the data pack instead of over HTTP, and dumps the full check results rather than a summary
row.

It reads the data pack through the engine's own resolver: `$LEASH_DATA_DIR`, then
`../viseca-2026/data`. The pack is never written to.

## Layout

```
index.html                 shell, top bar, view switch
assets/styles.css          design tokens, light + dark, all components
assets/data.js             generated — 45 decisions with full check results
assets/content.js          narrative content for both views
assets/app.js              routing, the explorer, the four instruments
tools/generate_data.py     regenerates assets/data.js from the live engine
```

`assets/data.js` is generated. Everything else is hand-written.

## What is hand-maintained, and can drift

`assets/content.js` holds prose about the system: the check explanations, the invariants, the
guard rails, the endpoint table, the command list. It is written from `specs/`, `AGENTS.md`,
`GUIDELINES.md` and `DEMO.md` rather than extracted from them, so a change to the specs will
not update it automatically.

Four things in it are worth re-checking after significant engine work:

- **the check list (`CHECKS`) against `evaluator.CHECKS`** — same entries, same order. There
  is no guard on this one, and it has already drifted once: the two standing-preference checks
  (`category_exclusion`, `spending_hours`) shipped in the engine while this file still listed
  twelve. `assets/data.js` carries the real list under `config.checks`, so a quick diff of the
  two is the cheapest way to catch it.
- the fixtures cited as each check's example. Two checks correctly cite **none of the 45**:
  they are gated on a facet no pack instruction produces, which is the point rather than a gap.
- the concern weights and threshold (`SIGNALS`) — these are also asserted live in
  `assets/data.js` under `config`, so a mismatch between the two is visible
- the endpoint table (`ENDPOINTS`) against `specs/service-contract.md`, which has grown the
  amend, preferences and candidates routes

The manipulation patterns in `app.js` are a faithful port of `domain/sanitize.py` — same
regexes, same labels, same excerpt window — so that the injection sandbox demonstrates the real
detector rather than an approximation of it. If those patterns change in Python, change them
here too.

## Deliberately not included

This playground explains the control layer; it is not a second implementation of it. It makes
no HTTP calls, runs no engine, and holds no key. The what-if surface for changing a rule and
watching a decision flip is `POST /v1/decide` on the real service — see the Engineering view's
**HTTP surface** section — and the surface for *editing* one is `../leash-demo`, which has the
sheet, the blast radius and the standing preferences screen. Building a second editor here
would be two things to keep in step and one of them always stale.
