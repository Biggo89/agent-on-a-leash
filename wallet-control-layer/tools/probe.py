#!/usr/bin/env python3
"""Contract probe against the REAL sandbox. Run this first on the day. `make probe-live`

The offline replica is a *model* of the organizers' API, built from the written contract.
Every place the documentation left something open is marked REPLICA-ASSUMPTION in
``sandbox/server.py``; this script checks those specific assumptions against the real thing
and prints what would need changing, rather than discovering it mid-demo.

It is read-mostly: it creates one mandate and runs the connection-check scenario, which exists
for exactly this — found by name among the scenarios the platform lists, because the live API
numbers its own (SCEN0101 for team17 since 2026-09-24; SCEN0000 in the repo pack). Against the
live API it first loads the pack that API serves (adapters/livepack.py). It decides the first
order only, then lets the platform time out the rest, and does not report until the run reads
`completed` — a half-finished live run holds the team's queue.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from leash.adapters.client import (  # noqa: E402
    SandboxClient,
    authorization_decision,
    limit_seconds,
    run_total,
)
from leash.adapters.datapack import DataPack  # noqa: E402
from leash.adapters.livepack import is_replica, use_live_pack  # noqa: E402
from leash.compile import compile_instruction, to_mandate_payload  # noqa: E402
from leash.domain import deadline as guard  # noqa: E402
from leash.runtime.runner import _ledger_inputs  # noqa: E402
from leash.service.deps import load_dotenv  # noqa: E402

OK, BAD, WARN, DIM, BOLD, OFF = (
    "\033[32m✓\033[0m",
    "\033[31m✗\033[0m",
    "\033[33m!\033[0m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)
findings: list[str] = []


def check(passed: bool, label: str, detail: str = "", *, warn_only: bool = False) -> bool:
    mark = OK if passed else (WARN if warn_only else BAD)
    print(f"  {mark} {label}{f'  {DIM}{detail}{OFF}' if detail else ''}")
    if not passed:
        findings.append(label + (f" — {detail}" if detail else ""))
    return passed


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--scenario", help="scenario to probe with (default: the one named 'Connection check')"
    )
    ap.add_argument("--base-url", help="override LEASH_BASE_URL (e.g. the replica, to self-test)")
    ap.add_argument("--reset", action="store_true", help="POST /v1/team/reset first")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    base_url = args.base_url or os.environ.get("LEASH_BASE_URL", "")
    key = os.environ.get("TEAM_API_KEY", "")
    if not base_url:
        print("LEASH_BASE_URL is not set — copy .env.example to .env", file=sys.stderr)
        return 2

    print(f"\n{BOLD}Contract probe → {base_url}{OFF}\n")

    with SandboxClient(base_url=base_url, api_key=key or "dev-key") as client:
        # 1 — reachability and authentication
        print(f"{BOLD}1. connection{OFF}")
        try:
            health = client.health()
            check(True, "GET /healthz", json.dumps(health)[:90])
        except Exception as exc:
            check(False, "GET /healthz", str(exc))
            return 1
        try:
            boot = client.bootstrap()
        except Exception as exc:
            check(False, "GET /v1/bootstrap (is TEAM_API_KEY right?)", str(exc))
            return 1
        limits = boot.get("limits", {})
        check(True, "GET /v1/bootstrap", json.dumps(limits))

        if not is_replica(base_url):
            live = use_live_pack(client)
            check(
                "pinned" in live or bool(live.get("scenario_ids")),
                "the data pack the API serves, written to out/live-pack",
                str(live.get("pinned") or live.get("pack_version")),
            )
        pack = DataPack.load()
        scenario_id = args.scenario or next(
            (
                str(s["scenario_id"])
                for s in boot.get("scenarios", [])
                if s.get("scenario_name") == "Connection check"
            ),
            "SCEN0000",
        )
        if not check(scenario_id in pack.scenarios, f"{scenario_id} is in the pack", scenario_id):
            return 1
        scenario = pack.scenarios[scenario_id]

        # 2 — the numbers the replica hard-codes
        print(f"\n{BOLD}2. limits the replica assumes{OFF}")
        # The real API and the replica name these differently; `limit_seconds` reads both,
        # so a `None` here means the platform stopped reporting the limit, not a rename.
        deadline_s = limit_seconds(boot, "decision_timeout_seconds")
        check(deadline_s == 8, "decision deadline is 8 s", f"got {deadline_s}", warn_only=True)
        window_s = limit_seconds(boot, "step_up_timeout_seconds")
        check(window_s == 120, "step-up window is 120 s", f"got {window_s}", warn_only=True)
        poll_s = limit_seconds(boot, "long_poll_max_seconds")
        check(poll_s == 25, "long poll is 25 s", f"got {poll_s}", warn_only=True)
        features = boot.get("features", {})
        check(
            "reset" in features or "reset_enabled" in features,
            "features report whether reset is enabled",
            json.dumps(features),
            warn_only=True,
        )

        if args.reset:
            try:
                client.reset()
                print(f"  {OK} POST /v1/team/reset")
            except Exception as exc:
                print(f"  {WARN} reset refused ({exc}) — disabled during judging, as documented")

        # 3 — mandate lifecycle
        print(f"\n{BOLD}3. mandate lifecycle{OFF}")
        instruction = scenario["cardholder_instruction"]
        ir = compile_instruction(instruction, mode="baseline")
        payload = to_mandate_payload(ir)
        check(
            payload["instruction"] == instruction,
            "the compiler round-trips the instruction byte-identically",
        )
        draft = client.create_mandate(payload)
        check("draft_id" in draft, "POST /v1/mandates returns draft_id", str(draft.get("draft_id")))
        active = client.confirm_mandate(draft["draft_id"])
        mandate_id = active.get("mandate_id", "")
        check(bool(mandate_id), "POST /confirm returns mandate_id", mandate_id)
        resource = client.get_mandate(mandate_id)
        check(
            "guidance" in resource and "open_questions" in resource,
            "guidance/open_questions readable from the mandate resource",
        )

        # 4 — one run, one decision, end to end
        print(f"\n{BOLD}4. decision path ({scenario_id}){OFF}")
        run = client.start_run(scenario_id, mandate_id)
        run_id = run.get("run_id", "")
        check(bool(run_id), "POST /v1/scenario-runs returns run_id", run_id)
        # The real API reports no total at all (the engine falls back to the catalogue);
        # only a total that DISAGREES with the catalogue is a finding.
        total = run_total(run)
        check(
            total is None or total == int(scenario["event_count"]),
            "run total, where reported, matches the catalogue event_count",
            f"{total} vs {scenario['event_count']}",
            warn_only=True,
        )

        envelope = client.next_request(wait=25)
        if not check(envelope is not None, "GET /v1/decision-requests/next delivered an event"):
            return 1
        assert envelope is not None
        event = envelope["data"]
        auth = event["authorization"]
        check(
            envelope.get("type") == "authorization.request",
            "envelope type is authorization.request",
            str(envelope.get("type")),
        )
        _schema_check(pack, event)

        deadline_at = guard.parse_deadline(event)
        budget = guard.budget_ms(deadline_at, datetime.now(UTC))
        check(deadline_at is not None, "data.deadline_at present and parseable", str(deadline_at))
        check(
            budget is not None and budget > guard.RESERVE_MS,
            f"budget on arrival exceeds the {guard.RESERVE_MS:.0f} ms reserve",
            f"{budget:.0f} ms left" if budget is not None else "no deadline",
            warn_only=True,
        )
        check(
            auth.get("spend_in_period_before_chf") is None,
            "authorization.spend_in_period_before_chf is null (we track the period)",
            str(auth.get("spend_in_period_before_chf")),
            warn_only=True,
        )
        context = event.get("context", {})
        check(
            "approved_spend_in_period_chf" in context,
            "context.approved_spend_in_period_chf present",
            str(context.get("approved_spend_in_period_chf")),
        )
        mandate_snapshot = event.get("mandate", {})
        check(
            "intent_facets" not in mandate_snapshot,
            "the event's mandate snapshot carries no intent_facets (as documented)",
        )

        auth_id = auth["authorization_id"]
        body = {
            "authorization_id": auth_id,
            "decision": "approve",
            "reason_codes": ["probe_connection_check"],
            "customer_message": "Connection probe.",
            "evidence": [],
            "engine_version": "leash-probe",
        }
        result = client.try_submit_decision(auth_id, body)
        check(result.ok, "POST decision accepted", f"HTTP {result.status} {result.body}")

        # 5 — idempotency, the thing at-least-once delivery depends on
        print(f"\n{BOLD}5. idempotency and error shapes{OFF}")
        repeat = client.try_submit_decision(auth_id, body)
        check(
            repeat.ok,
            "resubmitting the identical decision is accepted",
            f"HTTP {repeat.status} {repeat.error_code or ''}",
        )
        conflict = client.try_submit_decision(auth_id, {**body, "decision": "decline"})
        check(
            conflict.status == 409,
            "a *different* decision for the same authorization is 409",
            f"HTTP {conflict.status} {conflict.error_code}",
        )
        check(
            isinstance(conflict.body.get("error"), dict),
            "errors use the documented {error:{code,message}} envelope",
            json.dumps(conflict.body)[:80],
        )

        # 6 — resume inputs
        print(f"\n{BOLD}6. what a restart can rebuild from{OFF}")
        listed = client.authorizations()
        mine = [a for a in listed if a.get("authorization_id") == auth_id]
        check(bool(mine), "GET /v1/authorizations lists the decided authorization")
        if mine:
            row = mine[0]
            check(
                authorization_decision(row) == "approve",
                "  …carries the decision",
                str(authorization_decision(row)),
                warn_only=True,
            )
            check(row.get("status") is not None, "  …carries status", str(row.get("status")))
            timestamp, amount = _ledger_inputs(row)
            check(
                timestamp is not None and amount is not None,
                "  …carries what the ledger is rebuilt from (timestamp, billing_amount_chf)",
                f"{timestamp} · {amount}",
                warn_only=True,
            )
        feed = client.events(since=0)
        check(
            "next_cursor" in feed,
            "GET /v1/events returns next_cursor",
            str(feed.get("next_cursor")),
        )

        # 7 — leave nothing behind
        print(f"\n{BOLD}7. the run finishes{OFF}")
        status = _drain(client, run_id, int(scenario["event_count"]) - 1, deadline_s or 8)
        check(status == "completed", "the run reads completed", status)

    print(f"\n{BOLD}result{OFF}")
    if findings:
        print(f"  {BAD} {len(findings)} assumption(s) the replica gets wrong:\n")
        for f in findings:
            print(f"      · {f}")
        print(
            f"\n  {DIM}Fix sandbox/server.py and re-run the vectors before trusting a replay.{OFF}"
        )
        return 1
    print(f"  {OK} the real API matches every assumption this probe checks.")
    # Known and not probed — it takes a run as far as its first step-up: the real platform
    # holds the run, and the whole team queue, while a step-up waits on the customer. The
    # replica does not. specs/service-contract.md §5.
    print(
        f"  {DIM}Not checked here: a pending step-up holds the whole team queue on the real "
        f"platform — one live run at a time (service-contract.md §5).{OFF}\n"
    )
    return 0


def _drain(client: SandboxClient, run_id: str, remaining: int, deadline_s: int) -> str:
    """Let the platform settle every order the probe does not decide, then read the run.

    The probe judges one order; a scenario with more leaves the rest in the team-global queue,
    and a live run left there blocks every other run. Polling without answering is how the
    platform times each one out (`deadline_s` apiece) — the probe never invents a decision.
    """
    limit = time.monotonic() + remaining * (deadline_s + 5) + 30
    status = str(client.get_run(run_id).get("status"))
    while status != "completed" and time.monotonic() < limit:
        client.next_request(wait=deadline_s + 2)
        status = str(client.get_run(run_id).get("status"))
    return status


def _schema_check(pack: DataPack, event: dict[str, Any]) -> None:
    try:
        import jsonschema

        jsonschema.validate(event, pack.event_schema())
        check(True, "the delivered event validates against authorization_event.schema.json")
    except Exception as exc:
        check(False, "event is schema-valid", str(exc)[:160])


if __name__ == "__main__":
    raise SystemExit(main())
