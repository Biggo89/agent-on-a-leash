"""The live instructions the compiler did not read — specs/live-instructions.md.

Since 2026-09-24 the organizers' API serves ten team-specific scenarios. Each class below is one
of their sentences the compiler used to misread, pinned end to end: the baseline's reading,
the guard and safety floor over a model answer, the check that enforces it, and the edit
paths (compose, amend, apply_settings) that must not lose it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from leash.compile.baseline import compile_instruction, to_mandate_payload
from leash.compile.contract import accept, apply_safety_floor
from leash.domain import settings
from leash.domain.amend import TIGHTEN, WIDEN, classify
from leash.domain.checks.limits import check_per_order_limit
from leash.domain.checks.order import check_order_terms
from leash.domain.compose import Layer, compose
from leash.domain.preferences import apply_settings
from leash.domain.types import Verdict
from tests.builders import make_event, make_policy

SCEN0124 = (
    "Book me a hotel in Munich for 3 nights from 10 September to 13 September, at most CHF 200 "
    "per night, refundable rate only. No flights, no insurance. Ask me when uncertain."
)


def _facet(ir: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((f for f in ir["intent_facets"] if f["kind"] == kind), None)


# ------------------------------------------------------------ §A: a price per night


class TestPricePerNight:
    def test_the_baseline_derives_the_order_cap_from_the_price_and_the_count(self) -> None:
        ir = compile_instruction(SCEN0124)

        (cap,) = ir["rules"]
        assert cap["value"] == 600.0 and cap["scope"] == "purchase"
        assert cap["derived"] == {
            "unit": "night",
            "count": 3,
            "unit_amount": 200.0,
            "unit_currency": "CHF",
        }
        quotes = [entry["quote"] for entry in cap["provenance"]]
        assert quotes == ["CHF 200 per night", "3 nights"]
        assert all(quote in SCEN0124 for quote in quotes)
        assert "3 nights × CHF 200.00 per night" in ir["guidance"][0]

    def test_a_stay_under_the_nightly_price_is_within_the_cap(self) -> None:
        (cap,) = compile_instruction(SCEN0124)["rules"]
        policy = make_policy(hard_rules=[cap])

        at_190 = check_per_order_limit(make_event(billing_amount_chf="570.00"), policy)
        at_210 = check_per_order_limit(make_event(billing_amount_chf="630.00"), policy)

        assert at_190.verdict is Verdict.PASS
        assert at_210.verdict is Verdict.VIOLATION
        assert "(3 nights × CHF 200.00)" in at_210.detail

    def test_the_safety_floor_holds_the_derived_cap_and_keeps_the_models(self) -> None:
        answer = {
            "rules": [
                {
                    "field": "billing_amount_chf",
                    "operator": "<=",
                    "value": 600,
                    "scope": "purchase",
                    "confidence": "high",
                    "provenance": "at most CHF 200 per night",
                }
            ],
            "intent_facets": [],
        }
        model_ir = accept(answer, SCEN0124, model="m")
        assert model_ir is not None

        floored = apply_safety_floor(model_ir, compile_instruction(SCEN0124))

        assert [r["value"] for r in floored["rules"]] == [600]
        notes = " ".join(floored["compiler_notes"])
        assert "unsupported_cap_removed" not in notes
        assert "safety_floor_applied" not in notes

    def test_a_model_that_misses_the_cap_gets_the_derived_one_not_the_nightly_price(
        self,
    ) -> None:
        refundable = {
            "kind": "order_terms",
            "require": {"cancellable": True},
            "confidence": "high",
            "provenance": "refundable rate only",
        }
        model_ir = accept({"rules": [], "intent_facets": [refundable]}, SCEN0124, model="m")
        assert model_ir is not None

        floored = apply_safety_floor(model_ir, compile_instruction(SCEN0124))

        assert [r["value"] for r in floored["rules"]] == [600.0]

    def test_the_arithmetic_never_reaches_the_platform(self) -> None:
        payload = to_mandate_payload(compile_instruction(SCEN0124))

        assert payload["hard_rules"] == [
            {
                "field": "billing_amount_chf",
                "operator": "<=",
                "value": 600.0,
                "currency": "CHF",
                "scope": "purchase",
            }
        ]

    def test_without_a_count_the_nightly_price_stays_the_cap_and_we_ask(self) -> None:
        ir = compile_instruction("Hotels at most CHF 200 per night. Ask me when uncertain.")

        assert [r["value"] for r in ir["rules"]] == [200.0]
        assert any("How many nights" in q for q in ir["open_questions"])

    def test_a_foreign_nightly_price_converts_the_total_once(self) -> None:
        ir = compile_instruction("Two nights in Vienna, at most EUR 150 a night.")

        (cap,) = ir["rules"]
        assert cap["value"] == 285.0  # EUR 300 x 0.95, rounded down once
        assert cap["stated"] == {"amount": 300.0, "currency": "EUR", "rate": 0.95}


# ------------------------------------------------------------ §A: refundable rate only


class TestRefundableRateOnly:
    def test_the_baseline_reads_refundable_as_the_cancellation_term(self) -> None:
        terms = _facet(compile_instruction(SCEN0124), "order_terms")

        assert terms is not None
        assert terms["require"] == {"cancellable": True}
        assert terms["provenance"] == "refundable rate only"

    def test_non_refundable_never_asks_for_a_refundable_order(self) -> None:
        for instruction in (
            "Book the non-refundable rate if it is cheaper, at most CHF 300.",
            "A nonrefundable fare is fine, up to CHF 300 per order.",
            "Tickets that are not refundable are fine, up to CHF 80 per order.",
        ):
            assert _facet(compile_instruction(instruction), "order_terms") is None, instruction

    def test_a_return_window_and_refundable_share_one_facet_and_quote_both(self) -> None:
        ir = compile_instruction(
            "Boots only if they can be returned within 14 days or more, refundable only."
        )
        terms = _facet(ir, "order_terms")

        assert terms is not None
        assert terms["require"] == {"return_window_days_min": 14, "cancellable": True}
        assert [e["quote"] for e in terms["provenance"]] == [
            "returned within 14 days or more",
            "refundable only",
        ]

    def test_the_guard_keeps_true_and_drops_anything_else(self) -> None:
        def answer(value: object) -> dict[str, Any]:
            return {
                "rules": [
                    {
                        "field": "billing_amount_chf",
                        "operator": "<=",
                        "value": 600,
                        "scope": "purchase",
                        "confidence": "high",
                        "provenance": "at most CHF 200 per night",
                    }
                ],
                "intent_facets": [
                    {
                        "kind": "order_terms",
                        "require": {"cancellable": value},
                        "confidence": "high",
                        "provenance": "refundable rate only",
                    }
                ],
            }

        kept = accept(answer(True), SCEN0124, model="m")
        dropped = accept(answer(False), SCEN0124, model="m")

        assert kept is not None and _facet(kept, "order_terms") == {
            "kind": "order_terms",
            "provenance": "refundable rate only",
            "confidence": "high",
            "require": {"cancellable": True},
        }
        assert dropped is not None and _facet(dropped, "order_terms") is None

    def test_a_non_refundable_room_is_declined_on_the_trusted_field(self) -> None:
        policy = make_policy(facets=[{"kind": "order_terms", "require": {"cancellable": True}}])
        room = [
            {
                "item_name": "Hotel room, non-refundable rate",
                "item_category": "hotel",
                "quantity": 3,
                "unit_price": 180.0,
            }
        ]

        result = check_order_terms(make_event(items=room, order_cancellable="false"), policy)

        assert not isinstance(result, tuple)
        assert result.reason_code == "order_not_cancellable"

    def test_a_second_layer_does_not_lose_the_term(self) -> None:
        mandate = {
            "rules": [],
            "intent_facets": [
                {"kind": "order_terms", "require": {"cancellable": True}, "provenance": "x"}
            ],
        }
        standing = {
            "rules": [],
            "intent_facets": [{"kind": "order_terms", "require": {"return_window_days_min": 14}}],
        }

        merged = compose(Layer.of("preferences", standing), Layer.of("mandate", mandate)).policy
        terms = _facet({"intent_facets": merged["intent_facets"]}, "order_terms")

        assert terms is not None
        assert terms["require"] == {"return_window_days_min": 14, "cancellable": True}

    def test_editing_the_return_window_keeps_the_refundable_term(self) -> None:
        ir = compile_instruction(SCEN0124)

        edited = apply_settings(ir, {"order_terms": {"return_window_days_min": 14}})
        cleared = apply_settings(ir, {"order_terms": None})

        assert _facet(edited, "order_terms")["require"] == {  # type: ignore[index]
            "cancellable": True,
            "return_window_days_min": 14,
        }
        assert _facet(cleared, "order_terms")["require"] == {"cancellable": True}  # type: ignore[index]

    def test_dropping_the_term_is_a_widening_and_adding_it_a_tightening(self) -> None:
        with_term = {"intent_facets": [{"kind": "order_terms", "require": {"cancellable": True}}]}
        with_window = {
            "intent_facets": [{"kind": "order_terms", "require": {"return_window_days_min": 14}}]
        }
        both = {
            "intent_facets": [
                {
                    "kind": "order_terms",
                    "require": {"return_window_days_min": 14, "cancellable": True},
                }
            ]
        }

        assert classify(both, with_window).kind == WIDEN
        assert classify(with_window, both).kind == TIGHTEN
        assert classify(with_term, with_term).changes == ()


# ------------------------------------------------------------ §B: no alcohol

SCEN0117 = (
    "Groceries and everyday household items only, maximum CHF 100 per order, from the shops I "
    "use. No alcohol, no gift cards, no cosmetics. When in doubt, ask."
)
LIVE_CATALOGUE = {
    "groceries": ["Wine and spirits", "Drinks and snacks", "Bakery and dairy order"],
    "gift_card": ["Prepaid gift card"],
    "household": ["Light bulbs and batteries"],
}


def _exclusion_answer(**require: object) -> dict[str, Any]:
    return {
        "rules": [
            {
                "field": "billing_amount_chf",
                "operator": "<=",
                "value": 100,
                "scope": "purchase",
                "confidence": "high",
                "provenance": "maximum CHF 100 per order",
            }
        ],
        "intent_facets": [
            {
                "kind": "category_exclusion",
                "require": require,
                "confidence": "high",
                "provenance": "No alcohol",
            }
        ],
    }


class TestNoAlcohol:
    def test_the_baseline_keeps_every_exclusion_in_one_facet(self) -> None:
        exclusion = _facet(compile_instruction(SCEN0117), "category_exclusion")

        assert exclusion is not None
        assert exclusion["require"]["item_keywords_none"] == ["wine", "spirits", "beer", "liquor"]
        assert "gift_card" in exclusion["require"]["item_category_not_in"]
        assert [e["quote"] for e in exclusion["provenance"]] == [
            "No alcohol",
            "no gift cards",
            "no cosmetics",
        ]

    def test_gift_cards_are_read_as_a_phrase_never_as_the_word_card(self) -> None:
        assert _facet(compile_instruction("Groceries only, no gift cards."), "category_exclusion")
        assert not _facet(
            compile_instruction("Groceries only, no card fees."), "category_exclusion"
        )

    def test_the_guard_keeps_words_that_name_a_product(self) -> None:
        ir = accept(
            _exclusion_answer(item_keywords_none=["Wine", "wine and spirits", "beer"]),
            SCEN0117,
            model="m",
            catalogue=LIVE_CATALOGUE,
        )
        assert ir is not None
        exclusion = _facet(ir, "category_exclusion")

        assert exclusion is not None
        # "and" is gone from the phrase, and beer names no product in this catalogue.
        assert exclusion["require"]["item_keywords_none"] == ["wine", "wine spirits"]
        assert any("excluded_word_matches_no_product: 'beer'" in n for n in ir["compiler_notes"])

    def test_an_exclusion_that_names_nothing_is_a_question_not_a_rule(self) -> None:
        ir = accept(
            _exclusion_answer(item_keywords_none=["tobacco"]),
            SCEN0117,
            model="m",
            catalogue=LIVE_CATALOGUE,
        )
        assert ir is not None

        assert _facet(ir, "category_exclusion") is None
        assert any("never be bought" in q for q in ir["open_questions"])

    def test_the_exclusion_declines_wine_and_nothing_else(self) -> None:
        from leash.domain.checks.exclusion import check_category_exclusion

        exclusion = _facet(compile_instruction(SCEN0117), "category_exclusion")
        policy = make_policy(facets=[exclusion])

        def verdicts(name: str, category: str) -> list[str]:
            items = [{"item_name": name, "item_category": category}]
            produced = check_category_exclusion(make_event(items=items), policy)
            results = produced if isinstance(produced, tuple) else (produced,)
            return [str(r.verdict) for r in results]

        assert "violation" in verdicts("Wine and spirits", "groceries")
        assert verdicts("Drinks and snacks", "groceries") == ["pass", "pass"]
        assert verdicts("Light bulbs and batteries", "household") == ["pass", "pass"]

    def test_edits_and_layers_keep_the_product_words(self) -> None:
        ir = compile_instruction(SCEN0117)

        edited = apply_settings(ir, {"category_exclusion": {"item_category_not_in": ["books"]}})
        exclusion = _facet(edited, "category_exclusion")
        assert exclusion is not None
        assert exclusion["require"]["item_keywords_none"] == ["wine", "spirits", "beer", "liquor"]

        words = {
            "intent_facets": [
                {"kind": "category_exclusion", "require": {"item_keywords_none": ["wine"]}}
            ]
        }
        fewer = {
            "intent_facets": [{"kind": "category_exclusion", "require": {"item_keywords_none": []}}]
        }
        assert classify(words, fewer).kind == WIDEN
        assert classify(fewer, words).kind == TIGHTEN


# ------------------------------------------------------------ §C: never at the weekend

SCEN0113 = (
    "Weeknight dinners only: one delivery a day, CHF 40 maximum including the delivery fee, "
    "from my usual services. Never at the weekend. Ask me if something doesn't fit."
)
WORKDAYS = ["mon", "tue", "wed", "thu", "fri"]


class TestNeverAtTheWeekend:
    def test_the_baseline_reads_the_days_and_only_asks_about_dinner(self) -> None:
        ir = compile_instruction(SCEN0113)
        hours = _facet(ir, "spending_hours")

        assert hours is not None
        assert hours["require"] == {"weekdays": WORKDAYS}
        assert hours["provenance"] == "Never at the weekend"
        assert any("dinners" in q for q in ir["open_questions"])

    def test_a_weekend_trip_is_not_a_rule_about_days(self) -> None:
        for instruction in (
            "Book a weekend getaway in Lucerne for CHF 400.",
            "No weekend surcharge accepted, CHF 60 per order.",
        ):
            assert _facet(compile_instruction(instruction), "spending_hours") is None

    def test_a_saturday_delivery_is_declined_end_to_end(self) -> None:
        from leash.domain.evaluator import evaluate

        ir = compile_instruction(SCEN0113)
        policy = {
            "hard_rules": ir["rules"],
            "uncertainty_policy": "ask",
            "intent_facets": ir["intent_facets"],
        }
        saturday = make_event(
            timestamp=datetime.fromisoformat("2026-08-22T17:30:00+00:00"),
            billing_amount_chf="32.00",
            amount="32.00",
        )
        record = evaluate(saturday, policy)

        assert str(record.decision) == "decline"
        assert "outside_spending_days" in record.reason_codes

    def test_the_guard_reads_day_names_and_refuses_a_day_it_cannot_read(self) -> None:
        def answer(days: list[str]) -> dict[str, Any]:
            return {
                "rules": [
                    {
                        "field": "billing_amount_chf",
                        "operator": "<=",
                        "value": 40,
                        "scope": "purchase",
                        "confidence": "high",
                        "provenance": "CHF 40 maximum",
                    }
                ],
                "intent_facets": [
                    {
                        "kind": "spending_hours",
                        "require": {"weekdays": days},
                        "confidence": "high",
                        "provenance": "Never at the weekend",
                    }
                ],
            }

        read = accept(answer(["Monday", "tue", "WED", "thursday", "fri"]), SCEN0113, model="m")
        unread = accept(answer(["mon", "fri."]), SCEN0113, model="m")

        assert read is not None and _facet(read, "spending_hours")["require"] == {
            "weekdays": WORKDAYS
        }  # type: ignore[index]
        assert unread is not None and _facet(unread, "spending_hours") is None
        assert any("which days" in q.lower() for q in unread["open_questions"])

    def test_a_standing_hours_preference_and_the_errands_days_both_hold(self) -> None:
        standing = {
            "intent_facets": [
                {"kind": "spending_hours", "require": {"hours_from": 7, "hours_to": 22}}
            ]
        }
        errand = {"intent_facets": [{"kind": "spending_hours", "require": {"weekdays": WORKDAYS}}]}
        weekend_only = {
            "intent_facets": [{"kind": "spending_hours", "require": {"weekdays": ["sat", "sun"]}}]
        }

        merged = compose(Layer.of("preferences", standing), Layer.of("mandate", errand))
        clash = compose(Layer.of("preferences", weekend_only), Layer.of("mandate", errand))

        hours = _facet({"intent_facets": merged.policy["intent_facets"]}, "spending_hours")
        assert hours is not None
        assert hours["require"] == {"hours_from": 7, "hours_to": 22, "weekdays": WORKDAYS}
        assert clash.conflicts

    def test_edits_keep_the_days_and_amend_compares_them(self) -> None:
        ir = compile_instruction(SCEN0113)
        edited = apply_settings(ir, {"spending_hours": {"hours_from": 17, "hours_to": 22}})
        assert _facet(edited, "spending_hours")["require"] == {  # type: ignore[index]
            "weekdays": WORKDAYS,
            "hours_from": 17,
            "hours_to": 22,
        }

        days = {"intent_facets": [{"kind": "spending_hours", "require": {"weekdays": WORKDAYS}}]}
        fewer = {"intent_facets": [{"kind": "spending_hours", "require": {"weekdays": ["mon"]}}]}
        assert classify(days, fewer).kind == TIGHTEN
        assert classify(fewer, days).kind == WIDEN
        assert classify(days, days).changes == ()


# ------------------------------------------------------------ the model, as measured live


class TestWhatTheModelWroteLive:
    """Shapes the model returned for the live instructions on 2026-09-24, pinned offline."""

    HOTEL_CATALOGUE = {
        "hotel": ["Hotel room, refundable rate", "Hotel room, non-refundable rate", "Hostel bed"],
        "travel": ["Economy flight ticket", "Travel insurance add-on"],
        "sporting_goods": ["Hiking boots"],
    }

    @staticmethod
    def _answer(*facets: dict[str, Any]) -> dict[str, Any]:
        return {
            "rules": [
                {
                    "field": "billing_amount_chf",
                    "operator": "<=",
                    "value": 600,
                    "scope": "purchase",
                    "confidence": "high",
                    "provenance": "at most CHF 200 per night",
                }
            ],
            "intent_facets": list(facets),
        }

    def test_two_exclusions_are_merged_not_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `travel` is a live-pack category; register it as `DataPack.load` does for that pack.
        monkeypatch.setitem(settings._ACTIVE, "item", settings._ACTIVE["item"])
        settings.register_categories(["travel"], [])
        ir = accept(
            self._answer(
                {
                    "kind": "category_exclusion",
                    "require": {"item_keywords_none": ["flight"]},
                    "confidence": "high",
                    "provenance": "No flights",
                },
                {
                    "kind": "category_exclusion",
                    "require": {"item_category_not_in": ["travel"]},
                    "confidence": "high",
                    "provenance": "no insurance",
                },
            ),
            SCEN0124,
            model="m",
            catalogue=self.HOTEL_CATALOGUE,
        )
        assert ir is not None
        exclusion = _facet(ir, "category_exclusion")

        assert exclusion is not None
        assert exclusion["require"] == {
            "item_category_not_in": ["travel"],
            "item_keywords_none": ["flight"],
        }
        assert [e["quote"] for e in exclusion["provenance"]] == ["No flights", "no insurance"]
        assert "facet_merged: category_exclusion" in ir["compiler_notes"]

    def test_a_second_item_identity_is_still_dropped(self) -> None:
        identity = {
            "kind": "item_identity",
            "require": {"item_category_in": ["hotel"]},
            "confidence": "high",
            "provenance": "a hotel",
        }
        ir = accept(self._answer(identity, identity), SCEN0124, model="m")
        assert ir is not None

        assert [f["kind"] for f in ir["intent_facets"]] == ["item_identity"]
        assert "duplicate_facet: item_identity" in ir["compiler_notes"]

    def test_a_city_is_never_a_product_word(self) -> None:
        identity = {
            "kind": "item_identity",
            "require": {"item_category_in": ["hotel"], "item_keywords_all": ["munich"]},
            "confidence": "high",
            "provenance": "hotel in Munich",
        }
        ir = accept(
            self._answer(identity),
            SCEN0124,
            model="m",
            catalogue=self.HOTEL_CATALOGUE,
            places={"munich", "lucerne"},
        )
        assert ir is not None
        facet = _facet(ir, "item_identity")

        assert facet is not None and facet["require"] == {"item_category_in": ["hotel"]}
        assert any("'Munich' is not enforced" in q for q in ir["open_questions"])
        assert "keyword_is_a_place: 'munich'" in ir["compiler_notes"]

    def test_a_thing_the_catalogue_does_not_sell_still_declines(self) -> None:
        identity = {
            "kind": "item_identity",
            "require": {"item_category_in": ["sporting_goods"], "item_keywords_all": ["kayak"]},
            "confidence": "high",
            "provenance": "a hotel",
        }
        ir = accept(
            self._answer(identity),
            SCEN0124,
            model="m",
            catalogue=self.HOTEL_CATALOGUE,
            places={"munich"},
        )
        assert ir is not None

        assert _facet(ir, "item_identity")["require"]["item_keywords_all"] == ["kayak"]  # type: ignore[index]
