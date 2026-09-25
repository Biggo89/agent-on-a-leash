"""The compile-time guard: what a model is allowed to write into a Policy IR.

Spec: specs/llm-compiler.md, "Guard rails". Every test here runs with no key, no network and
no SDK — the trust argument for the compiler must be checkable offline, because that is how a
judge will want to check it.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from leash.adapters.datapack import data_dir, item_catalogue
from leash.compile.contract import (
    FACET_KINDS,
    FACET_REQUIRE_KEYS,
    ITEM_CATEGORIES,
    MERCHANT_CATEGORIES,
    accept,
    apply_safety_floor,
    instruction_hash,
    quotes_instruction,
)

INSTRUCTION = (
    "Replace my worn road-running shoes in size 43. Buy only from a specialist sports "
    "retailer, only if the order can be returned within 14 days or more, and pay no more "
    "than CHF 200. Ask me when uncertain."
)


def facet(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "merchant_type",
        "require": {"merchant_category_in": ["sporting_goods"]},
        "confidence": "high",
        "provenance": "from a specialist sports retailer",
    }
    base.update(overrides)
    return base


def rule(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "field": "billing_amount_chf",
        "operator": "<=",
        "value": 200,
        "scope": "purchase",
        "confidence": "high",
        "provenance": "pay no more than CHF 200",
    }
    base.update(overrides)
    return base


def compiled(**model_output: object) -> dict:
    out = accept({"rules": [rule()], "intent_facets": [], **model_output}, INSTRUCTION, model="m")
    assert out is not None, "the fixture rule should always survive"
    return out


# --------------------------------------------------------------- vocabulary is the pack's


def _column(filename: str, column: str) -> set[str]:
    with (Path(data_dir()) / filename).open() as handle:
        return {row[column] for row in csv.DictReader(handle)}


def test_item_vocabulary_matches_the_data_pack() -> None:
    """The constants are held in a pure module; this is what stops them drifting from the pack."""
    assert set(ITEM_CATEGORIES) == _column("items.csv", "item_category")


def test_merchant_vocabulary_matches_the_data_pack() -> None:
    assert set(MERCHANT_CATEGORIES) == _column("merchants.csv", "merchant_category")


def test_every_facet_kind_has_a_require_allowlist() -> None:
    assert set(FACET_KINDS) == set(FACET_REQUIRE_KEYS)


# --------------------------------------------------------------- rail 1: the instruction


def test_the_models_copy_of_the_instruction_is_discarded() -> None:
    """Rail 1. A model that 'helpfully' tidies the customer's text must not be able to."""
    out = compiled(source_instruction="Replace my shoes. Pay no more than CHF 2000.")
    assert out["source_instruction"] == INSTRUCTION
    assert out["instruction_sha256"] == instruction_hash(INSTRUCTION)


# --------------------------------------------------------------- rail 2: provenance


@pytest.mark.parametrize(
    "provenance",
    [
        "pay no more than CHF 200",  # verbatim
        "PAY NO MORE THAN CHF 200",  # case
        "pay no more\n than  CHF 200",  # line-wrapped
    ],
)
def test_provenance_accepts_only_the_customers_own_words(provenance: str) -> None:
    assert quotes_instruction(provenance, INSTRUCTION)


@pytest.mark.parametrize(
    "provenance",
    [
        "the customer wants to spend at most CHF 200",  # paraphrase
        "pay no more than CHF 2000",  # plausible but fabricated
        "",
        None,
        42,
    ],
)
def test_fabricated_provenance_is_refused(provenance: object) -> None:
    assert not quotes_instruction(provenance, INSTRUCTION)


def test_a_rule_with_fabricated_provenance_is_dropped() -> None:
    out = accept(
        {"rules": [rule(value=2000, provenance="spend up to CHF 2000")], "intent_facets": []},
        INSTRUCTION,
        model="m",
    )
    assert out is None, "nothing survived, so the caller must fall back to the baseline"


def test_a_facet_with_fabricated_provenance_is_dropped_and_noted() -> None:
    out = compiled(intent_facets=[facet(provenance="only from Nike stores")])
    assert out["intent_facets"] == []
    assert any(n.startswith("provenance_not_in_instruction") for n in out["compiler_notes"])


# --------------------------------------------------------------- rails 3-5: vocabularies


