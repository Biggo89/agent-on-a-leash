# Leash — the agent-on-a-leash demo

An interactive UI for the **Wallet Control Layer**: watch an AI shopping agent work, watch
every purchase it attempts get checked against the cardholder's mandate, and answer the ones
the control layer will not decide alone.

Three columns, which are the pitch:

| Agent | The Leash | You |
|---|---|---|
| understands intent | checks & enforces | stay in control |
| the agent's tool calls as it shops | all fifteen checks, evidence, concern score, verdict | the one app: ledger, mandate, step-up, **edit any rule**, preferences, revoke |

## Run it

```bash
./serve.py
```

Opens `http://127.0.0.1:8771`. No build, no `npm install`, no dependencies, and nothing leaves
the machine except the web font: two `preconnect` hints and one stylesheet, all to Google
Fonts. `font-family` declares `Roboto` in front of the system stack, so with the Wi-Fi
unplugged the page renders in the platform's own typeface and nothing else changes — verified
on 2026-09-22 with outbound HTTP refused at the process level.

The only other request it makes is `GET /healthz` against the engine on `:8000`, to decide
whether the **Live** toggle is offered. With no engine running that request is refused, the
demo stays in Replay mode, and the console carries an expected `ERR_CONNECTION_REFUSED` for
each check — at load, when Live is pressed, and when the window regains focus.

**Presenter keys:** `space` run/pause · `→` one order · `r` reset · `1` `2` `4` speed ·
`a` / `d` answer a step-up.

## The five beats

The pitch, in about four minutes. Everything after them is the answer to *"can the customer
edit this?"*, which is a different question and gets its own section.


1. **The mandate.** Pick a scenario and the cardholder's own sentence appears in the centre
   column with every span that became a rule highlighted. Each compiled rule quotes the words
   it came from. *A compiler that cannot quote you cannot add a rule.*
2. **An ordinary order.** Run the agent. It searches, picks a seller, fills a basket, asks to
   pay — and fifteen checks run, passes included, in about a millisecond.
3. **The rolling window.** `Household budget` climbs to exactly CHF 300.00 of 300 — which the
   cap admits, because it is `<=` — then the ledger *falls back* to 255.50 and 223.50 as
   earlier orders age out. A naive cumulative counter reads 388.00 and declines a legitimate
   grocery delivery at AU0011. Ours approves it.
3b. **The order that was split in two.** AU0005 is CHF 70.00 at 17:20; AU0006 is CHF 65.00 at
   17:26, same shop. Each clears the CHF 120 cap on its own; together they are CHF 135.00.
   The phone asks — *"each order is within your limit on its own"* — because nothing was
   breached and a shopper who forgot the milk is not an attacker. Hold it and AU0008 is
   approved two orders later, which an inflated ledger would have refused.
4. **Injection.** `Manipulated agent` → AU0037 carries text addressed to automated payment
   systems. It is highlighted in the seller's own text, reported in the decision, and it
   changes nothing: the order fails on arithmetic. No model sits in the decision path, so
   there is no prompt to inject into.
5. **Control.** Answer a step-up on the phone with the 120-second window counting down.
   Then drag *Tighten the leash* to CHF 250 — the hint says how many of the agent's remaining
   orders that will decline — apply it, and watch AU0038 flip from approved to
   *"CHF 391.50 exceeds the CHF 250.00 per-order limit you set in the app."* Then revoke, and
   the agent stops. A step-up still on the phone when you revoke is declined: the revoke is
   the customer's answer to it, and left pending it would hold the platform's queue.
---

## Five more, when the policy itself is the question

Raised in the Viseca Q&A: the control layer should let the customer edit settings and rules to
fit their own profile. These beats are that answer. They need the **You** column and nothing
else, so take as many as the time allows and stop anywhere.

6. **Editing the policy.** Every row of the mandate is a button — the slider in beat 5 is the
   fast gesture for the one number a presenter always changes; this is the whole editor. Tap the per-order limit and
   the sheet says what the edit would do *before* you commit — *"6 of the agent's 11 remaining
   orders would be decided differently"* — and then what it costs. **Narrowing is one tap.
   Widening says so and asks for a new confirmation**, because widening what an agent may do
   is a new consent moment rather than an edit to an old one. Clearing an allow-list is
   refused outright, with both sides named.
