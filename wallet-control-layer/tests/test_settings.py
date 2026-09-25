"""Editing the wallet policy, end to end through the service.

specs/customer-settings.md §9. The brief's *"tighten, update, or revoke"* split into three
operations with three different prices, and this file is where the price is proved:

    I2  a widening never applies without a new confirmation
    I4  an empty preferences layer moves nothing

The whole stack runs in one process — service, run loop, offline replica over an ASGI
transport — so what this proves is the integration, not just the classifier.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from leash.adapters.client import SandboxClient
from leash.adapters.datapack import DataPack, data_dir
from leash.adapters.history import HistoryIndex
from leash.audit.log import AuditLog
from leash.runtime.supervisor import Supervisor
from leash.service.app import app, get_supervisor
from sandbox.server import app as sandbox_app

PACK = DataPack.load()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")

#: SCEN0004's instruction caps a single order at CHF 400 — the mandate every amendment below
#: is measured against.
SCENARIO = "SCEN0004"
MANDATE_CAP = 400.0


@pytest.fixture()
def service(tmp_path: Path) -> Iterator[TestClient]:
    client = SandboxClient(
        base_url="http://sandbox.test",
        api_key="test-key",
        http=TestClient(
            sandbox_app,
            base_url="http://sandbox.test",
            headers={"Authorization": "Bearer test-key"},
        ),
    )
    client.reset()
    supervisor = Supervisor(
        client, pack=PACK, history=HISTORY, audit=AuditLog(tmp_path / "decisions.jsonl")
    )
    app.dependency_overrides[get_supervisor] = lambda: supervisor
    with TestClient(app) as c:
        yield c
    for session in supervisor.sessions.values():
        session.stop_requested.set()
    app.dependency_overrides.clear()
    client.reset()
    client.close()


def _confirmed_mandate(service: TestClient, scenario: str = SCENARIO) -> str:
    instruction = PACK.scenarios[scenario]["cardholder_instruction"]
    draft = service.post("/v1/mandates", json={"instruction": instruction})
    assert draft.status_code == 201, draft.text
    confirmed = service.post(
        f"/v1/mandates/{draft.json()['draft_id']}/confirm", json={"confirmed": True}
    )
    assert confirmed.status_code == 200, confirmed.text
    return str(confirmed.json()["mandate_id"])


def _binding_cap(service: TestClient, mandate_id: str, scope: str = "purchase") -> float | None:
    from leash.domain.policy import binding_cap

    ir = service.get(f"/v1/mandates/{mandate_id}").json()["ir"]
    found = binding_cap(ir.get("rules", []), scope)
    return float(found[0]["value"]) if found else None


# ------------------------------------------------------------------ the catalogue


def test_config_publishes_the_settings_catalogue(service: TestClient) -> None:
    """The UI mirrors this table; without it a control could be offered that reads nothing."""
    body = service.get("/v1/config").json()
    keys = {s["key"] for s in body["settings"]}
    assert "per_order_limit_chf" in keys and "merchant_type" in keys
    assert all(s["check"] for s in body["settings"])


def test_scenarios_endpoint_serves_what_the_ui_boots_from(service: TestClient) -> None:
    """leash-demo's live adapter calls this on boot and rendered nothing without it."""
    body = service.get("/v1/scenarios").json()
    assert len(body["scenarios"]) == len(PACK.scenarios)
    one = next(s for s in body["scenarios"] if s["id"] == SCENARIO)
    assert one["instruction"] == PACK.scenarios[SCENARIO]["cardholder_instruction"]
    # The opening demo beat highlights the spans that became rules, so `ir` is not optional.
    assert one["ir"]["rules"] and one["ir"]["rules"][0]["provenance"]


# ------------------------------------------------------------------ tighten


def test_a_narrowing_applies_immediately(service: TestClient) -> None:
    mandate_id = _confirmed_mandate(service)
    response = service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"per_order_limit_chf": 250}}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "tighten" and body["applied"] is True
    assert _binding_cap(service, mandate_id) == 250


def test_a_narrowing_keeps_what_the_customer_first_agreed_to(service: TestClient) -> None:
    """Add-only: the superseded rule stays on the record and §9 makes it inert."""
    mandate_id = _confirmed_mandate(service)
    service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"per_order_limit_chf": 250}}
    )
    ir = service.get(f"/v1/mandates/{mandate_id}").json()["ir"]
    values = sorted(float(r["value"]) for r in ir["rules"] if r["scope"] == "purchase")
    assert values == [250.0, MANDATE_CAP]