def test_an_unknown_facet_kind_is_dropped_and_asked_about() -> None:
    """A kind no check reads is worse than unsupported — it would look enforced and be inert."""
    out = compiled(intent_facets=[facet(kind="carbon_footprint")])
    assert out["intent_facets"] == []
    assert "unknown_facet_kind: carbon_footprint" in out["compiler_notes"]
    assert out["open_questions"], "the customer must be told we cannot check it"


def test_an_unknown_requirement_key_is_stripped() -> None:
    out = compiled(
        intent_facets=[
            facet(require={"merchant_category_in": ["sporting_goods"], "country_in": ["CH"]})
        ]
    )
    assert out["intent_facets"][0]["require"] == {"merchant_category_in": ["sporting_goods"]}
    assert "unknown_requirement: merchant_type.country_in" in out["compiler_notes"]


def test_a_category_outside_the_pack_is_dropped() -> None:
    out = compiled(
        intent_facets=[facet(require={"merchant_category_in": ["sporting_goods", "sneaker_shop"]})]
    )
    assert out["intent_facets"][0]["require"]["merchant_category_in"] == ["sporting_goods"]
    assert any(n.startswith("unknown_category") for n in out["compiler_notes"])


def test_a_facet_emptied_by_the_vocabulary_becomes_a_question() -> None:
    out = compiled(intent_facets=[facet(require={"merchant_category_in": ["sneaker_shop"]})])
    assert out["intent_facets"] == []
    assert "facet_empty_after_guard: merchant_type" in out["compiler_notes"]
    assert out["open_questions"]


def test_no_additions_needs_no_requirement() -> None:
    """Its presence is the whole rule, so the empty-facet rail must not swallow it."""
    out = compiled(
        intent_facets=[
            facet(kind="no_additions", require={}, provenance="only from a specialist sports")
        ]
    )
    assert [f["kind"] for f in out["intent_facets"]] == ["no_additions"]


# --------------------------------------------------------------- rails 5a-5b: keywords
#
# Measured 2026-09-24 (specs/llm-compiler.md, "Keywords"): a model compiled SCEN0004's monitor
# as ["27-inch", "monitor"] and SCEN0001's groceries as ["groceries", "household"]. Neither can
# match any product, so between them they declined 14 purchases the customers asked for.

CATALOGUE = item_catalogue()


def identity(**require: object) -> dict[str, object]:
    return facet(kind="item_identity", require=require, provenance="road-running shoes")


def guarded(
    require: dict[str, object], *, catalogue: object = CATALOGUE
) -> tuple[dict[str, object], list[str]]:
    """The identity facet's `require` after the guard, and the notes it wrote."""
    out = accept(
        {"rules": [rule()], "intent_facets": [identity(**require)]},
        INSTRUCTION,
        model="m",
        catalogue=catalogue,  # type: ignore[arg-type]
    )
    assert out is not None
    return out["intent_facets"][0]["require"], out["compiler_notes"]


def test_the_guard_cuts_keywords_with_the_item_checks_own_tokeniser() -> None:
    """One function, not two that agree today: if they drifted, rail 5a would repair nothing."""
    from leash.compile import contract
    from leash.domain.checks import item

    assert contract.name_tokens is item.name_tokens


def test_a_hyphenated_keyword_is_cut_into_the_words_the_check_compares() -> None:
    from leash.domain.checks.item import lines_matching

    monitor = ({"item_category": "electronics", "item_name": "27-inch computer monitor"},)
    as_written = {"item_category_in": ["electronics"], "item_keywords_all": ["27-inch", "monitor"]}
    assert lines_matching(monitor, {"require": as_written})[0] == [], "as written: no monitor"

    require, notes = guarded(as_written)
    assert require["item_keywords_all"] == ["27", "inch", "monitor"]
    assert "keyword_tokenised: '27-inch' → 27, inch" in notes
    assert lines_matching(monitor, {"require": require})[0] == [0], "after the rail: this one"


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (["Road Running"], ["road", "running"]),
        (["road-running", "shoes"], ["road", "running", "shoes"]),
        (["—", "running"], ["running"]),
    ],
    ids=["spaces-and-case", "hyphen", "no-tokens-at-all"],
)
def test_every_keyword_leaves_the_guard_as_name_tokens(
    written: list[str], expected: list[str]
) -> None:
    require, notes = guarded({"item_category_in": ["sporting_goods"], "item_keywords_all": written})
    assert require["item_keywords_all"] == expected
    assert any(n.startswith("keyword_tokenised") for n in notes)