7. **The customer, not the errand.** The sliders icon in the phone header opens *Your
   preferences* — the standing layer, which outlives every mandate. Set *only electronics
   shops* there and the Leash column's `merchant_type` check goes from `n/a` to `PASS` on the
   next order: a rule the instruction never mentioned, now enforced. One line on that screen
   is the whole architecture — *"Your per-order limit is CHF 400.00 — this errand asks for CHF
   400.00, your account at CHF 900.00. The tightest one applies."*
8. **Two ceilings at once.** Add a standing *CHF 500 across any 30 days* and the mandate
   grows a second spending row the instruction never mentioned. Both windows are enforced:
   an order inside the errand's weekly limit can still be refused by the monthly one, and the
   message says which. Until multi-window enrichment the shortest window displaced the
   longest, which made an *add-only* tightening able to delete a ceiling —
   `wallet-control-layer/specs/decision-rules.md` §9 has the measured case.
9. **Rules the instruction could not say.** *Things I never buy* is a deny-list, which an
   allow-list cannot express: the complement of "no gift cards" is the other twenty-one
   categories, and that is wrong the moment the vocabulary grows. *Hours the agent may buy* is
   a stated rule, deliberately not the same finding as the night-hours risk signal — one is
   the customer's rule, the other is our inference, and conflating them would decline every
   legitimate late-night order. Both come from `customers.csv`: Alex Meier's profile says
   *"avoids gift vouchers"*, and the preferences screen offers it back as a candidate that
   quotes those words.
10. **How often to ask.** *Only ask me when it is serious* turns two of SCEN0004's step-ups
    into approvals — and says so before you confirm it. Three positions, not a slider: the
    engine's own sweep reports the board flat from 0.5 to 2.0 with the nearest cliff at 2.5,
    so a free number would imply a precision the data does not have. It is also the clearest
    widening in the build: a countable price, shown before you pay it.

**The line that ties them together, if you only say one:** narrowing is one tap, widening
costs a confirmation, and the engine decides which is which. The customer is never asked to
know.

## What is real, and what is not

**Real:** every decision, verdict, evidence line, concern score, latency and customer message.
They are output from the engine in `../wallet-control-layer`, replayed over the organizers'
data pack — 45 authorizations, **17 approve · 20 decline · 8 step-up**, identical to the
project's own regression baseline.

**Reconstructed:** the agent's tool calls. The data pack records what an agent tried to pay
for, not the browsing before it, so `search_merchants` / `select_merchant` / `add_to_cart` are
derived from each order's own facts — merchant, category, basket, prices, prior-approval
counts, enrichment flags. The UI says so, in the footer of the agent column.

**Computed here, not replayed:** two things that are properties of a live session rather than
of a fixture.

- The **rolling-window ledger**, read from the engine's own `window_before_chf` on each event
  — never accumulated locally, because a local sum is exactly the bug the window exists to
  avoid.
- The **re-decision after an edit**, which reimplements the rules the engine owns —
  `decision-rules.md` §1.1, §5 and §9, and the editable checks themselves in
  `src/adapters/recheck.js` — and recomposes the message with the engine's own grammar.
  Verified against the engine's text on 2026-09-21: tightening SCEN0004 to 250 reproduces
  `"Declined: CHF 391.50 (USD 450.00) at HarborByte. CHF 391.50 exceeds the CHF 250.00
  per-order limit you set in the app. An order of CHF 250.00 or less would be within your
  limit."` word for word, and the same cap set as a *standing preference* instead reproduces
  `"…per-order limit you set in your preferences."`

  Two places are narrower than the engine, both marked in the code and both erring the same
  way — toward asking rather than approving:

  - `order_terms` compares against a return window the engine extracts from the seller's item
    text, and a fixture carries that number only when the scenario already ran the check.
    Adding a return-window rule where there was none therefore reads `unknown` here, which
    routes through `uncertainty_policy` and asks. That is the engine's own behaviour for an
    unestablished fact.
  - A **period limit on a window the fixtures do not carry** cannot be re-decided in replay:
    the ledger here is read from the engine's own `window_before_chf`, which is one window's
    figure. `SETTING_META.period_limit_chf.replayable` is `false` and the sheet shows no
    blast-radius count for it rather than guessing one.

In live mode neither gap exists, and both the ledger and the re-decision come from the
service.

## On smaller screens

The three-column deck is a projector layout. Below **1180px** the stage shows one panel at a
time and a switcher appears in the transport bar — `Agent · Leash · You` — so nothing is ever
unreachable, down to a 320px phone. Verified for horizontal overflow at 320 · 360 · 375 · 390 ·
430 · 600 · 700 · 768 · 834 · 900 · 1024 · 1179 · 1180 · 1280 · 1366 · 1440 · 1512, plus phone
landscape (844×390).

