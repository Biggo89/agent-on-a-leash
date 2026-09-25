"""Policy IR accessors.

The live event carries only the mandate's decision-bearing snapshot — `hard_rules`,
`instruction`, `uncertainty_policy` and the identity fields. Everything the API's small rule
format cannot express (item identity, retailer type, return terms, merchant familiarity) lives
in our Policy IR as `intent_facets`, which the engine holds locally against the mandate it
compiled. See specs/policy-ir.md.
"""

from __future__ import annotations

from typing import Any


def facet(policy: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """Return the first intent facet of ``kind``, or None when the policy states none."""
    for candidate in policy.get("intent_facets") or ():
        if isinstance(candidate, dict) and candidate.get("kind") == kind:
            return candidate
    return None


def requirement(facet_obj: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    """Read one requirement out of a facet, tolerating a missing `require` block."""
    if not facet_obj:
        return default
    value = (facet_obj.get("require") or {}).get(key, default)
    return default if value is None else value


def binding_cap(
    hard_rules: list[dict[str, Any]] | None, scope: str
) -> tuple[dict[str, Any], ...] | None:
    """The rule that binds one scope: the **tightest** matching cap, never the first.

    specs/decision-rules.md §9. `hard_rules` is a list and `PATCH` is tighten-only, so a
    customer who narrows their mandate adds a rule beside the old one rather than editing it.
    Taking the first match would answer with the cap they replaced, and would make the
    decision depend on list order — which §7's determinism requirement forbids.

    Returns a one-tuple holding the binding rule, or None when the scope carries no cap. (A
    tuple rather than the bare dict so callers cannot confuse "no cap" with a falsy rule.)

    **For `scope="period"` use `period_caps` instead.** Period rules naming different windows
    are not comparable by value — 7 days alone permits 1200 in a month, 30 days alone permits
    1000 in an afternoon — so there is no single binding period rule, and a function that
    returned one would be answering a question with no answer.
    """
    candidates = [
        r
        for r in (hard_rules or ())
        if isinstance(r, dict)
        and r.get("field") == "billing_amount_chf"
        and r.get("scope") == scope
        and r.get("operator") in ("<=", "<")
    ]
    if not candidates:
        return None
    # Ties on value break toward "<", which excludes the boundary and is the tighter of the two.
    return (min(candidates, key=lambda r: (_value(r), 0 if r.get("operator") == "<" else 1)),)


def period_caps(
    hard_rules: list[dict[str, Any]] | None,
) -> tuple[tuple[int | None, dict[str, Any]], ...]:
    """Every period constraint the policy states: one `(window, binding rule)` per window.

    specs/decision-rules.md §9. **All of them are enforced.** Within a window the tightest cap
    binds, exactly as for `purchase`; across windows nothing binds anything, because neither
    is strictly safer than the other.

    Ordered by window ascending, with rules carrying **no usable window last** and paired with
    `None`. Those are not dropped: a rule the engine cannot enforce has to reach the check so
    it can be reported as `unknown`, rather than vanish and leave the customer believing a
    limit is in force. Ascending order is what makes the result list deterministic whatever
    order the rules arrived in (§7).
    """
    candidates = [
        r
        for r in (hard_rules or ())
        if isinstance(r, dict)
        and r.get("field") == "billing_amount_chf"
        and r.get("scope") == "period"
        and r.get("operator") in ("<=", "<")
    ]
    by_window: dict[int, list[dict[str, Any]]] = {}
    unusable: list[dict[str, Any]] = []
    for rule in candidates:
        days = _window(rule)
        if days > 0:
            by_window.setdefault(days, []).append(rule)
        else:
            unusable.append(rule)

    out: list[tuple[int | None, dict[str, Any]]] = [
        (
            days,
            min(
                by_window[days],
                key=lambda r: (_value(r), 0 if r.get("operator") == "<" else 1),
            ),
        )
        for days in sorted(by_window)
    ]
    out.extend((None, rule) for rule in unusable)
    return tuple(out)


def period_windows(hard_rules: list[dict[str, Any]] | None) -> tuple[int, ...]:
    """The windows enrichment must compute, ascending. One figure per entry (§4).

    Enrichment computes the set; the check looks up the window belonging to the rule it is
    evaluating. A window missing from the set is `unknown`, never zero — which is the guard
    that replaced "enrichment and the check must make the same selection". There is no
    selection left to disagree about, only a lookup that either succeeds or admits it did not.
    """
    return tuple(days for days, _ in period_caps(hard_rules) if days is not None)


def _value(rule: dict[str, Any]) -> float:
    try:
        return float(rule["value"])
    except (KeyError, TypeError, ValueError):
        # A malformed cap must not win the min() and silently become the binding limit.
        return float("inf")


def _window(rule: dict[str, Any]) -> int:
    """A rule's window in days. Zero means "not a window" — see `period_caps`."""
    try:
        days = int(rule.get("period_days") or 0)
    except (TypeError, ValueError):
        return 0
    return days if days > 0 else 0
