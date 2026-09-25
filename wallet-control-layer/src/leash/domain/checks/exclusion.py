"""Standing exclusions: what the customer does not buy, and when they do not buy it.

Two checks that exist for the **standing preferences layer** rather than for any one errand
(specs/customer-settings.md §2). Both are gated on a facet the customer stated, so neither
touches a mandate that never mentioned them — over-blocking is a failure mode, not a safe
default (AGENTS.md §0.7).

    category_exclusion   "I do not buy gift vouchers"          specs/check-category-exclusion.md
    spending_hours       "Nothing between 23:00 and 07:00"     specs/check-spending-hours.md

Both read **trusted** fields only — `item_category` and `merchant_category` come from the
platform's records, and the timestamp from the event envelope. Nothing here reads merchant
free text, so there is nothing for a seller to reword their way past
(specs/decision-rules.md §3).
"""

from __future__ import annotations

from typing import Any

from ..localtime import DAY_NAMES, WEEKDAYS, ZONE, swiss_time
from ..policy import facet, requirement
from ..sanitize import safe_display
from ..types import CheckResult, EnrichedEvent, Evidence, Verdict
from .item import name_tokens

_MAX_HOUR = 23


def _readable(category: str) -> str:
    return str(category).replace("_", " ")


def _readable_list(categories: list[str]) -> str:
    names = [_readable(c) for c in categories]
    if len(names) == 1:
        return names[0]
    return " or ".join([", ".join(names[:-1]), names[-1]])


def _unknown(check_id: str, detail: str, evidence: tuple[Evidence, ...] = ()) -> CheckResult:
    """An unestablished fact never resolves in the agent's favour (decision-rules.md §5)."""
    return CheckResult(check_id, Verdict.UNKNOWN, "insufficient_evidence", evidence, detail)


# ------------------------------------------------------------------ category exclusion


def check_category_exclusion(
    ev: EnrichedEvent, policy: dict[str, Any]
) -> CheckResult | tuple[CheckResult, ...]:
    """A deny-list over shop kinds and item kinds — specs/check-category-exclusion.md.

    `merchant_type` is an allow-list, and the complement of "no gift cards" is the other
    twenty-one categories: absurd to write, and wrong the moment the vocabulary grows a
    category the customer never considered. A deny-list is the only shape under which
    *"everything except this"* stays correct as the world changes.

    **One result per dimension the facet states**, merchant first. A cart that is both from an
    excluded shop *and* carries an excluded item is two findings, and reporting them as one
    would hide half of it. Fixed order keeps the list deterministic whatever order the
    `require` keys arrived in (§7).
    """
    required = facet(policy, "category_exclusion")
    if required is None:
        return CheckResult("category_exclusion", Verdict.NOT_APPLICABLE)

    merchants = [str(c) for c in requirement(required, "merchant_category_not_in", []) or []]
    items = [str(c) for c in requirement(required, "item_category_not_in", []) or []]
    words = [str(k).lower() for k in requirement(required, "item_keywords_none", []) or []]
    if not merchants and not items and not words:
        return _unknown(
            "category_exclusion", "the kinds of purchase you rule out could not be established"
        )

    results: list[CheckResult] = []
    if merchants:
        results.append(_merchant_side(ev, merchants))
    if items:
        results.append(_item_side(ev, items))
    if words:
        results.append(_keyword_side(ev, words))
    return tuple(results)


def _merchant_side(ev: EnrichedEvent, excluded: list[str]) -> CheckResult:
    evidence = (
        Evidence("merchant_category", ev.merchant_category),
        Evidence("merchant_category_excluded_list", ", ".join(excluded)),
    )
    if not ev.merchant_category:
        return _unknown(
            "category_exclusion",
            "the kind of shop this is could not be established",
            (Evidence("merchant_id", ev.merchant_id), *evidence),
        )
    if ev.merchant_category in excluded:
        return CheckResult(
            "category_exclusion",
            Verdict.VIOLATION,
            "merchant_category_excluded",
            evidence,
            f"this is a {_readable(ev.merchant_category)} shop, which you do not buy from",
            f"A shop that is not {_readable_list(excluded)} would meet your preferences.",
        )
    return CheckResult(
        "category_exclusion",
        Verdict.PASS,
        "categories_permitted",
        evidence,
        f"this is not {_readable_list(excluded)}, which you do not buy from",
    )


