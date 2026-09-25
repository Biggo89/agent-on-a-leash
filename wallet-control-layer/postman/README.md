# Run the Wallet Control Layer from Postman

**You do not need to know how to code to use this.** You need to run two commands once, then
everything else is clicking **Send** in Postman.

This is the control layer for the Viseca *Agent on a Leash* challenge: the thing that decides
whether an AI shopping agent may spend a customer's money. Every proposed purchase gets one of
three answers — **approve**, **decline**, or **step_up** (ask the customer) — with a
plain-language explanation and an audit record.

---

## What you need

| | | |
|---|---|---|
| **Postman** | [postman.com/downloads](https://www.postman.com/downloads/) | free, no account needed for local work |
| **This repository** | on your laptop | ask the team for the folder or the git URL |
| **`uv`** | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | installs the right Python for you — your system Python is not used |
| **The team API key** | issued by the organizers on the day | only for the live sandbox; the offline mode needs no key |

---

## Step 1 — start the engine

Open a terminal, go to the `wallet-control-layer` folder, and run:

```bash
make setup
```

That installs everything, once. Then pick one of the two modes:

### Offline (works with no key, no network — use this to learn the tool)

You need **two terminals**.

```bash
make sandbox
```

Leave that running. In the second terminal:

```bash
make serve
```

### Live (the organizers' sandbox — the real thing)

Put the team key in a file called `.env` in the `wallet-control-layer` folder:

```bash
cp .env.example .env
```

Open `.env` in any text editor and put the key after `TEAM_API_KEY=`. Save it.
**Never commit or share that file — it is your team's key.** Then:

```bash
make serve-live
```

Either way you should see something like:

```
  service   http://127.0.0.1:8000
```

Leave the terminal open. Closing it stops the engine.

> **`make: command not found`?** On macOS, run `xcode-select --install` first.
> **Anything else?** Paste the whole error to the team — it is not your fault, and the message
> usually says exactly what is missing.

---

## Step 2 — import into Postman

1. Open Postman → **Import** (top left).
2. Drag **both** of these files in:
   - `postman/leash.postman_collection.json`
   - `postman/leash.postman_environment.json`
3. Top right, in the environment dropdown, choose **Leash — local**.

That is the whole setup.

---

## Step 3 — use it

Open the collection in the left sidebar. The folders are in the order you want them.

### The 30-second version

1. **0 · Start here** → Send all three. All green means you are connected.
2. **2 · Watch it decide** → Send *Start a scenario*.
3. Send *See the decisions* every second or so until it says `complete`.

A run stops at every step-up until the customer answers — on the real platform and in the
offline replica alike. Folder 2 answers them for you (it declines each one, and says so), so
the run can finish; folder 3 is where you answer one yourself.

**Open the Console** (bottom left, or `Cmd/Ctrl + Alt + C`). That is where the interesting
output is — each decision is printed in the plain language the cardholder would actually see:

```
[OK]  AU0002  Approved: CHF 68.40 at ValleyFresh Market.
[NO]  AU0004  Declined: CHF 142.00 at ValleyFresh Market. CHF 142.00 exceeds the CHF 120.00
              per-order limit you set.
[OK]  AU0011  Approved: CHF 45.00 at ValleyFresh Market.
Rolling 7-day spend: CHF 223.00 (2 orders in the window)
```

### The rest of the folders

| Folder | What it shows |
|---|---|
| **0 · Start here** | Is everything up, and which sandbox are we talking to |
| **1 · The customer's instruction** | A sentence becomes enforceable rules — and the customer can tighten or revoke it, but never loosen it |
| **2 · Watch it decide** | The engine deciding a whole scenario against the live API |
| **3 · When it needs you** | The `step_up` answer: the engine asks, a human answers |
| **4 · Show your work** | The audit trail behind any single decision |
| **5 · What if the rules were different?** | Change a limit, re-send the same purchase, watch the answer flip |
| **6 · The organizers' sandbox** | Debugging only — talks to the sandbox directly |

Every request has a description explaining what it does and what to look at. Requests remember
what the next one needs, so you never have to copy an ID by hand.

### Try a different scenario

In the collection's **Variables** tab, change `scenario_id`:

| id | what it exercises |
|---|---|
| `SCEN0000` | one small purchase — proves the path works |
| `SCEN0001` | a per-order limit and a weekly budget that *rolls* |
| `SCEN0002` | the right item, the right size, and acceptable return terms |
| `SCEN0003` | a session that looks hijacked — and its recovery |
| `SCEN0004` | a merchant embedding instructions in its product text |

Then re-run folder 2.

---

## Things that are supposed to fail

Two requests are **meant** to come back red. They are the point, not a bug:

- **1 · Loosen it** → `422 policy_loosened`. A leash an agent could loosen is not a leash.
- **3 · Customer says no**, run after *Customer says yes* → `409 already_resolved`. A
  confirmation can only be answered once.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| `ECONNREFUSED 127.0.0.1:8000` | The engine is not running. Go back to step 1. |
| `"upstream": {"reachable": false}` | The engine is up but the sandbox is not. Offline mode: start `make sandbox`. Live mode: check the venue Wi-Fi. |
| `401` on folder 6 | The team key is missing or wrong. Check `.env`, then restart `make serve-live`. |
| `No environment selected` | Pick **Leash — local** in the dropdown, top right. |
| A step-up request 404s | Run folder 3 from its first request; it starts its own scenario. |
| Everything is empty after a restart | Team state resets. Just start a run again. |

---

## For developers

The collection is the executable form of [`specs/service-contract.md`](../specs/service-contract.md),
which is the agreed HTTP contract between the UI and the engine. `tests/test_postman_collection.py`
checks every request against the service's real routes, so the two cannot drift.

Run the whole thing headlessly:

```bash
npx newman run postman/leash.postman_collection.json -e postman/leash.postman_environment.json --delay-request 700
```

To regenerate the collection after a contract change, edit the requests in Postman and export
over the file, or update the generator alongside `specs/service-contract.md`.
