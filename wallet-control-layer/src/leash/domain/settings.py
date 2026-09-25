"""The catalogue of settings a customer may edit — and the vocabularies they draw on.

One table, four consumers:

  * ``compile/contract.py``  — the guard rails over what a model may write into an IR
  * ``domain/compose.py``    — how two layers of policy merge
  * ``domain/amend.py``      — whether an edit narrows or widens the mandate
  * ``GET /v1/config``       — what the UI is allowed to offer, asserted by leash-demo's
                               ``check.mjs`` against this list

The governing rule is specs/customer-settings.md §7, and it is the reason this file exists
rather than four hand-kept copies: **a setting no check reads is not "unsupported", it is
inert.** It renders on the customer's screen as an enforced rule and enforces nothing. So
every entry below names the check that reads it, and a setting cannot be offered without one.

Pure data. No I/O, no framework (AGENTS.md §3.2).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Facet kinds the checks in domain/checks/ actually read.
FACET_KINDS: frozenset[str] = frozenset(
    {
        "item_identity",
        "item_attribute",
        "merchant_familiarity",
        "merchant_type",
        "order_terms",
        "no_additions",
        "category_exclusion",
        "spending_hours",
    }
)

# Per kind, the `require` keys the checks read. Same argument as above, one level down.
FACET_REQUIRE_KEYS: dict[str, frozenset[str]] = {
    "item_identity": frozenset({"item_category_in", "item_keywords_all", "item_description"}),
    "item_attribute": frozenset({"size"}),
    "merchant_familiarity": frozenset({"prior_approvals_min"}),
    "merchant_type": frozenset({"merchant_category_in"}),
    "order_terms": frozenset({"return_window_days_min", "cancellable"}),
    "no_additions": frozenset(),
    "category_exclusion": frozenset(
        {"item_category_not_in", "merchant_category_not_in", "item_keywords_none"}
    ),
    "spending_hours": frozenset({"hours_from", "hours_to", "weekdays"}),
}

# Facets that describe *this errand's object* rather than the customer. A standing preference
# saying "size 43" is a category error: it would silently mean the wrong thing on the next
# purchase. specs/customer-settings.md §3.2 — refused at the boundary, not merged.
TASK_ONLY_KINDS: frozenset[str] = frozenset({"item_identity", "item_attribute"})

# Requirement keys only an instruction sets. The settings controls do not edit them, because
# the "Minimum return window" control has no refundable switch. So an edit made through such a
# control must keep them: `preferences.apply_settings` carries them over, and
# `amend.classify` compares them like any other key. Otherwise changing the return window
# would silently drop "refundable rate only" (specs/customer-settings.md §6, invariant I3).
INSTRUCTION_ONLY_KEYS: dict[str, frozenset[str]] = {
    "order_terms": frozenset({"cancellable"}),
    "category_exclusion": frozenset({"item_keywords_none"}),
    "spending_hours": frozenset({"weekdays"}),
}

# The repo pack's closed vocabularies, from items.csv and merchants.csv. Held as constants so
# this module stays pure; tests/unit/test_compile_contract.py re-reads the CSVs and fails if
# the repo pack ever disagrees, so they cannot drift silently. They are the default, not the
# whole story: a loaded pack may add to them (`register_categories` below).
ITEM_CATEGORIES: frozenset[str] = frozenset(
    {
        "books",
        "clothing",
        "cosmetics",
        "dining",
        "electronics",
        "food_delivery",
        "fuel",
        "gift_card",
        "groceries",
        "home_improvement",
        "hotel",
        "household",
        "membership",
        "sporting_goods",
        "subscriptions",
        "transport",
    }
)
MERCHANT_CATEGORIES: frozenset[str] = frozenset(
    {
        "books",
        "cash_withdrawal",
        "clothing",
        "dining",
        "electronics",
        "entertainment",
        "food_delivery",
        "fuel",
        "groceries",
        "health",
        "home_improvement",
        "hotel",
        "household",
        "kids_family",
        "pet_care",
        "photography",
        "software",
        "sporting_goods",
        "subscriptions",
        "sustainable_goods",
        "transport",
        "travel",
    }
)

# The vocabularies in force: the repo pack's, plus every category a loaded pack brought. The
# live API serves its own pack (adapters/livepack.py), and on 2026-09-24 it filed the SCEN0122
# camera lens under `photography` and flights under `travel` — item categories the repo pack
# never used. A guard that knew only the constants dropped them, and the lens failed its own
# identity check. `DataPack.load` registers what it read; this module still reads no file.
#
# Registration only ever adds. A pack without a repo category leaves it known but unused, which
# costs nothing, and a vocabulary that could shrink would let whichever pack loaded last decide
# what every later caller may say.
_ACTIVE: dict[str, frozenset[str]] = {"item": ITEM_CATEGORIES, "merchant": MERCHANT_CATEGORIES}


def register_categories(items: Iterable[str], merchants: Iterable[str]) -> None:
    """Add a loaded pack's item and merchant categories to the vocabularies in force."""
    _ACTIVE["item"] = _ACTIVE["item"] | frozenset(items)
    _ACTIVE["merchant"] = _ACTIVE["merchant"] | frozenset(merchants)


