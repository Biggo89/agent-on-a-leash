"""Categories a loaded pack adds reach every reader. specs/live-instructions.md §D.

The live pack files the SCEN0122 camera lens under `photography` and flights under `travel`,
item categories the repo pack never used. With the vocabulary fixed at the repo's constants the
guard dropped them, the model could not name them, and the real lens failed its own identity
check: "this order is for 'Camera lens', not the camera lens you asked for".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from leash.compile import baseline
from leash.compile.contract import Guard
from leash.compile.llm import output_schema
from leash.domain import settings
from leash.domain.checks.item import check_item_matches_request
from leash.domain.types import Verdict
from tests.builders import make_event, make_policy

LENS = {
    "item_id": "IT0123",
    "item_name": "Camera lens",
    "item_category": "photography",
    "quantity": 1,
    "unit_price": "620.00",
    "currency": "CHF",
}


@pytest.fixture()
def live_vocabulary(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Register the live pack's two new item categories, and put the vocabulary back after."""
    monkeypatch.setitem(settings._ACTIVE, "item", settings._ACTIVE["item"])
    monkeypatch.setitem(settings._ACTIVE, "merchant", settings._ACTIVE["merchant"])
    settings.register_categories(["photography", "travel"], [])
    yield


def _identity(categories: list[str]) -> dict[str, object]:
    return {
        "kind": "item_identity",
        "require": {"item_category_in": categories, "item_keywords_all": ["camera", "lens"]},
        "provenance": "the camera lens I chose",
        "confidence": "high",
    }


def test_the_repo_vocabulary_is_unchanged_until_a_pack_adds_to_it() -> None:
    assert settings.item_categories() >= settings.ITEM_CATEGORIES
    assert "photography" not in settings.ITEM_CATEGORIES  # the constant is the repo pack


@pytest.mark.usefixtures("live_vocabulary")
def test_every_reader_sees_a_registered_category() -> None:
    guard = Guard("Buy the camera lens I chose, for CHF 900 or less.")
    facet = guard.facet(_identity(["photography"]))
    assert facet is not None and facet["require"]["item_category_in"] == ["photography"]

    require = output_schema()["properties"]["intent_facets"]["items"]["properties"]["require"]
    assert "photography" in require["properties"]["item_category_in"]["items"]["enum"]

    exclusions = next(s for s in settings.catalogue() if s["key"] == "category_exclusion")
    assert {"photography", "travel"} <= set(exclusions["options"])


@pytest.mark.usefixtures("live_vocabulary")
def test_the_real_lens_passes_when_the_mandate_can_name_its_category() -> None:
    event = make_event(items=[LENS], billing_amount_chf="620.00", amount="620.00")
    named = check_item_matches_request(event, make_policy(facets=[_identity(["photography"])]))
    assert named.verdict is Verdict.PASS
    # What the model had to write before: the lens contradicts its own purchase.
    wrong = check_item_matches_request(event, make_policy(facets=[_identity(["electronics"])]))
    assert wrong.verdict is Verdict.VIOLATION


@pytest.mark.usefixtures("live_vocabulary")
def test_the_baseline_maps_words_onto_a_live_category_only_when_it_exists() -> None:
    instruction = "Buy the camera lens I chose. No flights, no insurance."
    ir = baseline.compile_instruction(instruction)
    kinds = {f["kind"]: f.get("require", {}) for f in ir["intent_facets"]}
    assert kinds.get("category_exclusion", {}).get("item_category_not_in") == ["travel"]


def test_without_the_live_pack_the_baseline_ignores_those_words() -> None:
    ir = baseline.compile_instruction("No flights, no insurance.")
    assert not any(f["kind"] == "category_exclusion" for f in ir["intent_facets"])