def _item_side(ev: EnrichedEvent, excluded: list[str]) -> CheckResult:
    seen = [str(line.get("item_category") or "") for line in ev.items]
    categorised = [c for c in seen if c]
    if not categorised:
        return _unknown(
            "category_exclusion",
            "what kind of thing this order contains could not be established",
            (Evidence("item_category_excluded_list", ", ".join(excluded)),),
        )

    offending = sorted({c for c in categorised if c in excluded})
    evidence = (
        Evidence("item_categories", ", ".join(sorted(set(categorised)))),
        Evidence("item_category_excluded_list", ", ".join(excluded)),
    )
    if offending:
        # Boundary: one excluded line among many is a violation. A basket nine-tenths
        # groceries and one-tenth gift voucher still contains a gift voucher.
        return CheckResult(
            "category_exclusion",
            Verdict.VIOLATION,
            "item_category_excluded",
            (*evidence, Evidence("item_categories_offending", ", ".join(offending))),
            f"this order contains {_readable_list(offending)}, which you do not buy",
            f"An order without {_readable_list(offending)} would meet your preferences.",
        )
    return CheckResult(
        "category_exclusion",
        Verdict.PASS,
        "categories_permitted",
        evidence,
        f"nothing here is {_readable_list(excluded)}, which you do not buy",
    )


def _keyword_side(ev: EnrichedEvent, excluded: list[str]) -> CheckResult:
    """ "No alcohol" — a product the customer ruled out that is not a category of its own.

    The live pack files *"Wine and spirits"* (IT0168) under `groceries`, so a groceries
    allow-list admits it and no category can exclude it. The words are compared with the cart
    line's **name tokens**, the same tokenizer as `item_keywords_all`, never with
    `item_details`: the data dictionary guarantees the name matches the catalogue row, and the
    details are free merchant text. Each entry is a phrase, and **all** its words must appear
    in one name: "wine" catches "Wine and spirits", and "Drinks and snacks" (soft drinks)
    matches nothing. specs/check-category-exclusion.md §"Product words".
    """
    phrases = [set(entry.split()) for entry in excluded if entry.split()]
    names = [str(line.get("item_name") or "") for line in ev.items]
    evidence: tuple[Evidence, ...] = (Evidence("item_keywords_excluded", ", ".join(excluded)),)
    if not any(names):
        return _unknown(
            "category_exclusion",
            "what this order contains could not be established",
            evidence,
        )
    offending = [name for name in names if any(phrase <= name_tokens(name) for phrase in phrases)]
    if offending:
        # Boundary: one excluded line among many is a violation, as for categories.
        shown = ", ".join(f'"{safe_display(name)}"' for name in offending)
        return CheckResult(
            "category_exclusion",
            Verdict.VIOLATION,
            "item_keyword_excluded",
            (*evidence, Evidence("item_names_offending", ", ".join(offending))),
            f"this order contains {shown}, which you do not buy",
            "An order without it would meet your preferences.",
        )
    return CheckResult(
        "category_exclusion",
        Verdict.PASS,
        "categories_permitted",
        evidence,
        "nothing here is a product you ruled out",
    )


# ------------------------------------------------------------------ spending hours


def check_spending_hours(
    ev: EnrichedEvent, policy: dict[str, Any]
) -> CheckResult | tuple[CheckResult, ...]:
    """A stated rule about *when* the agent may spend: the hours, the days, or both.

    One result per term the facet states, hours first. A facet stating only `weekdays` is not
    judged on hours at all. A facet stating no term keeps the old `unknown`.
    specs/check-spending-hours.md, and §"Days" for `weekdays`.
    """
    required = facet(policy, "spending_hours")
    if required is None:
        return CheckResult("spending_hours", Verdict.NOT_APPLICABLE)

    days = requirement(required, "weekdays")
    if days is None:
        return _spending_hours(ev, required)
    states_hours = any(requirement(required, key) is not None for key in ("hours_from", "hours_to"))
    if not states_hours:
        return _spending_days(ev, days)
    return (_spending_hours(ev, required), _spending_days(ev, days))


