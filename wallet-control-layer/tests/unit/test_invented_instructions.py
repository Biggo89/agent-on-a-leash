"""Generalisation: instructions the compiler has never seen.

TASKS.md Phase 4 — "compile 5-6 *invented* instructions to prove generalisation beyond the
fixtures". Passing on the pack's five instructions proves nothing about generalisation, since
the regex baseline was written while looking at them.

The `always` expectations are safety properties of the **guard** and run on every `make check`
with no key present. The `with_model` expectations run only when a model actually compiled,
so a live run reports honestly whether the model earned its place rather than being assumed to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from leash.compile import compile_instruction

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "invented_instructions.yaml"
CASES: list[dict[str, Any]] = yaml.safe_load(FIXTURES.read_text())
IDS = [case["id"] for case in CASES]


@pytest.fixture(scope="module", params=CASES, ids=IDS)
def case(request: Any) -> dict[str, Any]:
    return request.param


@pytest.fixture(scope="module")
def ir(case: dict[str, Any]) -> dict[str, Any]:
    """Compiled once per case, by whichever compiler the environment makes available."""
    return compile_instruction(case["instruction"])


def covers(caps: list[dict[str, Any]], rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which required caps are NOT covered by a distinct rule at that amount or tighter.

    Same ascending greedy as the safety floor, and for the same reason: one rule cannot stand
    in for two required caps, or a compiler that dropped the weekly total would pass on the
    strength of the per-order one.
    """
    values = sorted((float(r["value"]), i) for i, r in enumerate(rules))
    used: set[int] = set()
    missing = []
    for cap in sorted(caps, key=lambda c: float(c["value"])):
        limit = float(cap["value"])
        match = next(
            (
                i
                for v, i in values
                if i not in used
                and v <= limit
                and ("scope" not in cap or rules[i].get("scope") == cap["scope"])
            ),
            None,
        )
        if match is None:
            missing.append(cap)
        else:
            used.add(match)
    return missing


# --------------------------------------------------------------- always


def test_it_compiles_at_all(ir: dict[str, Any], case: dict[str, Any]) -> None:
    """No invented instruction may crash the compiler or produce an empty mandate."""
    assert ir["source_instruction"] == case["instruction"]
    assert ir["rules"] or ir["intent_facets"], "nothing was enforceable"


def test_every_stated_limit_is_enforced(ir: dict[str, Any], case: dict[str, Any]) -> None:
    """The safety property: a limit the customer wrote is never lost or loosened."""
    missing = covers(case["always"]["caps"], ir["rules"])
    assert not missing, (
        f"limits stated in the instruction are not enforced: {missing} "
        f"(compiled: {[(r.get('scope'), r['value']) for r in ir['rules']]})"
    )


def test_no_restriction_is_invented(ir: dict[str, Any], case: dict[str, Any]) -> None:
    """The over-blocking guard, and the one most likely to fail on an unseen instruction.

    A facet the instruction does not support is a rule the customer never agreed to, enforced
    against them. `AGENTS.md` §0.7: over-blocking is a failure, not a safe default.
    """
    allowed = set(case["always"]["no_facets_beyond"])
    enforced = {f["kind"] for f in ir["intent_facets"]}
    assert enforced <= allowed, f"invented restriction(s): {sorted(enforced - allowed)}"


def test_a_stated_uncertainty_policy_is_enforced(ir: dict[str, Any], case: dict[str, Any]) -> None:
    """An `always` property since 2026-09-09: both compilers must read a stated policy.

    It sat in `with_model` while the baseline hardcoded the field, so the suite could not see
    that every instruction got `ask` whatever it said. `specs/policy-ir.md` rule 6.
    """
    expected = case["always"].get("uncertainty_policy")
    if not expected:
        pytest.skip("this instruction states no uncertainty policy")
    assert ir["uncertainty_policy"] == expected


def test_facets_that_would_be_wrong_are_absent(ir: dict[str, Any], case: dict[str, Any]) -> None:
    forbidden = set(case["always"].get("never_facets") or ())
    enforced = {f["kind"] for f in ir["intent_facets"]}
    assert not (enforced & forbidden), f"wrong restriction(s): {sorted(enforced & forbidden)}"


def test_every_enforced_item_quotes_the_customer(ir: dict[str, Any]) -> None:
    """Provenance is what makes the review screen checkable — it may never be missing."""
    for item in [*ir["rules"], *ir["intent_facets"]]:
        assert item.get("provenance"), f"no provenance on {item}"


def test_the_instruction_is_carried_verbatim(ir: dict[str, Any], case: dict[str, Any]) -> None:
    """The API rejects an altered instruction; a compiler that tidies text loses the run."""
    from leash.compile import instruction_hash

    assert ir["instruction_sha256"] == instruction_hash(case["instruction"])


# --------------------------------------------------------------- with a model


def ran_a_model(ir: dict[str, Any]) -> bool:
    return str(ir.get("compiler", "")).startswith("llm:")


def test_the_model_finds_what_the_regex_cannot(ir: dict[str, Any], case: dict[str, Any]) -> None:
    if not ran_a_model(ir):
        pytest.skip("no model compiled this instruction — see test_compile_fallback.py")
    expected = set(case.get("with_model", {}).get("facets") or ())
    enforced = {f["kind"] for f in ir["intent_facets"]}
    assert expected <= enforced, f"the model missed {sorted(expected - enforced)}"


def test_the_model_scopes_caps_correctly(ir: dict[str, Any], case: dict[str, Any]) -> None:
    if not ran_a_model(ir):
        pytest.skip("no model compiled this instruction")
    expected = case.get("with_model", {}).get("scoped_caps")
    if not expected:
        pytest.skip("no scope expectation for this instruction")
    compiled = {(float(r["value"]), r.get("scope"), r.get("period_days")) for r in ir["rules"]}
    for cap in expected:
        key = (float(cap["value"]), cap["scope"], cap.get("period_days"))
        assert key in compiled, f"expected {key}, compiled {sorted(compiled)}"


def test_the_model_asks_about_what_it_cannot_check(
    ir: dict[str, Any], case: dict[str, Any]
) -> None:
    """Silently dropping a requirement is the failure the brief names; asking is the fix."""
    if not ran_a_model(ir):
        pytest.skip("no model compiled this instruction")
    phrases = case.get("with_model", {}).get("mentions_question") or []
    if not phrases:
        pytest.skip("nothing unenforceable in this instruction")
    asked = " ".join(ir["open_questions"]).lower()
    for phrase in phrases:
        assert phrase.lower() in asked, (
            f"never asked about {phrase!r}; asked: {ir['open_questions']}"
        )


def test_the_model_explains_everything_it_enforces(ir: dict[str, Any]) -> None:
    """Every enforced rule and facet should be mentioned in the review screen's prose.

    `guidance` is what a non-technical customer actually reads before consenting. Observed
    2026-09-09 before the prompt was tightened: a five-requirement SCEN0002 mandate whose
    guidance mentioned only the spend cap, varying between one and three lines across runs on
    identical input. Nothing in the decision path notices, because guidance is not a decision
    — so `make replay-llm` cannot catch this and only a check here will.

    Allows one sentence to cover two closely related requirements, which is good writing
    rather than a defect; it fails only when the account is clearly incomplete.
    """
    if not ran_a_model(ir):
        pytest.skip("no model compiled this instruction")
    enforced = len(ir["rules"]) + len(ir["intent_facets"])
    explained = len(ir["guidance"])
    assert explained >= (enforced + 1) // 2, (
        f"{enforced} requirements enforced but only {explained} explained: {ir['guidance']}"
    )
