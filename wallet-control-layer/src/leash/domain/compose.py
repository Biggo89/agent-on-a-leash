"""Composing the policy layers into the one policy the checks evaluate.

specs/customer-settings.md §3. Three layers, and **composition takes the tightest of each
constraint**:

    account       platform limits on the account          not editable
    preferences   standing, per-customer, survives every mandate
    mandate       compiled from one instruction, confirmed once

That single rule is what makes the feature safe. Every merge direction below narrows, so a
composed policy can never permit something the mandate alone would refuse — stated as
invariant I1 in the spec and tested over all 45 fixtures in
``tests/unit/test_compose.py``.

One exclusion, and one that was lifted:

  * **`item_identity` and `item_attribute` come only from the mandate.** They describe this
    errand's object, not the customer (§3.2).
  * **Period rules used to be mandate-only** and no longer are. While `binding_cap` kept the
    shortest window and dropped the rest, layering a monthly ceiling under a weekly one
    *deleted* the monthly one — a loosening wearing a tightening's clothes. `period_caps`
    now enforces every window (decision-rules.md §9), so a period rule composes like any
    other: adding one for a new window adds a constraint, and adding one for a window that
    already exists can only lower the cap that binds it.

Pure — no I/O, no clock, no globals — and never raises: a conflict is returned, not thrown,
because the caller (the service) needs to render both sides of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import provenance as prov
from .localtime import WEEKDAYS
from .settings import TASK_ONLY_KINDS, UNCERTAINTY_ORDER

#: Layer names, least to most specific. Order decides provenance order, nothing else —
#: every merge below is commutative, so a reordered list composes to the same policy.
LAYERS: tuple[str, ...] = ("account", "preferences", "mandate")


@dataclass(frozen=True, slots=True)
class Layer:
    """One contributor to the effective policy."""

    source: str
    hard_rules: tuple[dict[str, Any], ...] = ()
    intent_facets: tuple[dict[str, Any], ...] = ()
    uncertainty_policy: str | None = None
    step_up_threshold: float | None = None

    @classmethod
    def of(cls, source: str, policy: dict[str, Any] | None) -> Layer:
        """Build a layer from an IR or a policy dict, stamping provenance the layer owns."""
        policy = policy or {}
        rules = tuple(
            r if prov.normalise(r.get("provenance")) else prov.stamp(r, source)
            for r in policy.get("hard_rules") or policy.get("rules") or ()
            if isinstance(r, dict)
        )
        facets = tuple(
            f if prov.normalise(f.get("provenance")) else prov.stamp(f, source)
            for f in policy.get("intent_facets") or ()
            if isinstance(f, dict)
        )
        return cls(
            source=source,
            hard_rules=rules,
            intent_facets=facets,
            uncertainty_policy=policy.get("uncertainty_policy"),
            step_up_threshold=policy.get("step_up_threshold"),
        )


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two layers that cannot both be satisfied. Never silently resolved — §3.2."""

    code: str
    setting: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "setting": self.setting, "message": self.message}


@dataclass(frozen=True, slots=True)
class Composition:
    policy: dict[str, Any]
    conflicts: tuple[Conflict, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "conflicts": [c.as_dict() for c in self.conflicts],
            "notes": list(self.notes),
        }


def compose(*layers: Layer) -> Composition:
    """Merge the layers into one policy, tightest-wins, with every origin recorded."""
    notes: list[str] = []
    conflicts: list[Conflict] = []

    # Concatenate. `binding_cap` picks the tightest per purchase scope and `period_caps`
    # the tightest per window, so the merge needs no logic of its own — and both are
    # monotone, which is what makes I1 true by construction rather than by inspection.
    rules: list[dict[str, Any]] = [rule for layer in layers for rule in layer.hard_rules]

    facets, facet_conflicts, facet_notes = _merge_facets(layers)
    conflicts.extend(facet_conflicts)
    notes.extend(facet_notes)

    stated = [
        layer.uncertainty_policy
        for layer in layers
        if layer.uncertainty_policy in UNCERTAINTY_ORDER
    ]
    uncertainty = max(stated, key=lambda p: UNCERTAINTY_ORDER[p]) if stated else "ask"

    # Lower is stricter — it takes less evidence to escalate — so the min is the tightest.
    thresholds = [
        layer.step_up_threshold
        for layer in layers
        if isinstance(layer.step_up_threshold, (int, float)) and layer.step_up_threshold > 0
    ]

    policy: dict[str, Any] = {
        "hard_rules": rules,
        "intent_facets": facets,
        "uncertainty_policy": uncertainty,
    }
    if thresholds:
        policy["step_up_threshold"] = min(thresholds)

    return Composition(
        policy=policy,
        conflicts=tuple(conflicts),
        notes=tuple(notes),
    )


