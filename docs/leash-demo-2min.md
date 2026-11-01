# Agent on a Leash: 2-minute live demo script

About 290 spoken words, which is two minutes at a calm pace. Keep the demo running while you talk: each block says what to have on screen. The full version is [leash-demo-talk-track.md](leash-demo-talk-track.md).

**Before you start:** Live on, every scenario loaded once (so nothing waits on the model), rules showing `llm:…`.

---

### 0:00 · Why this exists
*On screen: the three columns, Agent · The Leash · You.*

> AI agents are starting to shop for people and pay with the customer's card. The question for a card issuer is simple: **when an agent wants to spend a customer's money, who says yes?** We built the answer. The leash between the agent and the card.

### 0:15 · Words become rules
*Groceries scenario. Hover a rule so its quoted words light up.*

> The customer writes what they want, in their own words. AI turns that into rules, and **every rule quotes the words it came from.** The customer confirms, and from that moment **there is no AI anywhere.** Every payment runs through sixteen fixed checks and one fixed rule: a breach declines, a doubt asks the customer, everything else goes through.

### 0:45 · An ordinary order
*`space` to run. Stop on AU0002, then let it reach AU0011.*

> Here the agent shops. An ordinary grocery order: **approved** in about a millisecond, with one sentence saying why. Watch the weekly budget: it goes back down as older orders age out, so a normal delivery isn't blocked by a simple running total.

### 1:05 · A seller tries to manipulate the agent
*Manipulated agent scenario, AU0037.*

> This seller hid a message for AI agents, claiming the customer pre-approved 900 francs. We flag it, and it changes nothing: 520 is over the 400 limit, **declined.** No AI in the decision means nothing to talk into a yes.

### 1:25 · The customer stays in charge
*Monitor scenario. Answer the step-up with `a` or `d`, drag Tighten to CHF 250 and apply, then revoke.*

> When we're unsure, the customer gets the question on their phone. They can tighten the limit at any time, and the system shows what will change before they apply it. Loosening needs a new confirmation. **And revoke stops the agent immediately.**

### 1:50 · Close
*Back to the full board.*

> Forty-five test payments: 17 approved, 20 declined, 8 asked, each with a reason the customer can read and a record that is never rewritten. And even when an AI wrote the policies, **not one decision changed.**

---

**If short on time, cut 0:45 first.** Let the order approve on screen without comment. Keep the last line of the close: it's the one that explains the architecture.

**Tighten before revoking.** A revoked mandate can't be edited, and the tighten will do nothing.