What changes as it narrows:

- **≤1400** the tagline goes · **≤1179** one panel + switcher · **≤900** two transport rows,
  icon-only Step and Reset · **≤660** no device frame around the phone (a phone inside a phone
  is silly), no speed picker, `env(safe-area-inset-*)` respected · **≤480** the source switch
  shortens to *Live*.
- **Height matters too, and the budget is tight.** At a 800px viewport the You column has
  634px and wanted 752px. Two things give: the tighten and revoke controls are one compact
  card rather than two stacked ones (197px → 77px — they are actions, needed one moment each,
  and only have to be one gesture away), and the mock status bar hides below 850px. The phone
  screen is then `clamp(300px, 100dvh - 300px, 560px)` and scrolls internally as a last
  resort. Measured: the whole `one` screen — ledger, every mandate row, and both controls —
  is fully visible with nothing hidden anywhere from 800px up; only below that does the app
  scroll inside the phone.
- A **step-up** is the one thing that must not hide behind a tab: it switches to the **You**
  panel by itself, marks that tab with a live dot, and on a phone the confirmation leaves the
  mock device to become a fixed bottom sheet with full-width Approve / Decline.

## Does it still tell the truth?

```bash
node check.mjs
```

Asserts the demo against the engine's own regression baseline, and against every claim it
computes rather than replays. 55 assertions:

```
— the board —          45 authorizations · 17 approve · 20 decline · 8 step-up
— provenance —         every rule and facet quotes its instruction verbatim, all 5 mandates
— the rolling window — AU0008 lands on exactly 300.00 → AU0010 255.50 → AU0011 223.50
                       (not the cumulative 388.00)
— the split order —    AU0005 + AU0006 = CHF 135.00 at one shop in 6 min, against a CHF 120 cap
— the spend ring —     the arc moves on 6 of 10 orders, peaks at 100%, and runs *backwards*
                       twice — freeing CHF 44.50 then CHF 32.00 as orders age out
— tighten-only —       widening is 422; AU0038 flips, in the engine's own wording
— revoke —             a revoke declines the step-up waiting on the customer, and nothing
                       is left waiting on the phone
— editable settings —  all 12 settings name a check the engine actually runs
— tighten/widen —      a lower cap applies; a higher one waits; an empty list is refused
— period windows —     a lower cap on one window narrows; a monthly ceiling beside a weekly
                       one narrows; swapping one window for another widens, and says which
                       ceiling it dropped
— exclusions, hours —  an item exclusion refuses 10 orders in the engine's own wording;
  and sensitivity      stated hours refuse 4 night-time ones; "only ask me when it is
                       serious" turns 2 step-ups into approvals and no decline
— the blast radius —   the preview counts flips and mutates nothing
— preferences —        a standing cap binds under a looser mandate and names itself;
                       a looser one never widens it; a per-errand setting is refused
— the profile —        all 5 personas match customers.csv / cards.csv / accounts.csv,
                       and no candidate ever proposes an amount
— css namespaces —     no editor rule restyles another panel's class
— agent script —       every order ends in request_authorization
```

Run it after regenerating `src/data/engine-output.js`. If it fails, the demo is telling a
story the engine no longer supports.

## Live mode

The **Live** switch in the header lights up when something answers
`GET http://127.0.0.1:8000/healthz`, and names what that engine is pointed at — **Live
sandbox** for the organizers' API, **Live engine** for the offline replica. It asks again when
pressed and whenever the window regains focus, so the engine can be started after the page is
open; with none running, pressing it says what to start:

```bash
cd ../wallet-control-layer && make serve-live   # the organizers' sandbox (needs TEAM_API_KEY in .env)
cd ../wallet-control-layer && make serve        # or the offline replica
```

`src/adapters/http.js` maps every call to an endpoint in
`../wallet-control-layer/specs/service-contract.md` — scenarios, compile, create, confirm,
run, decide, step-up resolve, **amend**, **preferences**, **candidates**, revoke, audit.

The one real difference from replay: the mock hands out events *before* they are judged,
because it owns the queue. Here the service owns it — its own loop long-polls upstream,
decides and submits without asking the UI — so `nextEvent` polls `/v1/decisions` for the next
authorization this run has decided and reconstructs the request from the facts the decision
carries, including what the engine derived (prior orders, lookalikes, hostile spans), so a
live order renders with the same evidence as a replayed one.

### Against the organizers' sandbox