def test_a_keyword_that_is_already_a_token_passes_without_a_note() -> None:
    require, notes = guarded(
        {"item_category_in": ["sporting_goods"], "item_keywords_all": ["Road", "running"]}
    )
    assert require["item_keywords_all"] == ["road", "running"]
    assert not any(n.startswith("keyword_") for n in notes), "case alone is not worth a note"


def test_kind_words_the_category_covers_and_no_product_carries_are_dropped() -> None:
    """The SCEN0001 measurement: the category stays, the words that could only decline go."""
    require, notes = guarded(
        {"item_category_in": ["groceries"], "item_keywords_all": ["groceries", "household"]}
    )
    assert require == {"item_category_in": ["groceries"]}
    assert (
        "keyword_matches_no_product: 'household' (no groceries product has it in its name)" in notes
    )
    assert any("'groceries'" in n for n in notes if n.startswith("keyword_matches_no_product"))


def test_a_word_that_names_no_kind_keeps_declining() -> None:
    """Condition 1. The catalogue sells no kayak, so declining is the correct answer."""
    require, notes = guarded(
        {"item_category_in": ["sporting_goods"], "item_keywords_all": ["kayak"]}
    )
    assert require["item_keywords_all"] == ["kayak"]
    assert not any(n.startswith("keyword_matches_no_product") for n in notes)


def test_without_a_category_a_kind_word_is_the_only_statement_of_kind() -> None:
    """Condition 2. Dropping it would leave an identity that names nothing at all."""
    require, _ = guarded({"item_keywords_all": ["groceries"]})
    assert require["item_keywords_all"] == ["groceries"]


@pytest.mark.parametrize(
    ("word", "category"),
    [("fuel", "fuel"), ("hotel", "hotel"), ("household", "household")],
)
def test_a_kind_word_a_product_carries_still_distinguishes(word: str, category: str) -> None:
    """Condition 3. `Fuel purchase` carries `fuel`; the EV charging session does not."""
    require, _ = guarded({"item_category_in": [category], "item_keywords_all": [word]})
    assert require["item_keywords_all"] == [word]


def test_a_distinguishing_word_survives_beside_a_dropped_kind_word() -> None:
    require, _ = guarded(
        {"item_category_in": ["sporting_goods"], "item_keywords_all": ["goods", "running"]}
    )
    assert require["item_keywords_all"] == ["running"]


def test_without_a_catalogue_nothing_can_be_proven_so_nothing_is_dropped() -> None:
    """No pack, no rail 5b: the keyword declines, which is the behaviour before the rail."""
    require, _ = guarded(
        {"item_category_in": ["groceries"], "item_keywords_all": ["groceries", "household"]},
        catalogue=None,
    )
    assert require["item_keywords_all"] == ["groceries", "household"]


def test_the_catalogue_covers_every_item_category() -> None:
    assert set(CATALOGUE) == set(ITEM_CATEGORIES)


# --------------------------------------------------------------- rails 6-8: confidence


def test_a_low_confidence_facet_is_not_enforced_but_is_asked_about() -> None:
    """A facet restricts; enforcing a guess over-blocks. specs/llm-compiler.md, Low confidence."""
    out = compiled(
        intent_facets=[facet(confidence="low", open_question="Does a general sports shop count?")]
    )
    assert out["intent_facets"] == []
    assert "low_confidence_facet: merchant_type" in out["compiler_notes"]
    assert "Does a general sports shop count?" in out["open_questions"]


def test_a_low_confidence_cap_is_enforced_and_asked_about() -> None:
    """A cap permits; dropping a guess would loosen the mandate below the customer's words."""
    out = accept({"rules": [rule(confidence="low")], "intent_facets": []}, INSTRUCTION, model="m")
    assert out is not None
    assert out["rules"][0]["value"] == 200
    assert out["open_questions"], "…but the customer is still asked"


def test_missing_confidence_is_treated_as_low() -> None:
    out = compiled(intent_facets=[facet(confidence=None)])
    assert out["intent_facets"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"field": "amount"},  # the un-converted currency field — a real bug class
        {"operator": ">="},  # a floor, not a cap
        {"value": -5},
        {"value": "not a number"},
        {"scope": "lifetime"},
        {"scope": "period", "period_days": None},  # a window we would have to invent
    ],
)
def test_unenforceable_rules_are_dropped(bad: dict) -> None:
    out = accept({"rules": [rule(**bad)], "intent_facets": []}, INSTRUCTION, model="m")
    assert out is None