def test_a_narrowing_changes_the_verdict_it_is_supposed_to(service: TestClient) -> None:
    """AU0038 — CHF 391.50, compliant at 400 and not at 250. Beat 5 of the demo."""
    mandate_id = _confirmed_mandate(service)
    event = _event_for(service, "AU0038")

    before = service.post("/v1/decide", json={"event": event}).json()
    assert before["decision"] == "approve"

    service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"per_order_limit_chf": 250}}
    )
    after = service.post(
        "/v1/decide",
        json={"event": event, "policy": _policy_of(service, mandate_id)},
    ).json()
    assert after["decision"] == "decline"
    assert "CHF 250.00" in after["customer_message"]


def test_tightening_the_uncertainty_policy_applies(service: TestClient) -> None:
    mandate_id = _confirmed_mandate(service)
    body = service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"uncertainty_policy": "decline"}}
    ).json()
    assert body["kind"] == "tighten" and body["applied"] is True
    assert service.get(f"/v1/mandates/{mandate_id}").json()["ir"]["uncertainty_policy"] == "decline"


# ------------------------------------------------------------------ widen — I2


def test_a_widening_applies_nothing_until_it_is_confirmed(service: TestClient) -> None:
    """I2. The mandate must still read 400 while the draft is outstanding."""
    mandate_id = _confirmed_mandate(service)
    response = service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"per_order_limit_chf": 600}}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "widen"
    assert body["applied"] is False
    assert body["awaiting"] == "confirm"
    assert body["draft_id"] and body["draft_id"] != mandate_id
    assert _binding_cap(service, mandate_id) == MANDATE_CAP


def test_a_confirmed_widening_becomes_its_own_mandate(service: TestClient) -> None:
    """Not a patch: widening what an agent may do is a new consent moment."""
    mandate_id = _confirmed_mandate(service)
    draft_id = service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"per_order_limit_chf": 600}}
    ).json()["draft_id"]
    confirmed = service.post(f"/v1/mandates/{draft_id}/confirm", json={"confirmed": True})
    assert confirmed.status_code == 200, confirmed.text
    new_id = confirmed.json()["mandate_id"]
    assert _binding_cap(service, new_id) == 600
    # …and the mandate it came from is untouched.
    assert _binding_cap(service, mandate_id) == MANDATE_CAP


def test_dropping_a_rule_widens(service: TestClient) -> None:
    mandate_id = _confirmed_mandate(service)
    body = service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"no_additions": False}}
    ).json()
    assert body["kind"] == "widen" and body["applied"] is False


def test_a_widening_says_it_binds_from_the_next_run(service: TestClient) -> None:
    """The platform snapshots a mandate at run start, so silence here reads as a bug."""
    mandate_id = _confirmed_mandate(service)
    body = service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"per_order_limit_chf": 600}}
    ).json()
    assert "next run" in body["note"]


# ------------------------------------------------------------------ conflict


