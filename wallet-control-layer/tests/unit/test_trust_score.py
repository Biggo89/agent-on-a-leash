"""The trust score, and the properties that make it defensible rather than decorative.

Spec: specs/trust-score.md. The arithmetic is the easy part; what these guard is that the
score can never contradict the decision, never invent a judgement the engine did not make,
and never reach the organizers' API.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from leash.domain.evaluator import CONCERN_WEIGHTS
from leash.domain.score import BAND_TOP, TAIL, coverage, score
from leash.domain.types import CheckResult, Decision, DecisionRecord, Verdict
from tests.board import replay_board

BOARD = replay_board()


def _record(
    decision: Decision, results: list[CheckResult], codes: tuple[str, ...]
) -> DecisionRecord:
    return DecisionRecord(
        authorization_id="AU_TEST",
        decision=decision,
        reason_codes=codes,
        customer_message="",
        evidence=(),
        check_results=tuple(results),
        inputs_digest="d",
        engine_version="test",
        latency_ms=0.0,
        decided_at=datetime(2026, 9, 22, tzinfo=UTC),
    )


# ------------------------------------------------------------------ the board


@pytest.mark.parametrize("auth_id", sorted(BOARD))
def test_the_score_never_contradicts_its_decision(auth_id: str) -> None:
    """The band is chosen before any arithmetic, and the cap keeps the score inside it."""
    record = BOARD[auth_id]
    result = score(record)
    top = BAND_TOP[record.decision]
    assert top - TAIL <= result["trust"] <= top, f"{auth_id} left its band"
    expected_band = {
        Decision.APPROVE: "trusted",
        Decision.STEP_UP: "review",
        Decision.DECLINE: "blocked",
    }[record.decision]
    assert result["band"] == expected_band


@pytest.mark.parametrize("auth_id", sorted(BOARD))
def test_every_deduction_points_at_a_check_on_the_record(auth_id: str) -> None:
    """The whole claim: no point comes from anywhere but the evidence already published."""
    record = BOARD[auth_id]
    on_record = {r.check_id for r in record.check_results}
    for deduction in score(record)["deductions"]:
        assert deduction["check_id"] in on_record, deduction


def test_every_check_that_applies_runs_on_every_decision() -> None:
    """Coverage is 1.0 across the board — `CHECKS` has no placeholders.

    This is the figure that makes the score readable as "we looked" rather than "we looked at
    some of it", so it is worth failing loudly if it ever stops being true.
    """
    for auth_id, record in BOARD.items():
        cover = score(record)["coverage"]
        assert cover["failed"] == 0, auth_id
        assert cover["share"] == 1.0, f"{auth_id}: {cover}"


def test_the_published_distribution_is_what_the_spec_documents() -> None:
    """specs/trust-score.md carries this table; a silent drift would make the spec a lie."""
    from collections import Counter

    counts = Counter(score(r)["trust"] for r in BOARD.values())
    assert dict(counts) == {100: 17, 60: 8, 30: 12, 25: 2, 20: 2, 15: 2, 10: 2}


def test_the_worst_events_on_the_board_score_lowest() -> None:
    """A breach plus three session signals should not look like a breach alone."""
    burst = min(score(BOARD[a])["trust"] for a in ("AU0029", "AU0030"))
    plain = score(BOARD["AU0043"])["trust"]  # one clear breach, nothing else
    assert burst < plain


# ------------------------------------------------- the scale, on synthetic records


def test_the_driving_finding_is_not_charged_twice() -> None:
    """One clear breach sits at the top of its band, not below it."""
    one = _record(
        Decision.DECLINE,
        [CheckResult("a", Verdict.VIOLATION, "per_order_limit_exceeded")],
        ("per_order_limit_exceeded",),
    )
    assert score(one)["trust"] == BAND_TOP[Decision.DECLINE]
    assert score(one)["deductions"] == []


def test_two_adversarial_concerns_score_below_one() -> None:
    """The flat 60 across the board's step-ups is the fixtures, not the scale.

    Every step-up in the pack carries exactly one finding. Give a record two adversarial
    concerns and the score moves, which is what makes the eight identical scores information
    rather than a broken gauge.
    """
    codes = ("merchant_lookalike", "device_novel")
    assert all(CONCERN_WEIGHTS[c] == 2.0 for c in codes)
    two = _record(
        Decision.STEP_UP,
        [CheckResult(c, Verdict.CONCERN, c) for c in codes],
        codes,
    )
    assert score(two)["trust"] == 50


def test_an_ambient_signal_costs_half_an_adversarial_one() -> None:
    """The 2:1 ratio is read from CONCERN_WEIGHTS, never restated here."""
    adversarial = _record(
        Decision.DECLINE,
        [
            CheckResult("v", Verdict.VIOLATION, "per_order_limit_exceeded"),
            CheckResult("c", Verdict.CONCERN, "device_novel"),
        ],
        ("per_order_limit_exceeded",),
    )
    ambient = _record(
        Decision.DECLINE,
        [
            CheckResult("v", Verdict.VIOLATION, "per_order_limit_exceeded"),
            CheckResult("c", Verdict.CONCERN, "unusual_hour"),
        ],
        ("per_order_limit_exceeded",),
    )
    assert BAND_TOP[Decision.DECLINE] - score(adversarial)["trust"] == 10
    assert BAND_TOP[Decision.DECLINE] - score(ambient)["trust"] == 5


def test_a_pile_of_findings_cannot_leave_the_band() -> None:
    """The cap, and the capped lines still being listed at zero."""
    results = [CheckResult("v", Verdict.VIOLATION, "per_order_limit_exceeded")]
    results += [CheckResult(f"c{i}", Verdict.CONCERN, "device_novel") for i in range(10)]
    record = _record(Decision.DECLINE, results, ("per_order_limit_exceeded",))
    result = score(record)
    assert result["trust"] == BAND_TOP[Decision.DECLINE] - TAIL
    assert sum(d["points"] for d in result["deductions"]) == TAIL
    assert any(d["points"] == 0 for d in result["deductions"]), "a capped line was dropped"


def test_a_record_with_nothing_evaluated_has_no_score() -> None:
    """The deadline guard's shape. A low number would read as a verdict on the purchase."""
    from leash.domain import deadline as guard
    from tests.builders import make_event

    record = guard.guard_record(
        make_event(),
        "ask",
        budget=10.0,
        engine_version="test",
        decided_at=datetime(2026, 9, 22, tzinfo=UTC),
        inputs_digest="d",
    )
    result = score(record)
    assert result["trust"] is None
    assert result["band"] == "not_assessed"