# ------------------------------------------------------------------ facet merging


def _merge_facets(
    layers: tuple[Layer, ...],
) -> tuple[list[dict[str, Any]], list[Conflict], list[str]]:
    """One merged facet per kind.

    ``policy.facet()`` returns the *first* facet of a kind, so emitting two of one kind would
    make the second silently inert — the same defect the compiler's `duplicate_facet` rail
    exists to prevent. Everything of one kind is therefore merged into a single facet here.
    """
    conflicts: list[Conflict] = []
    notes: list[str] = []
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    order: list[str] = []

    for layer in layers:
        for facet in layer.intent_facets:
            kind = str(facet.get("kind") or "")
            if not kind:
                continue
            if kind in TASK_ONLY_KINDS and layer.source != "mandate":
                notes.append(
                    f"setting_not_standing: {kind} describes this errand, not you; "
                    f"the {layer.source} layer's copy was not applied"
                )
                continue
            if kind not in grouped:
                grouped[kind] = []
                order.append(kind)
            grouped[kind].append((layer.source, facet))

    merged: list[dict[str, Any]] = []
    for kind in order:
        contributors = [f for _, f in grouped[kind]]
        one, conflict = _merge_one(kind, contributors)
        if conflict is not None:
            conflicts.append(conflict)
        if one is not None:
            merged.append(one)
    return merged, conflicts, notes


_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def merge_facets(
    kind: str, contributors: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, Conflict | None]:
    """Several facets of one kind as the one facet the checks read, tightest wins.

    The rules composition uses between layers, offered to the compile guard, which meets the
    same thing inside one model answer: "No flights, no insurance" written as two exclusions.
    """
    return _merge_one(kind, contributors)


def _merge_one(
    kind: str, contributors: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, Conflict | None]:
    if len(contributors) == 1:
        return contributors[0], None

    require: dict[str, Any] = {}
    conflict: Conflict | None = None

    # max — a higher bar is the tighter one
    for key in ("prior_approvals_min", "return_window_days_min"):
        values = [
            _as_int(f.get("require", {}).get(key))
            for f in contributors
            if _as_int(f.get("require", {}).get(key)) is not None
        ]
        if values:
            require[key] = max(v for v in values if v is not None)

    # a stated flag — "refundable rate only" — holds if any layer states it: a layer that does
    # not mention it has said nothing, not "non-refundable is fine".
    if any(f.get("require", {}).get("cancellable") is True for f in contributors):
        require["cancellable"] = True

    # intersection — a smaller set of permitted categories is the tighter one
    for key in ("merchant_category_in", "item_category_in"):
        sets = [
            {str(v) for v in f.get("require", {}).get(key) or ()}
            for f in contributors
            if f.get("require", {}).get(key)
        ]
        if not sets:
            continue
        common: set[str] = set.intersection(*sets)
        if not common:
            # An empty list reaches `check_merchant_type` as UNKNOWN, which routes through
            # uncertainty_policy and turns every order into a step-up the customer cannot
            # connect to the preference they just saved. Refused here instead (§3.2).
            conflict = Conflict(
                code="preference_conflict",
                setting=kind,
                message=(
                    "your preferences and this errand allow no shop in common: "
                    + " vs ".join(", ".join(sorted(s)) for s in sets)
                ),
            )
            common = set.union(*sets)  # keep the policy renderable; the caller refuses the edit
        require[key] = sorted(common)

    # union — a deny-list grows: more excluded is tighter, and an exclusion one layer states
    # is not undone by another layer that happens not to mention it.
    for key in ("item_category_not_in", "merchant_category_not_in", "item_keywords_none"):
        excluded = sorted(
            {str(v) for f in contributors for v in f.get("require", {}).get(key) or ()}
        )
        if excluded:
            require[key] = excluded

    # intersection — the permitted hours of every layer, which may be empty or unrepresentable
    if kind == "spending_hours":
        window, hours_conflict = _merge_hours(contributors)
        if hours_conflict is not None:
            conflict = hours_conflict
        if window is not None:
            require.update(window)
        # intersection — the days every layer allows. A layer that names no day allows all.
        day_sets = [
            {str(d) for d in f["require"]["weekdays"]}
            for f in contributors
            if (f.get("require") or {}).get("weekdays") is not None
        ]
        if day_sets:
            days = set.intersection(*day_sets)
            if not days:
                conflict = Conflict(
                    code="preference_conflict",
                    setting="spending_hours",
                    message="your preferences and this errand allow no day in common",
                )
                days = set.union(*day_sets)  # keep the policy renderable; the edit is refused
            require["weekdays"] = [d for d in WEEKDAYS if d in days]

    # union — every keyword any layer requires must be present
    keywords = sorted(
        {str(k) for f in contributors for k in f.get("require", {}).get("item_keywords_all") or ()}
    )
    if keywords:
        require["item_keywords_all"] = keywords

    for key in ("item_description", "size"):
        for f in contributors:
            value = f.get("require", {}).get(key)
            if value is not None:
                require.setdefault(key, value)

    confidences = [str(f.get("confidence") or "high") for f in contributors]
    facet: dict[str, Any] = {
        "kind": kind,
        "provenance": prov.merge(*contributors),
        "confidence": min(confidences, key=lambda c: _CONFIDENCE_ORDER.get(c, 2)),
    }
    # `no_additions` carries no requirements — its presence *is* the requirement, so an empty
    # `require` is correct there and misleading anywhere else.
    if require or kind != "no_additions":
        facet["require"] = require
    return facet, conflict