def test_an_unrecognised_uncertainty_policy_falls_back_to_ask() -> None:
    out = compiled(uncertainty_policy="yolo")
    assert out["uncertainty_policy"] == "ask"


# --------------------------------------------------------------- rail 11: duplicates


def test_a_second_facet_of_the_same_kind_is_dropped() -> None:
    """domain/policy.facet() reads the first; a second would look enforced and do nothing."""
    out = compiled(intent_facets=[facet(), facet(require={"merchant_category_in": ["clothing"]})])
    assert len(out["intent_facets"]) == 1
    assert out["intent_facets"][0]["require"]["merchant_category_in"] == ["sporting_goods"]
    assert "duplicate_facet: merchant_type" in out["compiler_notes"]


# --------------------------------------------------------------- rail 9: the safety floor


def test_the_model_may_not_loosen_a_cap_the_instruction_states() -> None:
    """The fabricated cap is not merely out-voted by the tighter one — it leaves the IR.

    Both would be enforced and the tighter would bind, so this is about the review screen:
    two contradictory caps side by side is a mandate the customer cannot check.
    """
    model_ir = compiled(rules=[rule(value=2000)])
    floored = apply_safety_floor(model_ir, {"rules": [{**rule(), "value": 200.0}]})
    assert [r["value"] for r in floored["rules"]] == [200.0]
    assert any(n.startswith("safety_floor_applied") for n in floored["compiler_notes"])
    assert any(n.startswith("unsupported_cap_removed") for n in floored["compiler_notes"])


def test_the_model_may_not_drop_a_cap_the_instruction_states() -> None:
    model_ir = accept({"rules": [], "intent_facets": [facet()]}, INSTRUCTION, model="m")
    assert model_ir is not None
    floored = apply_safety_floor(model_ir, {"rules": [{**rule(), "value": 200.0}]})
    assert [r["value"] for r in floored["rules"]] == [200.0]


def test_the_model_may_tighten_a_cap() -> None:
    model_ir = compiled(rules=[rule(value=150)])
    floored = apply_safety_floor(model_ir, {"rules": [{**rule(), "value": 200.0}]})
    assert [r["value"] for r in floored["rules"]] == [150.0]
    assert not any(n.startswith("safety_floor_applied") for n in floored["compiler_notes"])


def test_the_model_may_rescope_a_cap_the_regex_mis_scoped() -> None:
    """The floor matches on value, not scope — the scope is the judgement the model is for.

    "…never more than CHF 75 on a single order" is scoped by the baseline as a fourteen-day
    total. A scope-keyed floor would import that misreading and cap a fortnight at CHF 75.
    """
    model_ir = compiled(
        rules=[
            rule(value=250, scope="period", period_days=14, provenance="CHF 200"),
            rule(value=75, scope="purchase", provenance="pay no more"),
        ]
    )
    regex_misparse = {
        "rules": [
            {**rule(), "value": 250.0, "scope": "period", "period_days": 14},
            {**rule(), "value": 75.0, "scope": "period", "period_days": 14},
        ]
    }
    floored = apply_safety_floor(model_ir, regex_misparse)
    assert {(r["scope"], r["value"]) for r in floored["rules"]} == {
        ("period", 250.0),
        ("purchase", 75.0),
    }
    assert not any(n.startswith("safety_floor_applied") for n in floored["compiler_notes"])


def test_two_baseline_caps_cannot_be_covered_by_one_accepted_cap() -> None:
    """A model that kept the per-order cap and dropped the weekly total must not pass."""
    model_ir = compiled(rules=[rule(value=120, provenance="pay no more")])
    baseline = {
        "rules": [
            {**rule(), "value": 120.0, "scope": "purchase"},
            {**rule(), "value": 300.0, "scope": "period", "period_days": 7},
        ]
    }
    floored = apply_safety_floor(model_ir, baseline)
    assert {(r["scope"], r["value"]) for r in floored["rules"]} == {
        ("purchase", 120.0),
        ("period", 300.0),
    }
    assert any(n.startswith("safety_floor_applied") for n in floored["compiler_notes"])


# --------------------------------------------------------------- shape


def test_nothing_survivable_returns_none_rather_than_a_thin_ir() -> None:
    """The fallback is total. A mandate that looks compiled and enforces nothing is worse."""
    assert accept({"rules": [], "intent_facets": []}, INSTRUCTION, model="m") is None
    assert accept("not an object", INSTRUCTION, model="m") is None
    assert accept(None, INSTRUCTION, model="m") is None


