"""One number for a decision, and every point of it accounted for.

A customer opening the app sees a verdict and a sentence. That is the right thing to read
*first*, but it answers "what happened" and not "how close was this". A score answers the
second question at a glance, and the checks already produce everything it needs.

**It adds no judgement.** The engine's opinion lives in `evaluator.CONCERN_WEIGHTS` and
`STEP_UP_THRESHOLD`; this module renders that opinion as a number. There is no second table
of importances here, nothing learned, and no model — which is exactly what makes it
defensible to an issuer, because every deduction can be pointed at a `CheckResult` that is
already on the record.

Three rules, in order of how much they matter:

1. **The decision sets the band.** `approve` 75-100, `step_up` 35-60, `decline` 5-30. The
   score can never contradict the decision, because the decision chose the range before any
   arithmetic ran. A "trusted-looking" decline is not a thing this can produce.
2. **The driving finding is not deducted twice.** It is already priced into the band.
   Everything *else* on the record is what moves the score inside it.
3. **The tail is capped.** Deductions total at most `TAIL`, so a pile of small findings can
   never push a decision out of its own band.

Pure: no I/O, no clock, no globals. Never raises — it runs on the audit path, and a
serializer that throws is worse than no score at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evaluator import CONCERN_WEIGHTS, STEP_UP_THRESHOLD
from .types import CheckResult, Decision, DecisionRecord, Verdict

#: Where each decision's band starts. The gap between bands is what stops a score from
#: implying an outcome the engine did not reach.
BAND_TOP: dict[Decision, int] = {
    Decision.APPROVE: 100,
    Decision.STEP_UP: 60,
    Decision.DECLINE: 30,
}

#: The most the findings beyond the driving one can take, together.
TAIL = 25

#: A finding that is not the one that decided the purchase. An adversarial concern costs
#: twice an ambient one, in the same 2:1 ratio `CONCERN_WEIGHTS` already uses — the ratio is
#: read from the weights rather than restated, so a tuned weight moves the score with it.
VIOLATION_COST = 5
UNKNOWN_COST = 10
CONCERN_UNIT = 5.0  # per 1.0 of concern weight

BANDS: tuple[tuple[int, str], ...] = ((75, "trusted"), (35, "review"), (0, "blocked"))
LABELS: dict[str, str] = {
    "trusted": "Trusted",
    "review": "Needs your decision",
    "blocked": "Blocked",
    "not_assessed": "Not assessed",
}


@dataclass(frozen=True, slots=True)
class Deduction:
    """One line of the arithmetic, in the customer's terms as well as ours."""

    check_id: str
    reason_code: str | None
    points: int
    why: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "reason_code": self.reason_code,
            "points": self.points,
            "why": self.why,
        }


def coverage(results: tuple[CheckResult, ...]) -> dict[str, Any]:
    """How much of the evidence set actually ran.

    A score over two checks and a score over fifteen are not the same claim, and only one of
    them deserves to be read as "we looked". `share` is over the checks that *applied* — a
    check standing down because the customer stated no such rule is not a gap, it is the
    gating working, and counting it as one would punish a narrow instruction.
    """
    total = len(results)
    failed = sum(1 for r in results if r.reason_code == "engine_error_defaulted")
    not_applicable = sum(1 for r in results if r.verdict is Verdict.NOT_APPLICABLE)
    applicable = total - not_applicable
    evaluated = applicable - failed
    return {
        "checks": total,
        "evaluated": evaluated,
        "not_applicable": not_applicable,
        "failed": failed,
        # None, not 1.0: "nothing applied" is not "everything passed".
        "share": round(evaluated / applicable, 3) if applicable else None,
    }


def _driving(record: DecisionRecord) -> CheckResult | None:
    """The first result that carries the decision, in evaluation order.

    Matched on the reason code the record actually cites, so the finding the customer was
    told about is the finding the band is priced for.
    """
    wanted = {
        Decision.DECLINE: Verdict.VIOLATION,
        Decision.STEP_UP: (Verdict.CONCERN, Verdict.UNKNOWN),
        Decision.APPROVE: (),
    }[record.decision]
    if not wanted:
        return None
    verdicts = wanted if isinstance(wanted, tuple) else (wanted,)
    for result in record.check_results:
        if result.verdict in verdicts and result.reason_code in record.reason_codes:
            return result
    return None


def _cost(result: CheckResult) -> tuple[int, str]:
    if result.verdict is Verdict.VIOLATION:
        return VIOLATION_COST, "another rule you wrote was broken"
    if result.verdict is Verdict.UNKNOWN:
        return UNKNOWN_COST, "a fact could not be established"
    weight = CONCERN_WEIGHTS.get(result.reason_code or "", 0.0)
    return round(CONCERN_UNIT * weight), "something was noticed that did not decide this"


def score(record: DecisionRecord) -> dict[str, Any]:
    """The trust score for one decision, with its arithmetic. Never raises."""
    try:
        return _score(record)
    except Exception:  # pragma: no cover — the audit path may not be the thing that fails
        return {
            "trust": None,
            "band": "not_assessed",
            "label": LABELS["not_assessed"],
            "deductions": [],
            "coverage": {"checks": 0, "evaluated": 0, "not_applicable": 0, "failed": 0},
            "scale": {"trusted_from": 75, "review_from": 35},
        }


def _score(record: DecisionRecord) -> dict[str, Any]:
    results = record.check_results
    cover = coverage(results)

    # Nothing was judged, so there is nothing to score. A low number here would read as a
    # verdict on the purchase when the truth is that no check reached it — the deadline
    # guard's record, for instance, carries no results at all by design.
    if cover["evaluated"] == 0:
        return {
            "trust": None,
            "band": "not_assessed",
            "label": LABELS["not_assessed"],
            "deductions": [],
            "coverage": cover,
            "scale": {"trusted_from": 75, "review_from": 35},
        }

    driver = _driving(record)
    total = BAND_TOP[record.decision]
    deductions: list[Deduction] = []
    spent = 0

    for result in results:
        if result is driver or result.verdict in (Verdict.PASS, Verdict.NOT_APPLICABLE):
            continue
        points, why = _cost(result)
        if points <= 0:
            continue
        # The cap is applied as the list is built, so a capped line is still *listed* at zero
        # rather than silently dropped: the customer can see what was noticed and that it
        # cost nothing.
        allowed = max(0, min(points, TAIL - spent))
        spent += allowed
        deductions.append(Deduction(result.check_id, result.reason_code, allowed, why))

    return {
        "trust": total - spent,
        "band": _band(total - spent),
        "label": LABELS[_band(total - spent)],
        "driver": driver.reason_code if driver else None,
        "deductions": [d.as_dict() for d in deductions],
        "coverage": cover,
        # The thresholds travel with the score so a gauge colours itself from the record
        # rather than from a constant of its own that could drift.
        "scale": {"trusted_from": 75, "review_from": 35},
        # The concern arithmetic this is a rendering of, so a reader can check our working.
        "concern_threshold": record.step_up_threshold or STEP_UP_THRESHOLD,
    }


def _band(value: int) -> str:
    for floor, name in BANDS:
        if value >= floor:
            return name
    return "blocked"


__all__ = ["BAND_TOP", "TAIL", "Deduction", "coverage", "score"]