def _spending_days(ev: EnrichedEvent, days: Any) -> CheckResult:
    """ "Never at the weekend" — read on the customer's clock, not on UTC.

    A Friday 23:30 UTC order in September is a Saturday in Zurich, and "the weekend" the
    customer wrote is their own (domain/localtime.py). The hours keep their UTC reading
    (§"Which clock"). The customer names days in their own time, while an hours preference
    is set on a control that says UTC, and changing that is a separate decision.
    specs/check-spending-hours.md §"Days".
    """
    named = [str(d).strip().lower() for d in days] if isinstance(days, (list, tuple)) else []
    allowed = [d for d in WEEKDAYS if d in named]
    stated = (Evidence("weekdays", ", ".join(named) or "(none)"),)
    # A day we cannot read would silently narrow or widen the rule, so the whole term is
    # unknown rather than judged on the days that did parse.
    if not allowed or len(allowed) != len(set(named)):
        return _unknown("spending_hours", "the days you allow could not be established", stated)

    local = swiss_time(ev.timestamp)
    today = WEEKDAYS[local.weekday()]
    evidence = (
        Evidence("order_weekday_local", today),
        Evidence("order_time_local", local.strftime("%Y-%m-%d %H:%M %Z")),
        Evidence("spending_days", ", ".join(allowed)),
        Evidence("timezone", ZONE),
    )
    if today in allowed:
        return CheckResult(
            "spending_hours",
            Verdict.PASS,
            "within_spending_days",
            evidence,
            f"it was placed on a {DAY_NAMES[today]} (Swiss time), a day you allow",
        )
    return CheckResult(
        "spending_hours",
        Verdict.VIOLATION,
        "outside_spending_days",
        evidence,
        f"it was placed on a {DAY_NAMES[today]} (Swiss time), and you only buy on "
        f"{_days_text(allowed)}",
        f"An order on {_days_text(allowed)} would meet your preferences.",
    )


def _days_text(days: list[str]) -> str:
    """ "Monday to Friday" for a run of days, otherwise the days named one by one."""
    first, last = WEEKDAYS.index(days[0]), WEEKDAYS.index(days[-1])
    if len(days) > 2 and days == list(WEEKDAYS[first : last + 1]):
        return f"{DAY_NAMES[days[0]]} to {DAY_NAMES[days[-1]]}"
    names = [DAY_NAMES[d] for d in days]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _spending_hours(ev: EnrichedEvent, required: dict[str, Any]) -> CheckResult:
    """A stated rule about *when* the agent may spend — specs/check-spending-hours.md.

    Not the `unusual_hour` concern. That is our inference about risk, fixed at 00:00–05:00,
    weight 1.0, always on; this is a rule the customer wrote, over hours they chose. A stated
    rule that only raised a concern could be outvoted by the threshold, which is not what
    "do not buy at night" means — and the ambient signal turned into a violation would decline
    every legitimate late-night order, which the pack's own CU0004 makes a point of having.

    Reads the **simulated** clock (§1.2): an hours rule is a fact about when the customer's
    money moved, so a replay must decide the same way at midnight as at noon.
    """
    start = _hour(requirement(required, "hours_from"))
    end = _hour(requirement(required, "hours_to"))
    stated = (
        Evidence("hours_from", str(requirement(required, "hours_from"))),
        Evidence("hours_to", str(requirement(required, "hours_to"))),
    )
    # Equal bounds could mean a zero-length window or a whole day, and choosing either is a
    # guess about a restriction. The editor cannot produce the case; a model might.
    if start is None or end is None or start == end:
        return _unknown("spending_hours", "the hours you allow could not be established", stated)

    hour = ev.timestamp.hour
    # `hours_from` inclusive, `hours_to` exclusive — the same half-open convention as the
    # rolling window (§2.1). A window where `from > to` wraps midnight, which is how a person
    # says quiet hours; making them write two windows would be an interface leaking an
    # implementation.
    inside = start <= hour < end if start < end else (hour >= start or hour < end)
    evidence = (
        Evidence("timestamp_hour_utc", f"{hour:02d}"),
        Evidence("spending_hours_utc", f"{start:02d}:00-{end:02d}:00"),
    )
    window = f"{start:02d}:00 and {end:02d}:00"
    if inside:
        return CheckResult(
            "spending_hours",
            Verdict.PASS,
            "within_spending_hours",
            evidence,
            f"it was placed at {hour:02d}:00, inside the hours you allow",
        )
    return CheckResult(
        "spending_hours",
        Verdict.VIOLATION,
        "outside_spending_hours",
        evidence,
        f"it was placed at {hour:02d}:00, and you only buy between {window}",
        f"An order between {window} would meet your preferences.",
    )


def _hour(value: Any) -> int | None:
    """An hour of the day, or None when the bound is missing or not one."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= _MAX_HOUR else None