def item_categories() -> frozenset[str]:
    return _ACTIVE["item"]


def merchant_categories() -> frozenset[str]:
    return _ACTIVE["merchant"]


def category_vocabulary() -> dict[str, frozenset[str]]:
    """Which vocabulary polices each category-valued requirement, as of now."""
    return {
        "item_category_in": item_categories(),
        "item_category_not_in": item_categories(),
        "merchant_category_in": merchant_categories(),
        "merchant_category_not_in": merchant_categories(),
    }


CONFIDENCE_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})
UNCERTAINTY_POLICIES: frozenset[str] = frozenset({"ask", "decline", "approve"})
RULE_OPERATORS: frozenset[str] = frozenset({"<=", "<"})
RULE_FIELDS: frozenset[str] = frozenset({"billing_amount_chf"})
RULE_SCOPES: frozenset[str] = frozenset({"purchase", "period"})

#: `approve` → `ask` → `decline`. Higher is stricter; composition takes the max and an
#: amendment may only move up. Mirrors `sandbox/server.py`'s own ordering.
UNCERTAINTY_ORDER: dict[str, int] = {"approve": 0, "ask": 1, "decline": 2}

#: What "ask me more / normally / less" means, in the only units the engine has.
#:
#: These are **measured**, not chosen: `make tune-sweep` reports the board unchanged for any
#: threshold in [0.5, 2.0] and the nearest cliff at 2.5, where four step-ups become approvals.
#: Offering a free slider would imply a precision the data does not have; three positions with
#: the sweep behind them is the honest control. Lower is stricter.
STEP_UP_SENSITIVITY: dict[str, float] = {"more": 1.0, "normally": 2.0, "less": 2.5}


@dataclass(frozen=True, slots=True)
class Setting:
    """One editable control.

    ``check`` is the important field: it is the mechanical form of "nothing inert ships". If a
    check is ever renamed or removed, ``tests/unit/test_settings_catalogue.py`` fails here
    rather than the UI quietly offering a control that changes no decision.
    """

    key: str
    label: str
    #: How the UI renders it: money · integer · choice · categories · flag
    control: str
    #: Where it lands in the IR: `rule:purchase`, `rule:period`, `uncertainty_policy`,
    #: `step_up_threshold`, or `facet:<kind>`.
    target: str
    #: The check that reads it — see the module docstring.
    check: str
    #: Which direction is a tightening: lower · higher · subset · superset (a deny-list grows)
    #: · narrower (an hours window shrinks) · present · toward_decline
    tighter: str
    #: May a standing preferences layer set it? False ⇒ per-errand only (§3.2, §3.4).
    standing: bool
    help: str = ""
    options: tuple[str, ...] = ()
    #: `item` or `merchant`: the options are that vocabulary as of the call, not of import.
    vocabulary: str | None = None

    def as_dict(self) -> dict[str, Any]:
        body = {
            "key": self.key,
            "label": self.label,
            "control": self.control,
            "target": self.target,
            "check": self.check,
            "tighter": self.tighter,
            "standing": self.standing,
            "help": self.help,
        }
        options = sorted(_ACTIVE[self.vocabulary]) if self.vocabulary else list(self.options)
        if options:
            body["options"] = options
        return body


SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="per_order_limit_chf",
        label="Per-order limit",
        control="money",
        target="rule:purchase",
        check="per_order_limit",
        tighter="lower",
        standing=True,
        help="The most the agent may spend on any single order.",
    ),
    Setting(
        key="period_limit_chf",
        label="Limit across a period",
        # Not plain money: a period rule without a window cannot be enforced, so the control
        # has to capture both or it can produce a setting the engine will refuse.
        control="money_window",
        target="rule:period",
        check="period_limit",
        tighter="lower",
        # Layerable since multi-window enrichment: every window a policy names is enforced,
        # so a standing monthly ceiling and an errand's weekly one are two constraints rather
        # than a contest one of them loses (decision-rules.md §9).
        standing=True,
        help="The most the agent may spend across a rolling window.",
    ),
    Setting(
        key="uncertainty_policy",
        label="When something is unclear",
        control="choice",
        target="uncertainty_policy",
        check="evaluator.combine",
        tighter="toward_decline",
        standing=True,
        options=("ask", "decline"),
        help="What happens when a fact cannot be established. Asking is the default.",
    ),
    Setting(
        key="merchant_familiarity",
        label="Only shops I have used",
        control="integer",
        target="facet:merchant_familiarity",
        check="merchant_permitted",
        tighter="higher",
        standing=True,
        help="How many past approved purchases a shop needs before the agent may use it.",
    ),
    Setting(
        key="merchant_type",
        label="Kinds of shop allowed",
        control="categories",
        target="facet:merchant_type",
        check="merchant_type",
        tighter="subset",
        standing=True,
        help="The agent may only buy from these kinds of shop.",
        vocabulary="merchant",
    ),
    Setting(
        key="order_terms",
        label="Minimum return window",
        control="integer",
        target="facet:order_terms",
        check="order_terms",
        tighter="higher",
        standing=True,
        help="The agent may only buy orders that can be sent back within this many days.",
    ),
    Setting(
        key="no_additions",
        label="Nothing added I did not ask for",
        control="flag",
        target="facet:no_additions",
        check="unrequested_addon",
        tighter="present",
        standing=True,
        help="A basket that carries anything beyond the request is refused.",
    ),
    Setting(
        key="category_exclusion",
        label="Things I never buy",
        # Two lists in one control: the facet carries both dimensions and the check reads
        # both, so splitting them into two settings would give one facet two owners.
        control="exclusions",
        target="facet:category_exclusion",
        check="category_exclusion",
        tighter="superset",
        standing=True,
        help="Kinds of item and kinds of shop the agent may never buy, whatever the errand says.",
        vocabulary="item",
    ),
    Setting(
        key="spending_hours",
        label="Hours the agent may buy",
        control="hours",
        target="facet:spending_hours",
        check="spending_hours",
        tighter="narrower",
        standing=True,
        help=(
            "Outside these hours nothing is bought. Read off the purchase's own clock, in UTC. "
            "Days named in an instruction are read in Swiss time."
        ),
    ),
    Setting(
        key="step_up_threshold",
        label="How often to ask me",
        control="choice",
        target="step_up_threshold",
        # Read by the combination step rather than by a check, like `uncertainty_policy`.
        check="evaluator.combine",
        tighter="lower",
        standing=True,
        options=("more", "normally", "less"),
        help="How much evidence of a problem it takes before the agent stops and asks you.",
    ),
    Setting(
        key="item_identity",
        label="The item asked for",
        control="categories",
        target="facet:item_identity",
        check="item_matches_request",
        tighter="subset",
        standing=False,
        help="What this errand is for. Belongs to the errand, not to you.",
    ),
    Setting(
        key="item_attribute",
        label="The variant asked for",
        control="integer",
        target="facet:item_attribute",
        check="item_attributes",
        tighter="subset",
        standing=False,
        help="Size and other attributes of the requested item.",
    ),
)

BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}
STANDING_KEYS: frozenset[str] = frozenset(s.key for s in SETTINGS if s.standing)


def setting_for_facet(kind: str) -> Setting | None:
    """The catalogue entry that targets a facet kind, or None when nothing reads it."""
    return next((s for s in SETTINGS if s.target == f"facet:{kind}"), None)


def catalogue() -> list[dict[str, Any]]:
    """The table `GET /v1/config` publishes and the UI mirrors."""
    return [s.as_dict() for s in SETTINGS]
