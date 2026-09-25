#!/usr/bin/env python3
"""Run scenarios end to end and print the decision table.

Default target is the offline replica, so this works with no network and no team key —
which is also what makes it safe to use as the live demo harness.

    make replay          all five scenarios against the replica
    make demo            the rehearsed demo sequence
    make baseline        save the current table as the regression baseline
    make diff-decisions  diff against that baseline  ← run after every rule change
    python tools/replay.py --scenario SCEN0001 --verbose

The replica holds a run at every step-up, as the real platform does, and nobody is watching a
replay: offline, the harness declines each one itself and says so. That leaves every window
where the board has it — a pending step-up never enters approved spend. Against the real
sandbox (`--live`) it answers nothing unless told to, and waits for the platform to close the
window instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leash.adapters.client import (  # noqa: E402
    PENDING_STEP_UP_STATUSES,
    SandboxClient,
    run_complete,
)
from leash.adapters.history import HistoryIndex  # noqa: E402
from leash.adapters.livepack import use_live_pack  # noqa: E402
from leash.adapters.parse import parse_event  # noqa: E402
from leash.compile import compile_instruction, to_mandate_payload  # noqa: E402
from leash.compile.llm import provider  # noqa: E402
from leash.domain.evaluator import evaluate  # noqa: E402
from leash.domain.ledger import Ledger  # noqa: E402
from leash.domain.money import Money  # noqa: E402
from leash.domain.policy import period_windows  # noqa: E402
from leash.domain.types import Decision, EnrichedEvent  # noqa: E402
from leash.service.deps import load_dotenv  # noqa: E402
from sandbox.fixtures import DataPack, data_dir  # noqa: E402

# The key lives in the git-ignored .env, not a shell profile. Without this, `--llm` would
# quietly compile with the baseline, and the run would "prove" the board does not move by
# never involving a model at all.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASELINE = Path(__file__).resolve().parent.parent / "out" / "baseline.json"
DEMO_SEQUENCE = ["SCEN0000", "SCEN0001", "SCEN0004"]
# Empty 2 s polls before a run the platform never calls complete is given up on.
MAX_IDLE_POLLS = 15

SYMBOL = {
    "approve": "\033[32m✓ approve \033[0m",
    "decline": "\033[31m✗ decline \033[0m",
    "step_up": "\033[33m? step_up \033[0m",
}


def run_scenario(
    client: SandboxClient,
    pack: DataPack,
    history: HistoryIndex,
    scenario_id: str,
    *,
    verbose: bool = False,
    auto_resolve: str | None = None,
    compiler_mode: str = "baseline",
) -> list[dict[str, object]]:
    scenario = pack.scenarios[scenario_id]
    instruction = scenario["cardholder_instruction"]

    # Pinned, not inherited from the environment: the 45-decision board is a regression
    # baseline and must not move because someone exported a key (specs/llm-compiler.md).
    ir = compile_instruction(instruction, mode=compiler_mode)
    draft = client.create_mandate(to_mandate_payload(ir))
    mandate = client.confirm_mandate(draft["draft_id"])
    run = client.start_run(scenario_id, mandate["mandate_id"])

    windows = period_windows(ir["rules"])
    ledger = Ledger()
    seen: list[EnrichedEvent] = []
    decided: dict[str, dict[str, object]] = {}
    rows: list[dict[str, object]] = []

    print(f"\n\033[1m{scenario_id} — {scenario['scenario_name']}\033[0m")
    print(f"  \033[2m{instruction}\033[0m")
    compiler = str(ir.get("compiler", "?"))
    # A silent fallback here is the one failure this command cannot tolerate: its whole
    # purpose is to show that a MODEL-compiled policy produces the same board, and a run
    # that quietly used the regex demonstrates that by never asking a model anything.
    if compiler_mode == "llm" and not compiler.startswith("llm:"):
        notes = "; ".join(str(n) for n in ir.get("compiler_notes") or ()) or "(no reason given)"
        raise SystemExit(
            f"\033[31m--llm asked for a model and got '{compiler}': {notes}\033[0m\n"
            "The board would not move because no model ran, which proves nothing. "
            f"Put {provider().key_env} in .env, then re-run."
        )
    print(
        f"  compiled by \033[1m{compiler}\033[0m: {len(ir['rules'])} hard rule(s), "
        f"{len(ir['intent_facets'])} facet(s), {len(ir['open_questions'])} open question(s)\n"
    )

    idle = 0
    waiting: set[str] = set()
    while True:
        envelope = client.next_request(wait=2)
        if envelope is None:
            # A 204 is not the end of a run (technical_details.md §6): the real platform
            # generates each event only once the one before it is settled. Ask it.
            if run_complete(client.get_run(run["run_id"])):
                break
            idle += 1
            if idle >= MAX_IDLE_POLLS:
                print("  \033[31mthe platform stopped delivering before the run completed\033[0m")
                break
            continue
        idle = 0
        if envelope.get("run_id") not in (None, run["run_id"]):
            # The queue is team-global. Another run's request is not ours to judge with this
            # run's policy; the platform times it out if nobody who owns it answers.
            time.sleep(0.5)
            continue
        event = envelope["data"]
        auth_id = event["authorization"]["authorization_id"]

        # The real platform holds a run at an unanswered step-up and hands it back on every
        # poll. Only /resolve settles it; with no --auto-resolve nobody will, so wait for the
        # platform to close the window — which it records as a decline.
        if envelope.get("status") in PENDING_STEP_UP_STATUSES:
            if auth_id not in waiting:
                waiting.add(auth_id)
                print(
                    f"    {'':<8} {auth_id:<22} \033[2mwaiting for the customer — "
                    "the platform declines it when the step-up window closes\033[0m"
                )
            time.sleep(1)
            continue

        # At-least-once delivery: replay the identical decision, never re-evaluate. Except a
        # step-up, which only /resolve may settle — resubmitting it is refused.
        if auth_id in decided:
            if decided[auth_id]["decision"] == "step_up":
                continue
            client.submit_decision(auth_id, decided[auth_id])
            print(f"    {'':<8} {auth_id:<22} \033[2midempotent replay\033[0m")
            continue

        ev = parse_event(
            event,
            history=history,
            ledger=ledger,
            merchants=pack.merchants,
            period_windows=windows,
            seen_in_run=seen,
        )
        # intent_facets are NOT carried on the live event (the mandate snapshot is
        # restricted by the event schema). The engine holds the Policy IR it compiled and
        # merges it back in at decision time. See specs/policy-ir.md.
        record = evaluate(
            ev,
            {
                "hard_rules": event["mandate"]["hard_rules"],
                "uncertainty_policy": event["mandate"]["uncertainty_policy"],
                "intent_facets": ir["intent_facets"],
            },
        )
        payload = record.to_payload()
        decided[auth_id] = payload
        client.submit_decision(auth_id, payload)

        if record.decision is Decision.APPROVE:
            ledger.record_approval(auth_id, ev.timestamp, ev.billing_amount_chf)
        elif record.decision is Decision.STEP_UP:
            ledger.record_step_up(auth_id, ev.timestamp, ev.billing_amount_chf)
            if auto_resolve:
                # Said plainly on the record: no person answered this.
                client.resolve(
                    auth_id, auto_resolve, "Answered by the replay harness, not a person."
                )
                ledger.resolve(auth_id, auto_resolve == "approve")
        seen.append(ev)

        src = event["authorization"]["source_authorization_id"]
        primary = ev.enrichment.primary_window()
        window = primary[1] if primary else Money.zero()
        print(
            f"    {SYMBOL[str(record.decision)]} {src:<8} CHF {str(ev.billing_amount_chf):>8}  "
            f"{ev.merchant_name:<18} window={str(window):>7}  {record.latency_ms:5.1f}ms  "
            f"{','.join(record.reason_codes) or '-'}"
        )
        if verbose:
            print(f"             \033[2m{record.customer_message}\033[0m")
        if record.decision is Decision.STEP_UP and auto_resolve:
            print(
                f"    {'':<8} {auth_id:<22} \033[2m{auto_resolve}d by the replay harness"
                " — nobody is watching a replay\033[0m"
            )

        rows.append(
            {
                "scenario_id": scenario_id,
                "source_authorization_id": src,
                "decision": str(record.decision),
                "reason_codes": list(record.reason_codes),
                "billing_amount_chf": str(ev.billing_amount_chf),
            }
        )

    progress = client.get_run(run["run_id"]) if hasattr(client, "get_run") else {}
    if progress:
        print(f"  run: {progress}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--scenario", help="one scenario id, e.g. SCEN0001")
    ap.add_argument("--all", action="store_true", help="every scenario in the pack")
    ap.add_argument("--demo", action="store_true", help="the rehearsed demo sequence")
    ap.add_argument(
        "--live", action="store_true", help="target the REAL sandbox (needs TEAM_API_KEY)"
    )
    ap.add_argument("--verbose", action="store_true", help="print the customer message too")
    ap.add_argument(
        "--auto-resolve",
        choices=["approve", "decline"],
        help="answer step-ups (offline default: decline — the replica holds a run at each one)",
    )
    ap.add_argument(
        "--llm",
        action="store_true",
        help="compile the instructions with the model instead of the regex baseline "
        "(needs the LEASH_COMPILER_PROVIDER's key; the decision board must not move — that "
        "is the point)",
    )
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--diff-baseline", action="store_true")
    args = ap.parse_args()

    if args.live and not os.environ.get("TEAM_API_KEY"):
        print("TEAM_API_KEY is not set — refusing to target the live sandbox.", file=sys.stderr)
        return 2
    base_url = os.environ["LEASH_BASE_URL"] if args.live else "http://127.0.0.1:8099"

    if args.live:
        # The live API serves its own pack, not the repo's (adapters/livepack.py): its
        # scenario ids exist nowhere else, and they have to be loaded before anything reads them.
        with SandboxClient(base_url=base_url) as client:
            live = use_live_pack(client)
        print(f"data pack: {live.get('pinned') or live.get('pack_version')} (live)")

    pack = DataPack.load()
    history = HistoryIndex.load(data_dir() / "authorization_history.csv")

    from leash.domain.evaluator import CHECKS

    if len(CHECKS) < 12:
        print(f"\033[33m⚠  {len(CHECKS)} of 12 checks registered — see TASKS.md\033[0m")

    if args.demo:
        scenarios = DEMO_SEQUENCE
        # The demo's fifth objective is explainability, so the rehearsed sequence always
        # prints the customer message under each decision. specs/customer-message.md.
        args.verbose = True
    elif args.scenario:
        scenarios = [args.scenario]
    else:
        scenarios = sorted(pack.scenarios)

    rows: list[dict[str, object]] = []
    with SandboxClient(base_url=base_url) as client:
        try:
            client.health()
        except Exception as exc:
            # Name the actual failure. "Cannot reach" alone sent someone hunting for a dead
            # replica when the real cause was a malformed auth header.
            print(
                f"Cannot reach {base_url}: {type(exc).__name__}: {exc}\n"
                "If the replica is not running, start it with: make sandbox",
                file=sys.stderr,
            )
            return 2
        if not args.live:
            client.reset()
        for scenario_id in scenarios:
            rows.extend(
                run_scenario(
                    client,
                    pack,
                    history,
                    scenario_id,
                    verbose=args.verbose,
                    # Offline the replica holds each run at a step-up, and nobody is watching.
                    # Live, answer nothing unless asked: the platform's timeout is the truth.
                    auto_resolve=args.auto_resolve or (None if args.live else "decline"),
                    compiler_mode="llm" if args.llm else "baseline",
                )
            )

    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r["decision"])] = counts.get(str(r["decision"]), 0) + 1
    print(
        f"\n\033[1m{len(rows)} decisions\033[0m  "
        + "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )

    if args.save_baseline:
        BASELINE.parent.mkdir(exist_ok=True)
        BASELINE.write_text(json.dumps(rows, indent=2))
        print(f"baseline saved → {BASELINE}")
    if args.diff_baseline:
        if not BASELINE.exists():
            print("no baseline yet — run: make baseline", file=sys.stderr)
            return 2
        old = {
            (r["scenario_id"], r["source_authorization_id"]): r
            for r in json.loads(BASELINE.read_text())
        }
        comparable = [r for r in rows if (r["scenario_id"], r["source_authorization_id"]) in old]
        if not comparable:
            # The live pack's scenarios share no id with the repo's, so a diff there compares
            # nothing — which must not print as "no decisions changed".
            print("no decision here has a baseline to compare against", file=sys.stderr)
            return 2
        changed = [
            (k, old[k]["decision"], r["decision"])
            for r in comparable
            if old[k := (r["scenario_id"], r["source_authorization_id"])]["decision"]
            != r["decision"]
        ]
        if changed:
            print(f"\n\033[31m{len(changed)} decision(s) changed:\033[0m")
            for (scen, auth), before, after in changed:
                print(f"  {scen} {auth}: {before} → {after}")
            return 1
        print("\n\033[32mno decisions changed\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