def _merge_hours(
    contributors: list[dict[str, Any]],
) -> tuple[dict[str, int] | None, Conflict | None]:
    """Intersect the permitted hours of every layer.

    Hours are the one setting whose tightest-wins is not a min or a max: two windows overlap
    in a set of hours, and that set has to be expressible as a single `from`/`to` pair or the
    check cannot read it.

    Two ways it fails, and neither may be resolved silently:

    * **No overlap.** "Only mornings" against "only evenings" permits nothing.
    * **An overlap in two pieces.** `22:00–07:00` against `05:00–23:00` leaves {5, 6, 22} —
      a real answer the facet shape cannot carry. Taking the narrower window instead would
      be *more* permissive than the other one at hour 23, which breaks the invariant the
      whole layer rests on, so it is refused rather than approximated.
    """
    windows = []
    for f in contributors:
        start = _as_int(f.get("require", {}).get("hours_from"))
        end = _as_int(f.get("require", {}).get("hours_to"))
        if (
            start is None
            or end is None
            or start == end
            or not (0 <= start <= 23 and 0 <= end <= 23)
        ):
            continue
        windows.append(
            set(range(start, end)) if start < end else set(range(start, 24)) | set(range(end))
        )
    if not windows:
        return None, None

    permitted = set.intersection(*windows)
    if not permitted:
        return None, Conflict(
            code="preference_conflict",
            setting="spending_hours",
            message="your preferences and this errand allow no hour in common",
        )

    run = _contiguous_run(permitted)
    if run is None:
        return None, Conflict(
            code="preference_conflict",
            setting="spending_hours",
            message=(
                "the hours you allow overlap in two separate stretches ("
                + ", ".join(f"{h:02d}:00" for h in sorted(permitted))
                + "), which a single window cannot express"
            ),
        )
    return {"hours_from": run[0], "hours_to": run[1]}, None


def _contiguous_run(hours: set[int]) -> tuple[int, int] | None:
    """`hours` as one half-open `(from, to)` window, wrapping allowed. None when it is not one."""
    if len(hours) == 24:
        return None  # every hour: not expressible as from != to, and not a restriction anyway
    for start in sorted(hours):
        if (start - 1) % 24 in hours:
            continue  # not the beginning of a run
        length = 0
        while (start + length) % 24 in hours:
            length += 1
        if length == len(hours):
            return start, (start + length) % 24
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