Rehearsed end to end on 2026-09-24 against the real API: SCEN0001, all ten decisions
identical to the regression baseline, one step-up answered on the phone and one left to
expire. What the real platform does — and, since the same day, the offline replica too — and
what the demo does about it:

- **Picking a scenario confirms the mandate; *Run agent* starts the run.** The platform's
  clock starts with the run, so creating it on scenario pick would let the service decide
  ahead unwatched — and a step-up nobody is looking at yet expires after 120 s. The mandate
  sent is the IR the mandate card shows, so what was reviewed is what is enforced.
- **A step-up holds the run.** The platform generates nothing more until the customer
  answers. The agent column pauses exactly where the phone asks — this is the product, and
  it is real here, not staged.
- **The countdown is the platform's.** It starts from the time actually left — the service
  decided in a millisecond, the screen caught up later — and ticks in real seconds whatever
  the speed control says. An answer that lands after the platform closed the window is
  refused (`409 authorization_not_pending`, the one expected error in the console) and the
  phone says the purchase was declined, because the platform declined it.
- **The progress count runs ahead of the screen.** `5 / 10` while the agent column is on
  order two means the engine has decided five and the platform is holding the run at a
  step-up. That is the engine's count, and it is the true one.
- **One live run at a time.** The queue is per team, and a step-up waiting on the customer
  holds *all* of it: measured, a second run's first order sat undelivered behind another
  run's step-up until its deadline passed and the platform declined it. So switching
  scenario or pressing *Reset* is refused while a live run is still going — let it finish,
  or revoke it. Switching back to **Replay** is always allowed; it is the fallback. A run
  left over from before a page reload is named in a toast when you switch to Live.

> **`check.mjs` covers the mock, not this.** Every assertion in it runs against
> `createMockClient`, so nothing here is exercised without a service on `:8000` and a person
> pressing the button. That is exactly what it cost: until 2026-09-21 live mode rendered the
> mandate and then did nothing at all when you pressed *Run agent*, because `nextEvent`
> returned `null` unconditionally and the loop read that as "the queue is empty". Rehearse
> live mode by hand before relying on it. The UI holds no API key and never talks to the
> organizers' sandbox; the service owns the key, the Policy IR, the ledger and the audit trail.

## Architecture

```
index.html                  shell — loads one module
serve.py                    the only way to run it (ES modules need an http origin)
check.mjs                   the consistency harness above
src/core/client.js          the LeashClient interface + check, signal and SETTING metadata
src/core/policy.js          compose layers · apply a setting · classify the edit
src/core/provenance.js      where a rule came from, typed
src/core/store.js           ~60-line reactive store
src/core/format.js          money, time, and an escaping `html` tag
src/adapters/mock.js        replays real engine output; owns the ledger and the policy layers
src/adapters/recheck.js     re-runs the editable checks after an edit, in the engine's wording
src/adapters/http.js        the same interface against the real service
src/data/engine-output.js   generated — 45 decisions with full check results
src/data/profiles.js        the five cardholders, copied from the pack (check.mjs guards it)
src/data/agent-script.js    derives the agent's tool calls from an order's facts
src/ui/theme.css            design tokens, sampled from one-digitalservice.ch
src/ui/app.css              components
src/ui/settings-sheet.js    one setting: the control, the blast radius, the cost
src/ui/prefs-screen.js      the standing layer and the profile's proposals
src/ui/{agent,leash,phone}-panel.js, audit.js, icons.js
src/ui/app.js               the run loop — the only file that knows about timing
```

The UI imports `core/client.js` and nothing below it. Swapping the adapter is the whole
migration path from demo to product: the panels take data and render it, and none of them
knows whether a decision came from a fixture or from an HTTP call.

## Regenerating the data

`src/data/engine-output.js` is generated from the real engine, offline:

```bash
cd ../playground && uv run --project ../wallet-control-layer python tools/generate_data.py
```

then copy `assets/data.js` here and change the first assignment to
`export const ENGINE_OUTPUT = `. If the engine's behaviour changes, this demo is wrong until
that is done.

## Design

Palette and type sampled from **one-digitalservice.ch**: brand orange `#f69f29`, sand
`#fbd591`, ink `#1a1a16`, body `#4c4c4c`, surfaces `#f6f6f6` / `#f2f2f7`, link `#007aff`,
error `#ba0909`, Roboto, 5 / 8 / 24 / 34px radii. Approve and decline borrow the same
restraint; **step-up is the brand orange**, because being asked is the product, not an error.
