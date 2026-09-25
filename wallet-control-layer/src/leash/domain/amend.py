"""Does this edit narrow the mandate, or widen it?

specs/customer-settings.md §6. The brief asks that a customer be able to *"tighten, update, or
revoke"* their wallet policy, and those are not one operation:

    tighten   applies immediately, add-only, no new consent moment
    widen     is not a patch at all — it mints a draft the customer confirms
    conflict  cannot be applied by anyone; both sides are named back to them

Classification is where that decision is made, and it is deliberately one pure, total
function. Two properties matter more than completeness:

  * **Uncertainty widens** (invariant I3). Anything this cannot *prove* narrows the mandate is
    classified `widen` and costs a confirmation. A false `widen` costs one tap; a false
    `tighten` spends money on a guess. The same asymmetry as `policy-ir.md` rule 3, the
    compiler's safety floor, and `decision-rules.md` §5.
  * **Period rules are compared window by window.** Every window a policy names is enforced
    (`decision-rules.md` §9), so a lower cap on one window is a tightening and dropping a
    window is a widening — but a change of *window* is neither: `CHF 500 / 7 days` does not
    replace `CHF 1000 / 30 days`, it stands beside it. Before multi-window enrichment the
    shortest window displaced the rest and every period edit had to be called a widening,
    because a "tightening" could delete a ceiling.

Pure. No I/O, no clock, no globals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .localtime import WEEKDAYS
from .policy import binding_cap, facet, period_caps, requirement
from .settings import BY_KEY, UNCERTAINTY_ORDER

TIGHTEN = "tighten"
WIDEN = "widen"
CONFLICT = "conflict"
NOOP = "noop"

#: `conflict` outranks `widen` outranks `tighten`: one widening field makes the whole
#: amendment a widening, because they are applied together or not at all.
_RANK = {NOOP: 0, TIGHTEN: 1, WIDEN: 2, CONFLICT: 3}


@dataclass(frozen=True, slots=True)
class FieldChange:
    setting: str
    before: str | None
    after: str | None
    kind: str
    why: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "setting": self.setting,
            "label": BY_KEY[self.setting].label if self.setting in BY_KEY else self.setting,
            "before": self.before,
            "after": self.after,
            "kind": self.kind,
            "why": self.why,
        }


@dataclass(frozen=True, slots=True)
class Amendment:
    kind: str
    changes: tuple[FieldChange, ...] = ()

    @property
    def applies_immediately(self) -> bool:
        """A tightening needs no new consent: it can only reduce what the agent may do."""
        return self.kind in (TIGHTEN, NOOP)

    @property
    def conflicts(self) -> tuple[FieldChange, ...]:
        return tuple(c for c in self.changes if c.kind == CONFLICT)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "changes": [c.as_dict() for c in self.changes]}


def classify(current: dict[str, Any], proposed: dict[str, Any]) -> Amendment:
    """Compare two policies (or two IRs) and say what the move costs."""
    changes: list[FieldChange] = []
    changes.extend(_caps(current, proposed))
    changes.extend(_uncertainty(current, proposed))
    changes.extend(_sensitivity(current, proposed))
    changes.extend(_facets(current, proposed))

    kind = NOOP
    for change in changes:
        if _RANK[change.kind] > _RANK[kind]:
            kind = change.kind
    return Amendment(kind=kind, changes=tuple(changes))


def _rules(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return list(policy.get("hard_rules") or policy.get("rules") or ())


# ------------------------------------------------------------------ money


def _caps(current: dict[str, Any], proposed: dict[str, Any]) -> list[FieldChange]:
    out: list[FieldChange] = []
    before_rules, after_rules = _rules(current), _rules(proposed)

    before = binding_cap(before_rules, "purchase")
    after = binding_cap(after_rules, "purchase")
    if (before is None) != (after is None):
        out.append(
            FieldChange(
                setting="per_order_limit_chf",
                before=_cap_text(before),
                after=_cap_text(after),
                kind=TIGHTEN if before is None else WIDEN,
                why=(
                    "a per-order limit where there was none"
                    if before is None
                    else "removing the per-order limit lets the agent spend without a ceiling"
                ),
            )
        )
    elif before is not None and after is not None:
        lhs, rhs = _value(before[0]), _value(after[0])
        if rhs < lhs:
            out.append(
                FieldChange(
                    "per_order_limit_chf",
                    _cap_text(before),
                    _cap_text(after),
                    TIGHTEN,
                    "a lower per-order limit",
                )
            )
        elif rhs > lhs:
            out.append(
                FieldChange(
                    "per_order_limit_chf",
                    _cap_text(before),
                    _cap_text(after),
                    WIDEN,
                    "a higher per-order limit lets the agent spend more on one order",
                )
            )

    out.extend(_period(before_rules, after_rules))
    return out


def _period(
    before_rules: list[dict[str, Any]], after_rules: list[dict[str, Any]]
) -> list[FieldChange]:
    """Compare window by window — every one of them is enforced (decision-rules.md §9).

    A window dropped is a ceiling removed, whatever happened to the others: a customer who
    lowers their weekly cap and loses their monthly one has not tightened anything. And a
    change of *window* is neither direction on its own — `CHF 500 / 7 days` does not replace
    `CHF 1000 / 30 days`, it stands beside it.
    """
    before, after = _by_window(before_rules), _by_window(after_rules)
    if before == after:
        return []

    # A rule the engine cannot enforce is `unknown` at decision time, which routes through
    # `uncertainty_policy`. We cannot prove that narrows the mandate, so I3 applies.
    if None in before or None in after:
        return [
            FieldChange(
                "period_limit_chf",
                _window_text(before),
                _window_text(after),
                WIDEN,
                "a period limit without a window cannot be enforced, so this needs confirming",
            )
        ]

    out: list[FieldChange] = []
    for days in sorted(set(before) | set(after), key=lambda d: d or 0):
        was, now = before.get(days), after.get(days)
        if was == now:
            continue
        if now is None:
            out.append(
                FieldChange(
                    "period_limit_chf",
                    f"CHF {was:.2f} / {days} days",
                    "not set",
                    WIDEN,
                    f"dropping the {days}-day ceiling you agreed to",
                )
            )
        elif was is None:
            out.append(
                FieldChange(
                    "period_limit_chf",
                    "not set",
                    f"CHF {now:.2f} / {days} days",
                    TIGHTEN,
                    f"a {days}-day ceiling where there was none",
                )
            )
        else:
            tighter = now < was
            out.append(
                FieldChange(
                    "period_limit_chf",
                    f"CHF {was:.2f} / {days} days",
                    f"CHF {now:.2f} / {days} days",
                    TIGHTEN if tighter else WIDEN,
                    f"a {'lower' if tighter else 'higher'} ceiling across {days} days",
                )
            )
    return out


def _by_window(rules: list[dict[str, Any]]) -> dict[int | None, float]:
    """The binding cap for each window this policy states. `None` keys an unusable rule."""
    return {days: _value(rule) for days, rule in period_caps(rules)}


def _window_text(windows: dict[int | None, float]) -> str:
    if not windows:
        return "not set"
    return ", ".join(
        f"CHF {value:.2f} / {days} days" if days else f"CHF {value:.2f} / no window"
        for days, value in sorted(windows.items(), key=lambda kv: kv[0] or 0)
    )


def _cap_text(found: tuple[dict[str, Any], ...] | None) -> str | None:
    if found is None:
        return None
    rule = found[0]
    days = rule.get("period_days")
    value = f"CHF {_value(rule):.2f}"
    return f"{value} / {int(days)} days" if days else value


def _value(rule: dict[str, Any]) -> float:
    try:
        return float(rule["value"])
    except (KeyError, TypeError, ValueError):
        return float("inf")


# ------------------------------------------------------------------ uncertainty


def _uncertainty(current: dict[str, Any], proposed: dict[str, Any]) -> list[FieldChange]:
    before = str(current.get("uncertainty_policy") or "ask")
    after = str(proposed.get("uncertainty_policy") or "ask")
    if before == after:
        return []
    lhs = UNCERTAINTY_ORDER.get(before, 1)
    rhs = UNCERTAINTY_ORDER.get(after, 1)
    return [
        FieldChange(
            setting="uncertainty_policy",
            before=before,
            after=after,
            kind=TIGHTEN if rhs > lhs else WIDEN,
            why=(
                "asking or refusing more often when a fact cannot be established"
                if rhs > lhs
                else "resolving unclear cases in the agent's favour more often"
            ),
        )
    ]


def _sensitivity(current: dict[str, Any], proposed: dict[str, Any]) -> list[FieldChange]:
    """How much evidence it takes to interrupt. Lower is stricter — it escalates sooner."""
    before = current.get("step_up_threshold")
    after = proposed.get("step_up_threshold")
    if before == after:
        return []
    lo, hi = _float(before), _float(after)
    if lo is None or hi is None:
        # One side unset means the global default, which is not a number the customer chose.
        # Setting one is a tightening only when it is stricter than the default.
        from .evaluator import STEP_UP_THRESHOLD

        lo = lo if lo is not None else STEP_UP_THRESHOLD
        hi = hi if hi is not None else STEP_UP_THRESHOLD
    if lo == hi:
        return []
    tighter = hi < lo
    return [
        FieldChange(
            setting="step_up_threshold",
            before=_sensitivity_word(lo),
            after=_sensitivity_word(hi),
            kind=TIGHTEN if tighter else WIDEN,
            why=(
                "stopping to ask on weaker evidence"
                if tighter
                else "letting more through without asking you"
            ),
        )
    ]


def _sensitivity_word(value: float) -> str:
    from .settings import STEP_UP_SENSITIVITY

    for word, threshold in STEP_UP_SENSITIVITY.items():
        if threshold == value:
            return word
    return f"{value:g}"


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ facets

#: How each facet's requirement is compared: `(require key, direction)`.
#:
#: `None` means presence alone is the requirement. A `None` key with direction `hours` means
#: the comparison needs the whole facet rather than one value — a window is a set, not a
#: number, and "different" is not "narrower".
_FACET_KEYS: dict[str, tuple[str | None, str] | None] = {
    "merchant_familiarity": ("prior_approvals_min", "higher"),
    "order_terms": ("return_window_days_min", "higher"),
    "merchant_type": ("merchant_category_in", "subset"),
    "item_identity": ("item_category_in", "subset"),
    "item_attribute": ("size", "exact"),
    "no_additions": None,
    # A deny-list is the one requirement that tightens by GROWING: more excluded is narrower.
    "category_exclusion": ("item_category_not_in", "superset"),
    "spending_hours": (None, "hours"),
}

#: Requirement keys beyond the one above that a facet can carry, compared the same way. An edit
#: that drops "refundable rate only" while leaving the return window alone is still a widening
#: (I3); comparing one key per facet would have read it as no change at all.
_EXTRA_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "order_terms": (("cancellable", "flag"),),
    "category_exclusion": (
        ("merchant_category_not_in", "superset"),
        ("item_keywords_none", "superset"),
    ),
    "spending_hours": (("weekdays", "days"),),
}


def _facets(current: dict[str, Any], proposed: dict[str, Any]) -> list[FieldChange]:
    out: list[FieldChange] = []
    for kind, comparison in _FACET_KEYS.items():
        before, after = facet(current, kind), facet(proposed, kind)
        if before is None and after is None:
            continue
        # An empty allow-list is never a tightening, however it arrived. It reaches the check
        # as UNKNOWN, routes through `uncertainty_policy`, and turns every order into a
        # step-up the customer cannot connect to the setting they just cleared — so it is
        # refused here, where both sides can still be named back to them (§3.2).
        allow_key = comparison[0] if comparison and comparison[1] == "subset" else None
        if after is not None and allow_key is not None:
            proposed_values = requirement(after, allow_key)
            if isinstance(proposed_values, (list, tuple)) and not proposed_values:
                out.append(
                    FieldChange(
                        kind,
                        _facet_text(before, comparison),
                        "nothing",
                        CONFLICT,
                        "nothing would be allowed at all",
                    )
                )
                continue
        if before is None:
            out.append(
                FieldChange(kind, None, _facet_text(after, comparison), TIGHTEN, "a new rule")
            )
            continue
        if after is None:
            out.append(
                FieldChange(
                    kind,
                    _facet_text(before, comparison),
                    None,
                    WIDEN,
                    "dropping a rule you agreed to",
                )
            )
            continue
        if comparison is None:
            continue  # both present, presence is the whole requirement
        key, direction = comparison
        if direction == "hours" or key is None:
            change = _compare_hours(kind, before, after)
        else:
            change = _compare(kind, before, after, key, direction)
        if change is not None:
            out.append(change)
        for extra_key, extra_direction in _EXTRA_KEYS.get(kind, ()):
            extra = _compare(kind, before, after, extra_key, extra_direction)
            if extra is not None:
                out.append(extra)
    return out


def _compare(
    kind: str, before: dict[str, Any], after: dict[str, Any], key: str, direction: str
) -> FieldChange | None:
    lhs, rhs = requirement(before, key), requirement(after, key)
    text = (_render(lhs), _render(rhs))
    if lhs == rhs:
        return None

    if direction == "higher":
        lo, hi = _int(lhs), _int(rhs)
        if lo is None or hi is None:
            return FieldChange(kind, *text, WIDEN, "the new value could not be compared")
        return FieldChange(
            kind, *text, TIGHTEN if hi > lo else WIDEN, "a higher bar" if hi > lo else "a lower bar"
        )

    if direction == "days":
        # A facet that names no day allows every day, so "not set" is all seven, not none.
        old = {str(v) for v in lhs} if lhs is not None else set(WEEKDAYS)
        new = {str(v) for v in rhs} if rhs is not None else set(WEEKDAYS)
        if old == new:
            return None
        if not new:
            return FieldChange(kind, *text, CONFLICT, "no day would be allowed at all")
        if new < old:
            return FieldChange(kind, *text, TIGHTEN, "fewer days")
        if new > old:
            return FieldChange(kind, *text, WIDEN, "more days")
        return FieldChange(kind, *text, WIDEN, "days were added as well as removed")

    if direction == "flag":
        # A true flag is the requirement: gaining one narrows, losing one widens.
        if rhs is True and lhs is not True:
            return FieldChange(kind, *text, TIGHTEN, "a new requirement")
        if lhs is True and rhs is not True:
            return FieldChange(kind, *text, WIDEN, "a requirement was removed")
        return None

    if direction == "superset":
        old = {str(v) for v in lhs or ()}
        new = {str(v) for v in rhs or ()}
        if new > old:
            return FieldChange(kind, *text, TIGHTEN, "more excluded")
        if new < old:
            return FieldChange(kind, *text, WIDEN, "fewer things excluded")
        # Traded one exclusion for another: something the customer ruled out is allowed again.
        return FieldChange(kind, *text, WIDEN, "exclusions were removed as well as added")

    if direction == "subset":
        old = {str(v) for v in lhs or ()}
        new = {str(v) for v in rhs or ()}
        if not new:
            return FieldChange(kind, *text, CONFLICT, "nothing would be allowed at all")
        if new < old:
            return FieldChange(kind, *text, TIGHTEN, "fewer options allowed")
        if new > old:
            return FieldChange(kind, *text, WIDEN, "more options allowed")
        # Overlapping but neither a subset nor a superset: some options were traded for
        # others, so the mandate permits something it did not before. I3 — widen.
        return FieldChange(kind, *text, WIDEN, "options were added as well as removed")

    # `exact` and anything unrecognised: we cannot prove this narrows the mandate.
    return FieldChange(kind, *text, WIDEN, "a different requirement, not a narrower one")


def _compare_hours(kind: str, before: dict[str, Any], after: dict[str, Any]) -> FieldChange | None:
    """A window narrows or it does not — and "different" is not "narrower".

    Hours are a set, not a number, so the only tightening is a strict subset of the hours
    already permitted. Shifting a window trades hours the customer forbade for hours they
    allowed, which permits something it did not before. I3 — that widens.
    """
    text = (_hours_text(before), _hours_text(after))
    # A facet may state only days ("never at the weekend"). No hours on either side is no
    # change in hours; gaining a window narrows, losing one widens.
    had, has = _states_hours(before), _states_hours(after)
    if not had and not has:
        return None
    if not had:
        return FieldChange(kind, *text, TIGHTEN, "a new window")
    if not has:
        return FieldChange(kind, *text, WIDEN, "the window was removed")
    old = _hour_set(before)
    new = _hour_set(after)
    if old is None or new is None:
        return FieldChange(kind, *text, WIDEN, "the hours could not be compared")
    if old == new:
        return None
    if new < old:
        return FieldChange(kind, *text, TIGHTEN, "a shorter window")
    if new > old:
        return FieldChange(kind, *text, WIDEN, "a longer window")
    return FieldChange(kind, *text, WIDEN, "hours were added as well as removed")


def _states_hours(facet_obj: dict[str, Any]) -> bool:
    return any(requirement(facet_obj, key) is not None for key in ("hours_from", "hours_to"))


def _hour_set(facet_obj: dict[str, Any]) -> set[int] | None:
    start, end = (
        _int(requirement(facet_obj, "hours_from")),
        _int(requirement(facet_obj, "hours_to")),
    )
    if start is None or end is None or start == end:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None
    return set(range(start, end)) if start < end else set(range(start, 24)) | set(range(end))


def _hours_text(facet_obj: dict[str, Any] | None) -> str:
    if facet_obj is None:
        return "not set"
    start, end = requirement(facet_obj, "hours_from"), requirement(facet_obj, "hours_to")
    if start is None or end is None:
        return "not set"
    return f"{int(start):02d}:00-{int(end):02d}:00"


def _facet_text(facet_obj: dict[str, Any] | None, comparison: tuple[str | None, str] | None) -> str:
    if facet_obj is None:
        return "not set"
    if comparison is None:
        return "on"
    key, direction = comparison
    if direction == "hours" or key is None:
        return _hours_text(facet_obj)
    return _render(requirement(facet_obj, key))


def _render(value: Any) -> str:
    if value is None:
        return "not set"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "nothing"
    return str(value)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