def test_an_amendment_that_allows_nothing_is_refused(service: TestClient) -> None:
    mandate_id = _confirmed_mandate(service, "SCEN0002")
    response = service.post(
        f"/v1/mandates/{mandate_id}/amend",
        json={"settings": {"merchant_type": {"merchant_category_in": []}}},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "preference_conflict"


# ------------------------------------------------------------------ preferences


def test_preferences_start_empty_and_round_trip(service: TestClient) -> None:
    assert service.get("/v1/preferences").json()["preferences"] == {}
    saved = service.put("/v1/preferences", json={"per_order_limit_chf": 300, "no_additions": True})
    assert saved.status_code == 200, saved.text
    body = service.get("/v1/preferences").json()
    assert body["preferences"]["per_order_limit_chf"] == 300
    assert {r["setting"] for r in body["rows"]} == {"per_order_limit_chf", "no_additions"}


def test_a_standing_preference_binds_when_it_is_the_tightest(service: TestClient) -> None:
    """Layer 1 under layer 2: CHF 300 preference against a CHF 400 instruction."""
    service.put("/v1/preferences", json={"per_order_limit_chf": 300})
    _confirmed_mandate(service)
    event = _event_for(service, "AU0038")  # CHF 391.50 — inside 400, outside 300
    body = service.post("/v1/decide", json={"event": event}).json()
    assert body["decision"] == "decline"
    # §5 — the message must say *which* limit bound, or the customer reads their own
    # instruction, sees CHF 400, and concludes the engine is wrong.
    assert "you set in your preferences" in body["customer_message"]


def test_a_looser_preference_never_widens_the_mandate(service: TestClient) -> None:
    """I1 at the service level: the mandate's own cap still binds."""
    service.put("/v1/preferences", json={"per_order_limit_chf": 5000})
    _confirmed_mandate(service)
    event = _event_for(service, "AU0037")  # CHF 520.00 — outside the mandate's 400
    assert service.post("/v1/decide", json={"event": event}).json()["decision"] == "decline"


def test_a_setting_that_belongs_to_one_errand_is_refused_as_standing(service: TestClient) -> None:
    response = service.put("/v1/preferences", json={"item_attribute": {"size": 43}})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "setting_not_standing"


def test_a_setting_no_check_reads_is_refused(service: TestClient) -> None:
    """§7 — an inert control is worse than a missing one: it looks like protection."""
    response = service.put("/v1/preferences", json={"merchant_country_in": ["CH"]})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_setting"


def test_an_unknown_shop_category_is_refused_with_the_offending_value(service: TestClient) -> None:
    response = service.put(
        "/v1/preferences", json={"merchant_type": {"merchant_category_in": ["speakeasy"]}}
    )
    assert response.status_code == 422, response.text
    assert "speakeasy" in response.json()["error"]["message"]


def test_profile_candidates_are_proposals_and_name_where_they_came_from(
    service: TestClient,
) -> None:
    """§8 — nothing derived from a profile is enforced without a tap."""
    body = service.get("/v1/preferences/candidates", params={"scenario_id": SCENARIO}).json()
    assert body["customer"]["persona_name"]
    assert body["account"]["per_transaction_limit_chf"] > 0
    for candidate in body["candidates"]:
        assert candidate["source"] == "profile" and candidate["quote"]
    # Nothing was applied by asking.
    assert service.get("/v1/preferences").json()["preferences"] == {}


def test_fields_we_will_not_offer_say_why(service: TestClient) -> None:
    """A judge asking "why isn't that a setting?" gets an answer from the API."""
    body = service.get("/v1/preferences/candidates", params={"scenario_id": SCENARIO}).json()
    reasons = {row["field"]: row["why"] for row in body["not_offered"]}
    # `shopping_preferences` used to be on this list. It is compiled now — the deny-list
    # check gave it somewhere to land — so it must NOT still be claiming to be unusable.
    assert "shopping_preferences" not in reasons
    assert "travel_pattern" in reasons and "home_region" in reasons
    assert all(reasons.values())


def test_profile_prose_becomes_a_candidate_that_quotes_it(service: TestClient) -> None:
    """CU0001 — "avoids gift vouchers". The profile says it; no instruction ever does.

    SCEN0001 belongs to Alex Meier, whose profile is the one sentence in the pack that names
    a category they refuse. It goes through the same compiler as an instruction, so the guard
    already requires the proposal to quote the words it came from.
    """
    body = service.get("/v1/preferences/candidates", params={"scenario_id": "SCEN0001"}).json()
    exclusion = next((c for c in body["candidates"] if c["setting"] == "category_exclusion"), None)
    assert exclusion is not None, [c["setting"] for c in body["candidates"]]
    assert exclusion["value"] == {"item_category_not_in": ["gift_card"]}
    assert exclusion["quote"] == "avoids gift vouchers"
    assert exclusion["source"] == "profile"
    # Proposed, never applied.
    assert service.get("/v1/preferences").json()["preferences"] == {}


def test_an_accepted_exclusion_declines_what_the_mandate_alone_would_approve(
    service: TestClient,
) -> None:
    """The whole point of the deny-list: a rule no instruction in the pack states."""
    saved = service.put(
        "/v1/preferences",
        json={"category_exclusion": {"item_category_not_in": ["groceries"]}},
    )
    assert saved.status_code == 200, saved.text
    event = _event_for(service, "AU0002")  # a compliant grocery delivery
    body = service.post("/v1/decide", json={"event": event}).json()
    assert body["decision"] == "decline"
    assert "which you do not buy" in body["customer_message"]


def test_spending_hours_decline_an_order_outside_them(service: TestClient) -> None:
    """AU0027 lands at 02:00 UTC. A customer who said daytime-only sees it refused."""
    saved = service.put(
        "/v1/preferences", json={"spending_hours": {"hours_from": 7, "hours_to": 22}}
    )
    assert saved.status_code == 200, saved.text
    body = service.post("/v1/decide", json={"event": _event_for(service, "AU0027")}).json()
    assert body["decision"] == "decline"
    assert "you only buy between" in body["customer_message"]


def test_asking_less_often_is_a_widening_that_must_be_confirmed(service: TestClient) -> None:
    """`make tune-sweep`: the board is flat to 2.0 and four step-ups become approvals at 2.5.

    So "ask me less" is the clearest case in the build where a widening has a countable price.
    """
    mandate_id = _confirmed_mandate(service)
    body = service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"step_up_threshold": "less"}}
    ).json()
    assert body["kind"] == "widen" and body["applied"] is False

    tighter = service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"step_up_threshold": "more"}}
    ).json()
    assert tighter["kind"] == "tighten" and tighter["applied"] is True


