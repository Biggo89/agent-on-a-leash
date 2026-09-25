#!/usr/bin/env python3
"""Weight and threshold analysis for the concern layer.

Replays all 45 fixtures **offline** — no sandbox server, no network — so a sweep costs
milliseconds and can be run mid-conversation while tuning.

    python tools/tune.py                 audit the current config
    python tools/tune.py --sweep         sensitivity of the board to STEP_UP_THRESHOLD
    python tools/tune.py --board         the 45-decision board

There is deliberately NO fitting here. The pack ships no expected decisions
(`metadata.json`: contains_expected_decisions=false), so "tuning" means understanding what the
config does — which knobs are live, whether the board sits on a knife-edge — not steering
fixtures toward a target. Fitting to the 45 would be the hardcoding the challenge prohibits,
done statistically.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leash.adapters.history import HistoryIndex  # noqa: E402
from leash.adapters.parse import parse_event  # noqa: E402
from leash.compile import compile_instruction, to_mandate_payload  # noqa: E402
from leash.domain import evaluator as EV  # noqa: E402
from leash.domain.ledger import Ledger  # noqa: E402
from leash.domain.policy import period_windows  # noqa: E402
from leash.domain.types import Decision, DecisionRecord, Verdict  # noqa: E402
from sandbox.fixtures import DataPack, build_event, data_dir  # noqa: E402

PACK = DataPack.load()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")
_IDENTITY = {
    "mandate_id": "TM_TUNE",
    "status": "active",
    "customer_id": "CU_TUNE",
    "card_id": "CA_TUNE",
    "profile_id": "PROFILE_TUNE",
}


def replay_all() -> dict[str, DecisionRecord]:
    """Every scenario in fixture order, carrying the rolling ledger across each run."""
    decisions: dict[str, DecisionRecord] = {}
    for scenario_id in sorted(PACK.scenarios):
        ir = compile_instruction(
            PACK.scenarios[scenario_id]["cardholder_instruction"], mode="baseline"
        )
        rules = to_mandate_payload(ir)["hard_rules"]
        windows = period_windows(rules)
        policy: dict[str, Any] = {
            "hard_rules": rules,
            "uncertainty_policy": ir["uncertainty_policy"],
            "intent_facets": ir["intent_facets"],
        }
        ledger, seen = Ledger(), []
        for attempt in PACK.scenario_attempts(scenario_id):
            mandate = {
                **_IDENTITY,
                "instruction": ir["source_instruction"],
                "hard_rules": rules,
                "uncertainty_policy": ir["uncertainty_policy"],
            }
            event = build_event(
                PACK,
                attempt,
                run_id="RUN_TUNE",
                mandate=mandate,
                approved_spend_in_period_chf=0.0,
                recent_authorizations=[],
                deadline_seconds=8,
                request_seq=1,
            )
            ev = parse_event(
                event,
                history=HISTORY,
                ledger=ledger,
                merchants=PACK.merchants,
                period_windows=windows,
                seen_in_run=seen,
            )
            record = EV.evaluate(ev, policy)
            if record.decision is Decision.APPROVE:
                ledger.record_approval(ev.authorization_id, ev.timestamp, ev.billing_amount_chf)
            elif record.decision is Decision.STEP_UP:
                ledger.record_step_up(ev.authorization_id, ev.timestamp, ev.billing_amount_chf)
            seen.append(ev)
            decisions[attempt["authorization_id"]] = record
    return decisions


def concerns_of(record: DecisionRecord) -> list[str]:
    return [
        r.reason_code
        for r in record.check_results
        if r.verdict is Verdict.CONCERN and r.reason_code
    ]


def board(decisions: dict[str, DecisionRecord]) -> dict[str, int]:
    return dict(collections.Counter(str(r.decision) for r in decisions.values()))


def audit() -> int:
    decisions = replay_all()
    print(f"board: {board(decisions)}   threshold: {EV.STEP_UP_THRESHOLD}\n")

    fired: collections.Counter[str] = collections.Counter()
    widths: collections.Counter[int] = collections.Counter()
    print("events carrying at least one concern:")
    for auth, record in sorted(decisions.items()):
        codes = concerns_of(record)
        if not codes:
            continue
        fired.update(codes)
        widths[len(codes)] += 1
        score = sum(EV.CONCERN_WEIGHTS.get(c, 0.0) for c in codes)
        escalated = "escalates" if score >= EV.STEP_UP_THRESHOLD else "below threshold"
        print(
            f"  {auth}  {'+'.join(codes):<52} {score:>4.1f}  {escalated:<15} -> {record.decision}"
        )

    print(f"\nconcerns per event: {dict(sorted(widths.items()))}")
    if set(widths) == {1}:
        print(
            "  ⚠  every concern event carries exactly one concern — accumulation never happens here"
        )

    print("\nmargin above the threshold (0.0 = a weight change moves this fixture first):")
    for auth, record in sorted(decisions.items()):
        codes = concerns_of(record)
        if not codes:
            continue
        margin = sum(EV.CONCERN_WEIGHTS[c] for c in codes) - EV.STEP_UP_THRESHOLD
        if margin >= 0:
            bar = "knife-edge" if margin == 0 else f"+{margin:.1f} headroom"
            print(f"  {auth}  {bar}")

    print("\nconfigured weights:")
    emitted = set(fired)
    dead = 0
    for code, weight in sorted(EV.CONCERN_WEIGHTS.items()):
        if code in emitted:
            print(f"  {code:<30} {weight:>4.1f}   fires on {fired[code]} event(s)")
        else:
            dead += 1
            print(f"  {code:<30} {weight:>4.1f}   ⚠  DEAD — no check emits this code")
    if dead:
        print(
            f"\n  ⚠  {dead} weight(s) connect to nothing. "
            "A knob that moves nothing is misleading config."
        )
    return 0


def sweep() -> int:
    """How does the board move as STEP_UP_THRESHOLD changes? A stable design has plateaus."""
    original = EV.STEP_UP_THRESHOLD
    baseline = None
    print("threshold  approve decline step_up   fixtures that move vs. the current setting")
    print("-" * 92)
    try:
        for threshold in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0):
            EV.STEP_UP_THRESHOLD = threshold
            decisions = replay_all()
            counts = board(decisions)
            if threshold == original:
                baseline = {a: str(r.decision) for a, r in decisions.items()}
            row = (
                f"{threshold:>6.1f}   {counts.get('approve', 0):>7} "
                f"{counts.get('decline', 0):>7} {counts.get('step_up', 0):>7}"
            )
            print(row + ("   <- current" if threshold == original else ""))
        # Second pass now that the baseline exists, to name what actually moves.
        print()
        for threshold in (0.5, 1.0, 1.5, 2.5, 3.0, 4.0, 5.0):
            EV.STEP_UP_THRESHOLD = threshold
            decisions = replay_all()
            moved = [
                f"{a}:{baseline[a]}->{d}"
                for a, r in sorted(decisions.items())
                if (d := str(r.decision)) != baseline[a]
            ]
            print(f"  {threshold:>4.1f}: {', '.join(moved) if moved else 'no change'}")
    finally:
        EV.STEP_UP_THRESHOLD = original
    return 0


def weight_sensitivity() -> int:
    """Halve each weight in turn and report what moves. Names which knob owns which fixture."""
    baseline = {a: str(r.decision) for a, r in replay_all().items()}
    print("weight change                                  fixtures that move")
    print("-" * 92)
    for code in sorted(EV.CONCERN_WEIGHTS):
        original = EV.CONCERN_WEIGHTS[code]
        for trial in (1.0, 3.0):
            if trial == original:
                continue
            EV.CONCERN_WEIGHTS[code] = trial
            try:
                moved = [
                    f"{a}:{baseline[a]}->{d}"
                    for a, r in sorted(replay_all().items())
                    if (d := str(r.decision)) != baseline[a]
                ]
            finally:
                EV.CONCERN_WEIGHTS[code] = original
            label = f"{code} {original:.1f} -> {trial:.1f}"
            print(f"  {label:<44} {', '.join(moved) if moved else 'no change'}")
    return 0


def show_board() -> int:
    decisions = replay_all()
    for auth, record in sorted(decisions.items()):
        print(f"  {auth}  {str(record.decision):<9} {','.join(record.reason_codes) or '-'}")
    print(f"\n  {board(decisions)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--sweep", action="store_true", help="threshold sensitivity")
    ap.add_argument("--board", action="store_true", help="the 45-decision board")
    ap.add_argument("--weights", action="store_true", help="per-weight sensitivity")
    args = ap.parse_args()
    if args.sweep:
        return sweep()
    if args.weights:
        return weight_sensitivity()
    if args.board:
        return show_board()
    return audit()


if __name__ == "__main__":
    raise SystemExit(main())