def test_a_check_that_did_not_apply_is_not_counted_as_a_gap() -> None:
    results = [
        CheckResult("ran", Verdict.PASS, "within_per_order_limit"),
        CheckResult("stood_down", Verdict.NOT_APPLICABLE),
    ]
    cover = coverage(tuple(results))
    assert cover == {"checks": 2, "evaluated": 1, "not_applicable": 1, "failed": 0, "share": 1.0}


def test_the_score_never_raises() -> None:
    """It runs on the audit path. A serializer that throws is worse than no score."""
    broken = _record(Decision.APPROVE, [None], ())  # type: ignore[list-item]
    assert score(broken)["band"] == "not_assessed"


# ------------------------------------------------------------ the platform boundary


@pytest.mark.parametrize("auth_id", sorted(BOARD))
def test_the_score_never_reaches_the_organizers_api(auth_id: str) -> None:
    """A presentation feature must not be able to affect a submission.

    `to_payload()` is the body POSTed to `/v1/authorizations/{id}/decision`. It carries the
    fields their contract names and nothing we invented.
    """
    payload = BOARD[auth_id].to_payload()
    assert set(payload) == {
        "authorization_id",
        "decision",
        "reason_codes",
        "customer_message",
        "evidence",
        "engine_version",
    }
    assert "trust" not in str(payload)


def test_the_score_does_reach_the_service_response() -> None:
    """…and it is in the one place the UI and the audit trail read."""
    from leash.audit.record import decision_summary

    summary = decision_summary(BOARD["AU0037"])
    assert summary["score"]["trust"] == 20
    assert summary["score"]["band"] == "blocked"
    assert summary["score"]["coverage"]["share"] == 1.0
    # The concern arithmetic it renders is still published beside it.
    assert summary["score"]["concern_score"] == 2.0