# ------------------------------------------------------------------ live runs


def _run_to_completion(service: TestClient, scenario: str) -> dict[str, Any]:
    """A whole scenario. The platform holds a run at every step-up, so `auto_resolve`
    answers them — as a decline, which leaves every window where the board has it."""
    run = service.post("/v1/runs", json={"scenario_id": scenario, "auto_resolve": "decline"})
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        body = service.get(f"/v1/runs/{run_id}").json()
        if body["status"] == "complete":
            return body
        time.sleep(0.05)
    service.post(f"/v1/runs/{run_id}/stop")
    raise AssertionError(f"run {run_id} did not complete")


def test_a_standing_preference_reaches_a_live_run(service: TestClient) -> None:
    """The layers must reach the *run loop*, not just the what-if endpoint.

    They did not. `start_run` composed every layer onto the session, and then the runner
    rebuilt the policy from the mandate snapshot the event carries — which only ever holds
    the mandate's own rules. A standing ceiling was composed, stored, and never consulted.
    Every test before this one went through `POST /v1/decide`, which took a different path.
    """
    before = _run_to_completion(service, "SCEN0001")["counters"]

    saved = service.put("/v1/preferences", json={"per_order_limit_chf": 50})
    assert saved.status_code == 200, saved.text
    after = _run_to_completion(service, "SCEN0001")["counters"]

    assert after["decline"] > before["decline"], (
        f"a standing CHF 50 per-order ceiling changed nothing: {before} -> {after}"
    )


def test_a_standing_period_ceiling_reaches_a_live_run(service: TestClient) -> None:
    """A monthly ceiling the errand never mentioned — the point of Tier 2.

    SCEN0001 states `CHF 300 across seven days`; this adds `CHF 200 across thirty` on top.
    Both windows are enforced, so orders inside the weekly limit can still be refused.
    """
    before = _run_to_completion(service, "SCEN0001")["counters"]

    saved = service.put(
        "/v1/preferences", json={"period_limit_chf": {"value": 200, "period_days": 30}}
    )
    assert saved.status_code == 200, saved.text
    body = _run_to_completion(service, "SCEN0001")

    assert body["counters"]["decline"] > before["decline"]
    assert [w["period_days"] for w in body["window"]["windows"]] == [7, 30]
    assert any("across 30 days" in d["customer_message"] for d in body["decisions"])


def test_a_period_preference_needs_a_window(service: TestClient) -> None:
    """A period rule with no window reaches the check as `unknown` — never stored."""
    response = service.put("/v1/preferences", json={"period_limit_chf": {"value": 200}})
    assert response.status_code == 422, response.text
    assert "days" in response.json()["error"]["message"]


def test_a_decision_says_what_it_was_about(service: TestClient) -> None:
    """A verdict with no subject cannot be rendered, and cannot be audited either.

    `/v1/decisions` carried the checks and the message but nothing naming the purchase, so a
    reader could not tell what was bought or from whom — and the demo UI's live mode had no
    way to draw the agent's basket. Found rehearsing the UI on 2026-09-21.
    """
    body = _run_to_completion(service, SCENARIO)
    decisions = service.get("/v1/decisions", params={"run_id": body["run_id"]}).json()["decisions"]
    subject = decisions[0]["authorization"]
    assert subject["merchant_name"] and subject["merchant_id"]
    assert subject["billing_amount_chf"] and subject["currency"]
    assert subject["items"] and subject["items"][0]["name"]
    assert subject["timestamp"]