def test_the_ir_names_the_compiler_that_produced_it() -> None:
    assert compiled()["compiler"] == "llm:m"


# --------------------------------------------------------------- the request we will send


def test_the_output_schema_is_valid_json_schema() -> None:
    """A malformed schema is a 400 on the day, discovered on stage. Check it offline."""
    import jsonschema

    from leash.compile.llm import OUTPUT_SCHEMA

    jsonschema.Draft202012Validator.check_schema(OUTPUT_SCHEMA)


def test_a_well_formed_model_answer_satisfies_the_schema() -> None:
    """Belt and braces: the shape the guard expects is the shape the API is told to produce."""
    import jsonschema

    from leash.compile.llm import OUTPUT_SCHEMA

    answer = {
        "uncertainty_policy": "ask",
        "rules": [
            {
                "field": "billing_amount_chf",
                "operator": "<=",
                "value": 200,
                "scope": "purchase",
                "period_days": None,
                "confidence": "high",
                "provenance": "pay no more than CHF 200",
            }
        ],
        "intent_facets": [
            {
                "kind": "merchant_type",
                "require": {
                    "item_category_in": None,
                    "item_keywords_all": None,
                    "item_description": None,
                    "size": None,
                    "prior_approvals_min": None,
                    "merchant_category_in": ["sporting_goods"],
                    "return_window_days_min": None,
                    "item_category_not_in": None,
                    "merchant_category_not_in": None,
                    "item_keywords_none": None,
                    "hours_from": None,
                    "hours_to": None,
                    "weekdays": None,
                    "cancellable": None,
                },
                "confidence": "medium",
                "provenance": "from a specialist sports retailer",
                "open_question": None,
            }
        ],
        "guidance": ["Each order stays at or below CHF 200."],
        "open_questions": [],
    }
    jsonschema.validate(answer, OUTPUT_SCHEMA)

    # …and the guard accepts exactly that, so schema and guard cannot drift apart silently.
    ir = accept(answer, INSTRUCTION, model="m")
    assert ir is not None
    assert [r["value"] for r in ir["rules"]] == [200.0]
    assert [f["kind"] for f in ir["intent_facets"]] == ["merchant_type"]


# --------------------------------------------------------------- guidance coverage


def _policy(guidance: list[str]) -> dict:
    """Two enforced items — a cap and a facet — with whatever guidance is passed."""
    return {
        "uncertainty_policy": "ask",
        "rules": [
            {
                "field": "billing_amount_chf",
                "operator": "<=",
                "value": 200,
                "scope": "purchase",
                "confidence": "high",
                "provenance": "CHF 200",
            }
        ],
        "intent_facets": [
            {
                "kind": "item_attribute",
                "require": {"size": 43},
                "confidence": "high",
                "provenance": "size 43",
            }
        ],
        "guidance": guidance,
        "open_questions": [],
    }


GUIDANCE_INSTRUCTION = "Buy shoes in size 43 for CHF 200 or less."


def test_guidance_that_omits_an_enforced_rule_is_flagged() -> None:
    """The review screen must not under-report what the agent will enforce.

    The rest of this module checks nothing unjustified is enforced. This is the other
    direction, and it had no check at all: observed 2026-09-09, a five-requirement mandate
    whose guidance mentioned only the spend cap.
    """
    ir = accept(
        _policy(["Your purchase will not exceed CHF 200."]), GUIDANCE_INSTRUCTION, model="m"
    )
    assert ir is not None
    note = next((n for n in ir["compiler_notes"] if n.startswith("guidance_thinner")), None)
    assert note is not None, f"a thin review screen went unflagged: {ir['compiler_notes']}"
    assert "2 enforced, 1 explained" in note


def test_complete_guidance_is_not_flagged() -> None:
    """Informational, not noisy — a complete review screen must stay silent."""
    ir = accept(
        _policy(["Your purchase will not exceed CHF 200.", "Only size 43 will be bought."]),
        GUIDANCE_INSTRUCTION,
        model="m",
    )
    assert ir is not None
    assert not [n for n in ir["compiler_notes"] if n.startswith("guidance_thinner")]


def test_guidance_survives_the_guard_unchanged() -> None:
    """The guard never writes customer-facing prose — it only reports on it."""
    lines = ["Your purchase will not exceed CHF 200.", "Only size 43 will be bought."]
    ir = accept(_policy(lines), GUIDANCE_INSTRUCTION, model="m")
    assert ir is not None
    assert ir["guidance"] == lines
