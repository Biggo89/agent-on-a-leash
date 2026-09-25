"""The live run loop's failure paths — TASKS.md Phase 3, `handle 409/408, resume from cursor`.

`make replay` only ever exercised the happy path against a replica that answered instantly.
These are the four things the day will actually do to us, driven directly at the runner so
each one is isolated: redelivery, a late decision, a conflicting decision, and a restart.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from leash.adapters.client import SandboxClient, UpstreamResult
from leash.adapters.datapack import DataPack, data_dir
from leash.adapters.history import HistoryIndex
from leash.audit.log import AuditLog
from leash.compile.baseline import compile_instruction, to_mandate_payload
from leash.domain.money import Money
from leash.runtime.runner import ScenarioRunner
from leash.runtime.session import RunSession
from sandbox.server import app as sandbox_app

PACK = DataPack.load()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")


@pytest.fixture()
def client() -> Iterator[SandboxClient]:
    c = SandboxClient(
        base_url="http://sandbox.test",
        api_key="test-key",
        http=TestClient(
            sandbox_app,
            base_url="http://sandbox.test",
            headers={"Authorization": "Bearer test-key"},
        ),
    )
    c.reset()
    yield c
    c.reset()
    c.close()


def _start(client: SandboxClient, scenario_id: str = "SCEN0000") -> tuple[RunSession, dict]:
    instruction = PACK.scenarios[scenario_id]["cardholder_instruction"]
    ir = compile_instruction(instruction)
    draft = client.create_mandate(to_mandate_payload(ir))
    mandate = client.confirm_mandate(draft["draft_id"])
    run = client.start_run(scenario_id, mandate["mandate_id"])
    session = RunSession(
        run_id=run["run_id"],
        scenario_id=scenario_id,
        mandate_id=mandate["mandate_id"],
        policy={
            "hard_rules": [
                {k: v for k, v in r.items() if k not in ("provenance", "confidence")}
                for r in ir["rules"]
            ],
            "uncertainty_policy": ir["uncertainty_policy"],
            "intent_facets": ir["intent_facets"],
        },
        period_windows=(),
        total=int(run["counters"]["total"]),
        ir=ir,
    )
    return session, run


def _runner(client: SandboxClient, session: RunSession, tmp_path: Path) -> ScenarioRunner:
    return ScenarioRunner(
        client,
        session,
        history=HISTORY,
        merchants=PACK.merchants,
        audit=AuditLog(tmp_path / "decisions.jsonl"),
    )


def _audit_lines(tmp_path: Path) -> list[dict[str, Any]]:
    import json

    return [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]


def _foreign_monitor_envelope(client: SandboxClient) -> dict[str, Any]:
    """A real SCEN0004 event (a compliant 27-inch monitor) delivered for a run nobody owns."""
    ir = compile_instruction(PACK.scenarios["SCEN0004"]["cardholder_instruction"])
    draft = client.create_mandate(to_mandate_payload(ir))
    mandate = client.confirm_mandate(draft["draft_id"])
    client.start_run("SCEN0004", mandate["mandate_id"])
    while (envelope := client.next_request(wait=1)) is not None:
        if envelope["data"]["authorization"]["source_authorization_id"] == "AU0035":
            return {**envelope, "run_id": "RUN_NOBODY_KNOWS"}
    raise AssertionError("AU0035 was never delivered")


# ------------------------------------------------------- at-least-once delivery


def test_redelivery_replays_the_stored_decision_and_never_re_evaluates(
    client: SandboxClient, tmp_path: Path
) -> None:
    session, _ = _start(client)
    runner = _runner(client, session, tmp_path)

    envelope = client.next_request(wait=5)
    assert envelope is not None
    first = runner.handle(envelope)
    assert first is not None

    # The same envelope again — exactly what at-least-once delivery does.
    assert runner.handle(envelope) is None
    assert session.replays == 1
    assert len(session.decisions) == 1, "a replay must not produce a second decision"

    auth_id = envelope["data"]["authorization"]["authorization_id"]
    records = runner.audit.records_for(auth_id)
    assert [r["type"] for r in records] == ["decision", "replay"]
    assert "idempotent_replay" in records[1]["reason_codes"]
    assert records[1]["decision"] == first["decision"], "the identical decision, replayed"


def test_the_ledger_counts_an_approval_once_however_often_it_is_delivered(
    client: SandboxClient, tmp_path: Path
) -> None:
    session, _ = _start(client, "SCEN0001")
    session.period_windows = (7,)
    runner = _runner(client, session, tmp_path)

    envelope = client.next_request(wait=5)
    assert envelope is not None
    runner.handle(envelope)
    approvals_after_first = len(session.ledger.approvals)
    runner.handle(envelope)
    runner.handle(envelope)

    assert len(session.ledger.approvals) == approvals_after_first


# --------------------------------------------------------------- 409 and 408


def test_a_conflicting_decision_is_recorded_not_fought(
    client: SandboxClient, tmp_path: Path
) -> None:
    """The platform already has an answer. Record the conflict; do not retry."""
    session, _ = _start(client)
    runner = _runner(client, session, tmp_path)
    envelope = client.next_request(wait=5)
    assert envelope is not None
    auth_id = envelope["data"]["authorization"]["authorization_id"]

    # Something else decided it first — a second worker, or a restart mid-flight.
    client.submit_decision(auth_id, {"authorization_id": auth_id, "decision": "decline"})

    summary = runner.handle(envelope)
    assert summary is not None
    assert summary["upstream"]["http_status"] == 409
    # Our record stands and says the platform disagreed — the thing you need afterwards.
    stored = runner.audit.get(auth_id)
    assert stored is not None
    assert stored["upstream"]["error"] == "authorization_finalized"
    # And nothing entered our window on the strength of a decision that was refused.
    assert session.ledger.approvals == []


def test_a_late_decision_is_recorded_as_refused_and_never_becomes_an_approval(
    client: SandboxClient, tmp_path: Path
) -> None:
    session, _ = _start(client)
    runner = _runner(client, session, tmp_path)
    envelope = client.next_request(wait=5)
    assert envelope is not None
    auth_id = envelope["data"]["authorization"]["authorization_id"]

    # Push the platform's own deadline into the past, so the submit arrives late.
    from sandbox.server import STATE

    STATE.authorizations[auth_id].deadline_at = datetime.now(UTC) - timedelta(seconds=1)

    summary = runner.handle(envelope)
    assert summary is not None
    assert summary["upstream"]["http_status"] == 408
    assert summary["upstream"]["error"] == "deadline_exceeded"
    assert session.ledger.approvals == [], "a refused decision must not enter approved spend"
    stored = runner.audit.get(auth_id)
    assert stored is not None and stored["upstream"]["http_status"] == 408


def test_the_deadline_guard_answers_instead_of_missing_the_window(
    client: SandboxClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """specs/deadline-guard.md — forced by shrinking the reserve past the whole budget."""
    session, _ = _start(client)
    runner = _runner(client, session, tmp_path)
    runner.reserve_ms = 1_000_000.0  # nothing can ever be inside this reserve

    envelope = client.next_request(wait=5)
    assert envelope is not None
    summary = runner.handle(envelope)

    assert summary is not None
    assert summary["decision"] == "step_up", "uncertainty_policy is ask ⇒ step_up"
    assert summary["reason_codes"] == ["deadline_risk"]
    assert summary["checks"] == [], "no check ran, and the record must not pretend otherwise"
    stored = runner.audit.get(envelope["data"]["authorization"]["authorization_id"])
    assert stored is not None and stored["timing"]["guard_tripped"] is True


# ------------------------------------------------------------------- resume


def test_a_restart_rebuilds_the_decided_set_and_the_ledger(
    client: SandboxClient, tmp_path: Path
) -> None:
    """The process died mid-run. Nothing may be decided twice, and the window must survive."""
    session, run = _start(client, "SCEN0001")
    session.period_windows = (7,)
    runner = _runner(client, session, tmp_path)
    for _ in range(3):
        envelope = client.next_request(wait=5)
        assert envelope is not None
        runner.handle(envelope)

    decided_before = dict(session.decided)
    window_before = session.ledger.spend_in_window(
        session.seen[-1].timestamp + timedelta(days=1), 7
    )
    assert decided_before and window_before > Money.zero()

    # A brand-new process: same run, no memory of it at all.
    restarted = RunSession(
        run_id=session.run_id,
        scenario_id=session.scenario_id,
        mandate_id=session.mandate_id,
        policy=session.policy,
        period_windows=(7,),
        total=session.total,
    )
    _runner(client, restarted, tmp_path).resume()

    assert set(restarted.decided) == set(decided_before)
    assert (
        restarted.ledger.spend_in_window(session.seen[-1].timestamp + timedelta(days=1), 7)
        == window_before
    )


def test_resume_advances_the_event_cursor(client: SandboxClient, tmp_path: Path) -> None:
    session, _ = _start(client)
    runner = _runner(client, session, tmp_path)
    assert session.cursor == 0
    runner.resume()
    assert session.cursor > 0, "the append-only feed is where a restart picks up"


def test_resume_survives_an_api_that_will_not_answer(client: SandboxClient, tmp_path: Path) -> None:
    """Losing the resume data degrades the window. Crashing loses the whole run."""
    session, _ = _start(client)
    runner = _runner(client, session, tmp_path)

    def boom() -> dict[str, Any]:
        raise RuntimeError("network is down")

    runner.client.authorizations = boom  # type: ignore[method-assign]
    runner.client.events = lambda since=0: boom()  # type: ignore[assignment]
    runner.resume()  # must not raise
    assert session.decided == {}


# ------------------------------------------------------------------ dispatch


def test_an_authorization_for_an_unknown_run_is_answered_but_not_judged_here(
    client: SandboxClient, tmp_path: Path
) -> None:
    """The decision queue is team-global, so a second engine on the same team key can be
    handed another run's work. It must still be answered — a missing decision is a platform
    decline nobody explained — but not against this run's policy, and not into this run's
    window."""
    session, _ = _start(client, "SCEN0001")
    session.period_windows = (7,)
    runner = _runner(client, session, tmp_path)
    envelope = client.next_request(wait=5)
    assert envelope is not None
    auth_id = envelope["data"]["authorization"]["authorization_id"]

    assert runner.handle({**envelope, "run_id": "RUN_SOMEONE_ELSE"}) is None

    assert session.ledger.approvals == []
    assert session.seen == []
    assert session.counters()["delivered"] == 0, "not counted against this run"
    assert auth_id in session.decided, "but remembered, so a redelivery replays it"

    entry = _audit_lines(tmp_path)[-1]
    assert entry["type"] == "unowned"
    assert entry["reason_codes"] == ["insufficient_evidence"]
    assert entry["delivered_for_run_id"] == "RUN_SOMEONE_ELSE"


def test_an_unknown_run_is_never_judged_with_this_run_s_facets(
    client: SandboxClient, tmp_path: Path
) -> None:
    """Regression: intent_facets are per-mandate and are not on the wire.

    Borrowing this run's facets applied the wrong rules with full confidence — AU0035, a
    compliant monitor purchase, declined as `cart_contradicts_purpose` when handed to a run
    enforcing "road-running shoes". It now routes to the event's own uncertainty policy.
    """
    shoes, _ = _start(client, "SCEN0002")  # facets: sporting_goods + road/running
    runner = _runner(client, shoes, tmp_path)
    assert any(f["kind"] == "item_identity" for f in shoes.policy["intent_facets"])

    monitor = _foreign_monitor_envelope(client)
    assert runner.handle(monitor) is None

    entry = _audit_lines(tmp_path)[-1]
    assert entry["type"] == "unowned"
    assert entry["decision"] == "step_up", "uncertainty_policy 'ask' on the event's own mandate"
    assert "cart_contradicts_purpose" not in entry["reason_codes"]
    assert "merchant_type_not_permitted" not in entry["reason_codes"]


def test_a_foreign_authorization_is_routed_to_the_run_that_owns_it(
    client: SandboxClient, tmp_path: Path
) -> None:
    session_a, _ = _start(client, "SCEN0000")
    session_b = RunSession(
        run_id="RUN_B",
        scenario_id="SCEN0001",
        mandate_id="TM_B",
        policy=session_a.policy,
        period_windows=(7,),
        total=10,
    )
    runner_b = _runner(client, session_b, tmp_path)
    runner_a = _runner(client, session_a, tmp_path)
    runner_a.dispatch = lambda run_id: runner_b if run_id == "RUN_B" else None

    envelope = client.next_request(wait=5)
    assert envelope is not None
    runner_a.handle({**envelope, "run_id": "RUN_B"})

    assert len(session_b.decisions) == 1, "the owning run recorded it"
    assert session_a.decisions == [], "and this one did not"


def test_a_stopped_run_decides_nothing_more_even_when_another_loop_is_handed_its_order(
    client: SandboxClient, tmp_path: Path
) -> None:
    """Revoked or stopped, a run's queued order must not be judged under a withdrawn permission.

    The queue is team-global, so the live loop of a *different* run can be handed it — and the
    route to the owning runner would decide it, perhaps as an approval, after the customer
    withdrew the permission it needed. Left alone, the platform declines it at its deadline.
    """
    session_a, _ = _start(client, "SCEN0000")
    session_a.stop_requested.set()  # the wait before the next poll returns at once
    session_b = RunSession(
        run_id="RUN_B",
        scenario_id="SCEN0001",
        mandate_id="TM_B",
        policy=session_a.policy,
        period_windows=(7,),
        total=10,
    )
    session_b.stop_requested.set()  # revoked
    runner_b = _runner(client, session_b, tmp_path)
    runner_a = _runner(client, session_a, tmp_path)
    runner_a.dispatch = lambda run_id: runner_b if run_id == "RUN_B" else None

    envelope = client.next_request(wait=5)
    assert envelope is not None
    assert runner_a.handle({**envelope, "run_id": "RUN_B"}) is None

    assert session_b.decisions == [] and session_b.decided == {}, "nothing decided under it"
    assert session_a.decisions == [], "and nothing borrowed this run's policy either"


def test_upstream_result_reports_a_dead_network_as_data(tmp_path: Path) -> None:
    """A transport failure is an outcome the loop records, not an exception it dies on."""
    dead = SandboxClient(base_url="http://127.0.0.1:9", api_key="x", timeout=0.3)
    result: UpstreamResult = dead.try_submit_decision("AU0001", {"decision": "approve"})
    assert result.ok is False
    assert result.status is None
    assert result.error_code == "unreachable"
    assert result.as_dict()["submitted"] is False
    dead.close()


# ------------------------------------------------------- the real platform's shapes
#
# Everything below was observed against the organizers' sandbox on 2026-09-24 (`make
# probe-live`, then a live SCEN0001 run). The replica reports the same facts under different
# names, and on one point behaves differently: the real platform holds a run at an unanswered
# step-up. These pin the engine to what the real API actually sends.

REAL_RUN_RUNNING = {
    "run_id": "run_caeb7f6051aedac2",
    "status": "running",
    "generated_event_count": 5,
    "finalized_event_count": 4,
    "pending_event_count": 1,
    "queued_event_count": 0,
}
REAL_RUN_COMPLETED = {**REAL_RUN_RUNNING, "status": "completed", "pending_event_count": 0}
REAL_BOOTSTRAP = {
    "limits": {
        "decision_timeout_seconds": 8,
        "step_up_timeout_seconds": 120,
        "long_poll_max_seconds": 25,
    },
    "features": {"reset": False},
}


def _real_row(auth_id: str, decision: str, status: str, ts: str, chf: float) -> dict[str, Any]:
    """A `/v1/authorizations` row as the real API returns it: decision and purchase nested."""
    return {
        "authorization_id": auth_id,
        "source_authorization_id": auth_id.split("-")[0],
        "run_id": "run_caeb7f6051aedac2",
        "status": status,
        "decision": {
            "authorization_id": auth_id,
            "decision": decision,
            "reason_codes": ["within_per_order_limit"],
            "customer_message": "Approved.",
            "decision_source": "team",
        },
        "decision_source": "team",
        "reason_codes": ["within_per_order_limit"],
        "authorization": {"timestamp": ts, "billing_amount_chf": chf},
    }


class _RealApi:
    """The organizers' API, reduced to what these tests touch. Refuses a second decision."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.resolve_result = UpstreamResult(200, {"status": "approved"})

    def authorizations(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def events(self, since: int = 0) -> dict[str, Any]:
        return {"since": since, "next_cursor": since, "events": []}

    def try_submit_decision(self, auth_id: str, payload: dict[str, Any]) -> UpstreamResult:
        raise AssertionError(f"a second automated decision was sent for {auth_id}")

    def try_resolve(self, auth_id: str, decision: str, message: str = "") -> UpstreamResult:
        return self.resolve_result


def _bare_session(total: int = 10) -> RunSession:
    return RunSession(
        run_id="run_caeb7f6051aedac2",
        scenario_id="SCEN0001",
        mandate_id="TMd2d4e092f25fdfd5",
        policy={"hard_rules": [], "uncertainty_policy": "ask", "intent_facets": []},
        period_windows=(7,),
        total=total,
    )


def test_the_real_api_s_progress_limits_and_rows_are_read() -> None:
    from leash.adapters.client import (
        authorization_decision,
        limit_seconds,
        run_complete,
        run_total,
    )

    assert run_complete(REAL_RUN_COMPLETED) is True
    assert run_complete(REAL_RUN_RUNNING) is False, "5 of 10 generated is progress, not the end"
    assert run_total(REAL_RUN_RUNNING) is None, "the real API reports no total"
    assert limit_seconds(REAL_BOOTSTRAP, "step_up_timeout_seconds") == 120
    assert limit_seconds(REAL_BOOTSTRAP, "decision_timeout_seconds") == 8
    row = _real_row("AU0002-51aedac2", "approve", "approved", "2026-08-10T09:00:00Z", 44.5)
    assert authorization_decision(row) == "approve"


def test_the_replica_s_shapes_still_read_the_same(client: SandboxClient) -> None:
    from leash.adapters.client import limit_seconds, run_complete, run_total

    _, run = _start(client)
    assert run_total(run) == 1
    assert run_complete(client.get_run(run["run_id"])) is False
    assert limit_seconds(client.bootstrap(), "step_up_timeout_seconds") == 120
    assert isinstance(client.authorizations(), list)


def test_a_step_up_holding_the_run_is_waited_on_not_resubmitted(tmp_path: Path) -> None:
    """The real platform hands a pending step-up back on every poll. It is not work."""
    session = _bare_session()
    session.stop_requested.set()  # the wait returns at once; the point is what is NOT sent
    runner = _runner(_RealApi(), session, tmp_path)  # type: ignore[arg-type]
    envelope = {
        "run_id": session.run_id,
        "authorization_id": "AU0006-51aedac2",
        "status": "pending_step_up",
        "data": {"authorization": {"authorization_id": "AU0006-51aedac2"}},
    }

    assert runner.handle(envelope) is None
    assert session.delivered == 0 and session.decisions == []
    assert not (tmp_path / "decisions.jsonl").exists(), "nothing to audit: nothing happened"


def test_a_redelivered_step_up_is_never_resubmitted(tmp_path: Path) -> None:
    """Only /resolve settles a step-up; the guide forbids a second automated decision."""
    session = _bare_session()
    session.decided["AU0006-51aedac2"] = {"decision": "step_up", "reason_codes": []}
    runner = _runner(_RealApi(), session, tmp_path)  # type: ignore[arg-type]
    envelope = {
        "run_id": session.run_id,
        "status": "awaiting_decision",
        "data": {"authorization": {"authorization_id": "AU0006-51aedac2"}},
    }

    assert runner.handle(envelope) is None
    assert session.replays == 1


def test_a_restart_rebuilds_from_the_real_api_s_nested_rows(tmp_path: Path) -> None:
    rows = [
        _real_row("AU0002-51aedac2", "approve", "approved", "2026-08-10T09:00:00Z", 44.5),
        _real_row("AU0006-51aedac2", "step_up", "pending_step_up", "2026-08-13T17:26:00Z", 65.0),
    ]
    session = _bare_session()
    _runner(_RealApi(rows), session, tmp_path).resume()  # type: ignore[arg-type]

    assert session.decided["AU0002-51aedac2"]["decision"] == "approve"
    assert session.decided["AU0006-51aedac2"]["decision"] == "step_up"
    assert session.decided["AU0002-51aedac2"]["customer_message"] == "Approved."
    as_of = datetime(2026, 8, 14, tzinfo=UTC)
    assert session.ledger.spend_in_window(as_of, 7) == Money.from_value("44.50")
    assert "AU0006-51aedac2" in session.ledger.pending, "waiting on the customer, not approved"


def test_an_answer_after_the_platform_closed_the_window_is_refused_and_closes_it(
    tmp_path: Path,
) -> None:
    """The phone answered at 0 s; the platform had already declined it as `step_up_expired`."""
    from leash.runtime.session import StepUp
    from leash.runtime.supervisor import Supervisor, SupervisorError

    row = _real_row("AU0006-51aedac2", "decline", "declined", "2026-08-13T17:26:00Z", 65.0)
    row["decision_source"] = "timeout"
    api = _RealApi([row])
    api.resolve_result = UpstreamResult(
        409,
        {"error": {"code": "authorization_not_pending"}},
        error_code="authorization_not_pending",
        error_message="Authorization is not awaiting step-up",
    )
    session = _bare_session()
    session.step_ups["AU0006-51aedac2"] = StepUp(
        authorization_id="AU0006-51aedac2",
        source_authorization_id="AU0006",
        run_id=session.run_id,
        asked_at=datetime.now(UTC) - timedelta(seconds=130),
        expires_at=datetime.now(UTC) - timedelta(seconds=10),
        amount_chf="65.00",
        merchant_name="Alpine Basket",
        customer_message="Needs your confirmation.",
        reason_codes=["split_order_suspected"],
        evidence=[],
    )
    sup = Supervisor(api, pack=PACK, history=HISTORY, audit=AuditLog(tmp_path / "a.jsonl"))  # type: ignore[arg-type]
    sup.sessions[session.run_id] = session
    sup.runners[session.run_id] = _runner(api, session, tmp_path)  # type: ignore[arg-type]

    with pytest.raises(SupervisorError) as refused:
        sup.resolve_step_up("AU0006-51aedac2", "approve")

    assert refused.value.status == 409, "the late answer was not applied, and says so"
    assert session.step_ups["AU0006-51aedac2"].resolved == "expired"
    assert session.counters()["final"] == {"approved": 0, "declined": 1, "waiting": 0}


def test_the_window_reports_the_cap_it_is_measured_against() -> None:
    """A reader needs "CHF 223.50 of 300.00", and a tightening must move the "of"."""
    session = _bare_session()
    session.policy["hard_rules"] = [
        {
            "field": "billing_amount_chf",
            "operator": "<=",
            "value": 300,
            "scope": "period",
            "period_days": 7,
        },
    ]
    assert session.window()["limit_chf"] == "300.00"

    session.policy["hard_rules"].append(
        {
            "field": "billing_amount_chf",
            "operator": "<=",
            "value": 250,
            "scope": "period",
            "period_days": 7,
        }
    )
    assert session.window()["limit_chf"] == "250.00", "add-only: the tightest per window binds"


def test_a_live_decision_carries_the_facts_the_ui_renders(
    client: SandboxClient, tmp_path: Path
) -> None:
    """The replayed demo reads prior orders and hostile spans; a live decision must too."""
    session, _ = _start(client)
    runner = _runner(client, session, tmp_path)
    envelope = client.next_request(wait=5)
    assert envelope is not None
    summary = runner.handle(envelope)

    assert summary is not None
    context = summary["context"]
    assert context["purchase_description"] == "Grocery delivery order"
    assert context["enrichment"]["merchant_prior_approvals"] > 0, "AU0001's shop is familiar"
    audit = _audit_lines(tmp_path)[-1]
    assert "context" not in audit, "display facts are served, never written to the trail"


def test_one_request_held_by_two_loops_is_decided_once(
    client: SandboxClient, tmp_path: Path
) -> None:
    """Measured live 2026-09-24: with two runs active, both loops were handed AU0009–AU0011.

    The route to the owning runner is correct, but the owning runner was entered on two
    threads, both passed the "already decided?" check, and both evaluated, submitted and
    recorded — so the run reported 13 outcomes for 10 orders.
    """
    import threading

    session, _ = _start(client)
    submitting = threading.Event()
    release = threading.Event()
    submits: list[str] = []

    class Held:
        """The real client, except that a submission waits until the test lets it go."""

        def __getattr__(self, name: str) -> Any:
            return getattr(client, name)

        def try_submit_decision(self, auth_id: str, payload: dict[str, Any]) -> UpstreamResult:
            submits.append(auth_id)
            submitting.set()
            release.wait(5)
            return client.try_submit_decision(auth_id, payload)

    runner = _runner(Held(), session, tmp_path)  # type: ignore[arg-type]
    envelope = client.next_request(wait=5)
    assert envelope is not None

    first = threading.Thread(target=runner.handle, args=(envelope,))
    first.start()
    assert submitting.wait(5), "the first delivery reached the platform"
    assert runner.handle(envelope) is None, "the second delivery defers to the first"
    release.set()
    first.join(5)

    assert submits == [envelope["authorization_id"]], "submitted once"
    assert len(session.decisions) == 1, "recorded once"
    assert session.counters()["delivered"] == 2, "while both deliveries are still counted"
    assert session.in_flight == set(), "and the claim is released"