def test_the_score_names_the_threshold_that_was_actually_applied(service: TestClient) -> None:
    """A customer may set their own bar; a score shown against a bar nobody applied misleads."""
    saved = service.put("/v1/preferences", json={"step_up_threshold": "more"})
    assert saved.status_code == 200, saved.text
    body = _run_to_completion(service, SCENARIO)
    decisions = service.get("/v1/decisions", params={"run_id": body["run_id"]}).json()["decisions"]
    assert {d["score"]["step_up_threshold"] for d in decisions} == {1.0}


def test_counters_separate_the_verdict_from_the_outcome(service: TestClient) -> None:
    """The cardholder's screen needs outcomes; an audit reader needs verdicts.

    They stop agreeing the moment a human answers, and `final` is the half that moves. Without
    it the phone kept reporting a step-up as waiting after it had been answered.
    """
    run = service.post("/v1/runs", json={"scenario_id": SCENARIO})
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]

    # The run is held at each step-up, so play the customer: while it waits, the one holding
    # it is the one the phone shows; answer it, and it moves from waiting to declined.
    asked = 0
    deadline = time.monotonic() + 20.0
    while (body := service.get(f"/v1/runs/{run_id}").json())["status"] != "complete":
        assert time.monotonic() < deadline, "run did not complete"
        for pending in body["step_ups"]:
            assert body["counters"]["final"]["waiting"] == 1
            service.post(
                f"/v1/step-ups/{pending['authorization_id']}/resolve", json={"decision": "decline"}
            )
            asked += 1
        time.sleep(0.05)

    after = body["counters"]
    assert asked == after["step_up"] > 0
    assert after["final"]["waiting"] == 0
    assert after["final"]["declined"] == after["decline"] + after["step_up"]
    # The engine's own verdicts never move — that is what makes them an audit trail.
    assert after["step_up"] == sum(1 for d in body["decisions"] if d["decision"] == "step_up")
    assert after["final"]["approved"] == after["approve"]


def test_a_narrowing_reaches_a_run_already_under_way(service: TestClient) -> None:
    """A tightening can only reduce what the agent may do, so it lands on the next order."""
    mandate_id = _confirmed_mandate(service)
    run = service.post(
        "/v1/runs",
        json={"scenario_id": SCENARIO, "mandate_id": mandate_id, "auto_resolve": "decline"},
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]

    body = service.post(
        f"/v1/mandates/{mandate_id}/amend", json={"settings": {"per_order_limit_chf": 250}}
    ).json()
    assert run_id in body["runs_refreshed"]

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if service.get(f"/v1/runs/{run_id}").json()["status"] == "complete":
            break
        time.sleep(0.05)
    service.post(f"/v1/runs/{run_id}/stop")


# ------------------------------------------------------------------ helpers


def _event_for(service: TestClient, source_id: str) -> dict[str, Any]:
    """Build one authorization.request straight from the pack, with the scenario's mandate."""
    from leash.compile.baseline import compile_instruction
    from sandbox.fixtures import build_event

    attempt = next(a for a in PACK.attempts if a["authorization_id"] == source_id)
    ir = compile_instruction(PACK.scenarios[attempt["scenario_id"]]["cardholder_instruction"])
    return build_event(
        PACK,
        attempt,
        run_id="RUN_TEST",
        mandate={
            "mandate_id": "TM_TEST",
            "status": "active",
            "customer_id": "CU0000",
            "card_id": "CA0000",
            "profile_id": "PROFILE_TEST",
            "instruction": ir["source_instruction"],
            "hard_rules": [
                {
                    k: v
                    for k, v in r.items()
                    if k in ("field", "operator", "value", "currency", "scope", "period_days")
                }
                for r in ir["rules"]
            ],
            "uncertainty_policy": ir["uncertainty_policy"],
        },
        approved_spend_in_period_chf=0.0,
        recent_authorizations=[],
        deadline_seconds=8,
        request_seq=1,
    )


def _policy_of(service: TestClient, mandate_id: str) -> dict[str, Any]:
    ir = service.get(f"/v1/mandates/{mandate_id}").json()["ir"]
    return {
        "hard_rules": ir.get("rules", []),
        "intent_facets": ir.get("intent_facets", []),
        "uncertainty_policy": ir.get("uncertainty_policy", "ask"),
    }
