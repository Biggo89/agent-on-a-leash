"""Runner for the language-neutral vectors in tests/vectors/*.yaml.

The vectors are the portability contract: a TypeScript port of domain/ must pass these exact
files. Keep the runner thin and keep the semantics in the YAML.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from leash.domain.ledger import Ledger
from leash.domain.money import CurrencyError, Money, to_chf
from leash.domain.sanitize import extract_item_facts, is_lookalike

VECTORS = Path(__file__).parent / "vectors"


def load(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load((VECTORS / name).read_text())["cases"]


def ids(cases: list[dict[str, Any]]) -> list[str]:
    return [c["name"] for c in cases]


# --------------------------------------------------------------------- money

MONEY = load("money_and_currency.yaml")


@pytest.mark.parametrize("case", MONEY, ids=ids(MONEY))
def test_money(case: dict[str, Any]) -> None:
    op = case["op"]
    if case.get("expect_error"):
        with pytest.raises(CurrencyError):
            to_chf(case["input"]["amount"], case["input"]["currency"])
        return
    if op == "money.from_value":
        assert Money.from_value(case["input"]).centimes == case["expect_centimes"]
    elif op == "money.sum":
        total = Money.zero()
        for v in case["input"]:
            total = total + Money.from_value(v)
        assert total.centimes == case["expect_centimes"]
    elif op == "money.to_chf":
        got = to_chf(case["input"]["amount"], case["input"]["currency"])
        assert got.centimes == case["expect_centimes"]
    else:  # pragma: no cover
        pytest.fail(f"unknown op {op}")


# ------------------------------------------------------------- rolling window

WINDOW_DOC = yaml.safe_load((VECTORS / "rolling_window.yaml").read_text())
WINDOW = WINDOW_DOC["cases"]


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


@pytest.mark.parametrize("case", WINDOW, ids=ids(WINDOW))
def test_rolling_window(case: dict[str, Any]) -> None:
    ledger = Ledger()
    for a in case.get("approvals", []):
        ledger.record_approval(a["id"], _ts(a["ts"]), Money.from_value(a["chf"]))
    for s in case.get("step_ups", []):
        ledger.record_step_up(s["id"], _ts(s["ts"]), Money.from_value(s["chf"]))
    if r := case.get("resolve"):
        ledger.resolve(r["id"], r["approved"])

    got = ledger.spend_in_window(_ts(case["now"]), WINDOW_DOC["period_days"])
    assert str(got) == case["expect_window_chf"]
    if "expect_cumulative_chf" in case:
        assert str(ledger.cumulative()) == case["expect_cumulative_chf"]


# ------------------------------------------------------------- untrusted text

TEXT = load("untrusted_text.yaml")


@pytest.mark.parametrize("case", TEXT, ids=ids(TEXT))
def test_untrusted_text(case: dict[str, Any]) -> None:
    if case.get("op") == "lookalike":
        assert is_lookalike(case["candidate"], case["known"]) is case["expect"]
        return

    facts = extract_item_facts(case["text"])
    for key, expected in (case.get("expect_facts") or {}).items():
        assert getattr(facts, key) == expected, f"{key}: {getattr(facts, key)!r} != {expected!r}"
    if "expect_return_window_days" in case:
        assert facts.return_window_days == case["expect_return_window_days"]

    labels = {m.label for m in facts.manipulations}
    if "expect_manipulations" in case:
        assert labels == set(case["expect_manipulations"])
    for expected_label in case.get("expect_manipulation_labels", []):
        assert expected_label in labels, f"missed {expected_label}; got {sorted(labels)}"


# --------------------------------------------------- the injection corpus

CORPUS = load("injection_corpus.yaml")


@pytest.mark.parametrize("case", CORPUS, ids=ids(CORPUS))
def test_injection_corpus(case: dict[str, Any]) -> None:
    """specs/check-manipulation-detected.md — four languages, and the near misses.

    The benign half is the point: a detector nobody measured for false positives is a
    detector that will eventually accuse an honest seller in front of a customer.
    """
    from leash.domain.sanitize import detect_manipulation

    found = detect_manipulation(case["text"], "item_details")
    labels = {m.label for m in found}

    if case["kind"] == "benign":
        assert not found, (
            f"ordinary {case['language']} merchant copy was flagged as "
            f"{sorted(labels)}: {case['text'][:90]!r}"
        )
        return

    assert found, f"attack text produced no finding: {case['text'][:90]!r}"
    for expected in case.get("expect_labels", []):
        assert expected in labels, f"missed {expected}; got {sorted(labels)}"


def test_the_corpus_covers_every_language_on_both_sides() -> None:
    """A language with no benign case has an unmeasured false-positive rate."""
    by_language: dict[str, set[str]] = {}
    for case in CORPUS:
        by_language.setdefault(case["language"], set()).add(case["kind"])
    assert set(by_language) >= {"en", "de", "fr", "it"}, sorted(by_language)
    for language, kinds in sorted(by_language.items()):
        assert kinds == {"attack", "benign"}, f"{language} only has {sorted(kinds)}"


def test_every_label_is_exercised_in_more_than_one_language() -> None:
    """A label only one language can produce is a language the detector cannot read."""
    from leash.domain.sanitize import detect_manipulation

    languages_by_label: dict[str, set[str]] = {}
    for case in CORPUS:
        for hit in detect_manipulation(case["text"], "item_details"):
            languages_by_label.setdefault(hit.label, set()).add(case["language"])
    thin = {label: sorted(ls) for label, ls in languages_by_label.items() if len(ls) < 2}
    assert not thin, f"labels reachable in only one language: {thin}"


# ------------------------------------------------------------------ checks

CHECK_VECTORS = [
    "merchant_permitted.yaml",
    "merchant_lookalike.yaml",
    "merchant_type.yaml",
    "order_terms.yaml",
    "item_matches_request.yaml",
    "item_attributes.yaml",
    "unrequested_addon.yaml",
    "goal_fulfilled.yaml",
    "duplicate_order.yaml",
    "split_order.yaml",
    "session_integrity.yaml",
    "manipulation_detected.yaml",
    "category_exclusion.yaml",
    "spending_hours.yaml",
]


def _check_cases() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for filename in CHECK_VECTORS:
        doc = yaml.safe_load((VECTORS / filename).read_text())
        for case in doc["cases"]:
            out.append((doc["check"], case))
    return out


CHECKS = _check_cases()


@pytest.mark.parametrize(("check_id", "case"), CHECKS, ids=[f"{c}:{v['name']}" for c, v in CHECKS])
def test_check_vectors(check_id: str, case: dict[str, Any]) -> None:
    from leash.domain.checks.exclusion import check_category_exclusion, check_spending_hours
    from leash.domain.checks.item import (
        check_goal_fulfilled,
        check_item_attributes,
        check_item_matches_request,
        check_unrequested_addon,
    )
    from leash.domain.checks.limits import check_split_order
    from leash.domain.checks.manipulation import check_manipulation_detected
    from leash.domain.checks.merchant import (
        check_merchant_lookalike,
        check_merchant_permitted,
        check_merchant_type,
    )
    from leash.domain.checks.order import check_duplicate_order, check_order_terms
    from leash.domain.checks.session import check_session_integrity
    from tests.builders import make_event, make_policy

    registry = {
        "merchant_permitted": check_merchant_permitted,
        "merchant_lookalike": check_merchant_lookalike,
        "merchant_type": check_merchant_type,
        "order_terms": check_order_terms,
        "item_matches_request": check_item_matches_request,
        "item_attributes": check_item_attributes,
        "unrequested_addon": check_unrequested_addon,
        "goal_fulfilled": check_goal_fulfilled,
        "duplicate_order": check_duplicate_order,
        "split_order": check_split_order,
        "session_integrity": check_session_integrity,
        "manipulation_detected": check_manipulation_detected,
        "category_exclusion": check_category_exclusion,
        "spending_hours": check_spending_hours,
    }
    event_fields = {
        k: v
        for k, v in case.items()
        if not k.startswith("expect_")
        and k not in ("name", "note", "facets", "hard_rules", "repeat_purchase_action")
    }
    if isinstance(event_fields.get("timestamp"), str):
        event_fields["timestamp"] = _ts(event_fields["timestamp"])
    # `facets` and `hard_rules` describe the mandate, never the event: a check that reads a
    # cap out of hard_rules (split_order, the limit checks) needs them on the policy side.
    policy = make_policy(facets=case.get("facets", []), hard_rules=case.get("hard_rules", []))
    if "repeat_purchase_action" in case:
        policy["repeat_purchase_action"] = case["repeat_purchase_action"]
    produced = registry[check_id](make_event(**event_fields), policy)
    # A check may return one result or several named signals.
    results = list(produced) if isinstance(produced, tuple) else [produced]

    if "expect_verdicts" in case:
        assert [str(r.verdict) for r in results] == case["expect_verdicts"]
        assert [r.reason_code for r in results if r.reason_code] == case["expect_reasons"]
        if "expect_detail_contains" in case:
            joined = " | ".join(r.detail for r in results)
            assert case["expect_detail_contains"] in joined, joined
        return

    assert len(results) == 1, f"expected one result, got {len(results)}"
    result = results[0]
    assert str(result.verdict) == case["expect_verdict"]
    assert result.reason_code == case["expect_reason"]
    if "expect_evidence_contains" in case:
        joined = " ".join(f"{e.field}={e.value}" for e in result.evidence)
        assert case["expect_evidence_contains"] in joined, joined
    if "expect_detail_contains" in case:
        assert case["expect_detail_contains"] in result.detail, result.detail
    if "expect_detail_excludes" in case:
        assert case["expect_detail_excludes"] not in result.detail, result.detail
    if "expect_follow_up" in case:
        assert result.follow_up == case["expect_follow_up"], result.follow_up


# ------------------------------------------------------------- hard-rule combination

BINDING = load("binding_cap.yaml")


@pytest.mark.parametrize("case", BINDING, ids=ids(BINDING))
def test_binding_cap(case: dict[str, Any]) -> None:
    """specs/decision-rules.md §9 — the tightest cap binds, never the first."""
    from leash.domain.policy import binding_cap

    found = binding_cap(case["hard_rules"], case["scope"])
    if case["expect_value"] is None:
        assert found is None
        return
    assert found is not None
    rule = found[0]
    assert float(rule["value"]) == case["expect_value"]
    if "expect_operator" in case:
        assert rule["operator"] == case["expect_operator"]


# ------------------------------------------------------------- period windows

PERIODS = load("period_windows.yaml")


@pytest.mark.parametrize("case", PERIODS, ids=ids(PERIODS))
def test_period_windows(case: dict[str, Any]) -> None:
    """specs/decision-rules.md §9 — every window the mandate states is enforced."""
    from leash.domain.checks.limits import check_period_limit
    from leash.domain.policy import period_caps
    from tests.builders import make_event, make_policy

    event = make_event(
        billing_amount_chf=case["billing_amount_chf"],
        amount=case["billing_amount_chf"],
        approved_spend_windows=case["windows"],
    )
    produced = check_period_limit(event, make_policy(hard_rules=case["hard_rules"]))
    results = list(produced) if isinstance(produced, tuple) else [produced]

    assert [str(r.verdict) for r in results] == case["expect_verdicts"]
    if "expect_reasons" in case:
        assert [r.reason_code for r in results if r.reason_code] == case["expect_reasons"]
    if "expect_detail_contains" in case:
        joined = " | ".join(r.detail for r in results)
        assert case["expect_detail_contains"] in joined, joined
    if "expect_window_order" in case:
        got = [days for days, _ in period_caps(case["hard_rules"]) if days is not None]
        assert got == case["expect_window_order"]


# ------------------------------------------------------------- policy layer composition

COMPOSITION = load("policy_composition.yaml")


@pytest.mark.parametrize("case", COMPOSITION, ids=ids(COMPOSITION))
def test_policy_composition(case: dict[str, Any]) -> None:
    """specs/customer-settings.md §3 — three layers, tightest wins, nothing silently dropped."""
    from leash.domain.compose import Layer, compose
    from leash.domain.policy import binding_cap, facet, requirement
    from leash.domain.provenance import source_of

    composed = compose(
        *(
            Layer.of(
                layer["source"],
                {
                    "hard_rules": layer.get("hard_rules", []),
                    "intent_facets": layer.get("intent_facets", []),
                    "uncertainty_policy": layer.get("uncertainty_policy"),
                },
            )
            for layer in case["layers"]
        )
    )
    policy = composed.policy

    if "expect_conflicts" in case:
        assert [c.setting for c in composed.conflicts] == case["expect_conflicts"]
    else:
        assert not composed.conflicts, [c.message for c in composed.conflicts]

    if "expect_purchase_cap" in case:
        found = binding_cap(policy["hard_rules"], "purchase")
        assert found is not None, "no purchase cap composed"
        assert float(found[0]["value"]) == case["expect_purchase_cap"]
        if "expect_cap_source" in case:
            assert source_of(found[0]) == case["expect_cap_source"]

    if "expect_period_windows" in case:
        from leash.domain.policy import period_windows

        assert list(period_windows(policy["hard_rules"])) == case["expect_period_windows"]
    if want := case.get("expect_period_cap_for"):
        from leash.domain.policy import period_caps

        bound = dict(period_caps(policy["hard_rules"]))
        assert float(bound[want["days"]]["value"]) == want["value"]

    if "expect_uncertainty_policy" in case:
        assert policy["uncertainty_policy"] == case["expect_uncertainty_policy"]
    if "expect_facet_count" in case:
        assert len(policy["intent_facets"]) == case["expect_facet_count"]
    if "expect_has_facet" in case:
        assert facet(policy, case["expect_has_facet"]) is not None
    if "expect_missing_facet" in case:
        assert facet(policy, case["expect_missing_facet"]) is None
    if want := case.get("expect_requirement"):
        assert requirement(facet(policy, want["kind"]), want["key"]) == want["value"]
    if "expect_note_contains" in case:
        assert any(case["expect_note_contains"] in n for n in composed.notes), composed.notes


# ---------------------------------------------------------- customer message composition

MESSAGE = load("customer_message.yaml")


@pytest.mark.parametrize("case", MESSAGE, ids=ids(MESSAGE))
def test_customer_message(case: dict[str, Any]) -> None:
    """specs/customer-message.md — how a set of check results becomes one sentence."""
    from leash.domain.evaluator import explain
    from leash.domain.types import CheckResult, Decision, Verdict
    from tests.builders import make_event

    results = [
        CheckResult(
            check_id=r.get("check_id", "vector"),
            verdict=Verdict(r["verdict"]),
            reason_code=r.get("reason_code"),
            detail=r.get("detail", ""),
            follow_up=r.get("follow_up", ""),
        )
        for r in case["results"]
    ]
    got = explain(
        Decision(case["decision"]),
        results,
        make_event(**case["event"]),
        tuple(case["codes"]),
    )
    assert got == " ".join(case["expect_message"].split())


# ------------------------------------------------------------- deadline guard

DEADLINE_DOC = yaml.safe_load((VECTORS / "deadline_guard.yaml").read_text())
DEADLINE = DEADLINE_DOC["cases"]


@pytest.mark.parametrize("case", DEADLINE, ids=ids(DEADLINE))
def test_deadline_guard(case: dict[str, Any]) -> None:
    """specs/deadline-guard.md — the only place the real clock changes an outcome."""
    from leash.domain import deadline as guard

    reserve = float(DEADLINE_DOC["reserve_ms"])
    budget = case["budget_ms"]
    budget = float(budget) if budget is not None else None

    assert guard.at_risk(budget, reserve_ms=reserve) is case["expect_at_risk"]
    if not case["expect_at_risk"]:
        return

    from tests.builders import make_event

    ev = make_event()
    record = guard.guard_record(
        ev,
        case["uncertainty_policy"],
        budget=budget,
        engine_version="leash-test",
        decided_at=datetime(2026, 9, 24, 9, 0, tzinfo=UTC),
        inputs_digest="test-digest",
    )
    assert str(record.decision) == case["expect_decision"]
    # One code, and no check results: nothing ran, and a record that implied otherwise
    # would tell a judge the evidence set was examined when it was not.
    assert record.reason_codes == ("deadline_risk",)
    assert record.check_results == ()
    assert "deadline" in record.customer_message.lower()
