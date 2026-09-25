#!/usr/bin/env python3
"""What the control layer adds over a plain spending limit — measured, not asserted.

    "Track relevant state over time so rolling limits, retries, duplicate requests, and prior
     decisions are handled correctly **without blocking ordinary purchases unnecessarily**."
                                                                  — the challenge brief

The brief asks for two things at once, and any control layer can have either alone: approve
nothing and stop every problem, or approve everything and interrupt nobody. The question a
card issuer actually has is *what does the understanding layer buy over a spending limit* —
and that is answerable from the organizers' own data with no assumption of ours.

**Every figure here comes from the data pack and this engine.** There is no interchange rate,
no dispute probability, no cost of a blocked purchase: the pack ships none of those, and a
model built on numbers we invented would be a worse argument than no model at all. What this
prints is decisions and Swiss francs, both of which are facts.

Four regimes over the same 45 attempts, the same instructions and the same delivery order:

    no control      every attempt approved — the agent with a blank cheque
    per-order cap   only the per-order limit the customer wrote
    all limits      every spending limit they wrote, per-order and rolling
    full mandate    this engine as it ships

The three engine regimes differ **only** in which checks run. They share the real
`combine()`, so the combination semantics under test are the shipped ones and not a
simplified copy.

    uv run python tools/impact.py            # the table
    uv run python tools/impact.py --detail   # plus every attempt the regimes disagree on
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leash.adapters.datapack import DataPack, data_dir  # noqa: E402
from leash.adapters.history import HistoryIndex  # noqa: E402
from leash.adapters.parse import parse_event  # noqa: E402
from leash.compile import compile_instruction  # noqa: E402
from leash.domain.checks.limits import (  # noqa: E402
    check_per_order_limit,
    check_period_limit,
    check_split_order,
)
from leash.domain.evaluator import CHECKS, combine  # noqa: E402
from leash.domain.ledger import Ledger  # noqa: E402
from leash.domain.money import Money  # noqa: E402
from leash.domain.policy import period_windows  # noqa: E402
from leash.domain.types import CheckResult, Decision, EnrichedEvent  # noqa: E402
from sandbox.fixtures import build_event  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, OFF = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[0m",
)

#: Each regime is a subset of the shipped `CHECKS`, so "what a spending limit alone would do"
#: is this engine with the other checks removed rather than a second implementation that could
#: quietly disagree about boundaries, currency or the rolling window.
REGIMES: dict[str, tuple[str, tuple[Any, ...] | None]] = {
    "no control": ("every attempt approved", ()),
    "per-order cap": ("only the per-order limit the customer wrote", (check_per_order_limit,)),
    "all limits": (
        "every spending limit they wrote, per-order and rolling",
        (check_per_order_limit, check_period_limit, check_split_order),
    ),
    "full mandate": ("this engine as it ships", None),
}


def _decide(ev: EnrichedEvent, policy: dict[str, Any], checks: tuple[Any, ...] | None) -> Decision:
    """One decision under one regime, through the shipped combination rule."""
    if checks is None:
        checks = CHECKS
    if not checks:
        return Decision.APPROVE
    results: list[CheckResult] = []
    for check in checks:
        produced = check(ev, policy)
        results.extend(produced if isinstance(produced, Sequence) else (produced,))
    decision, _ = combine(results, policy.get("uncertainty_policy", "ask"))
    return decision


def run(
    pack: DataPack, history: HistoryIndex, checks: tuple[Any, ...] | None
) -> dict[str, Decision]:
    """Replay all five scenarios under one regime. Each regime keeps its own ledger."""
    out: dict[str, Decision] = {}
    for scenario_id in sorted(pack.scenarios):
        ir = compile_instruction(pack.scenarios[scenario_id]["cardholder_instruction"])
        windows = period_windows(ir["rules"])
        mandate = {
            "mandate_id": "TM_IMPACT",
            "status": "active",
            "customer_id": "CU0000",
            "card_id": "CA0000",
            "profile_id": "PROFILE_IMPACT",
            "instruction": ir["source_instruction"],
            "hard_rules": ir["rules"],
            "uncertainty_policy": ir["uncertainty_policy"],
        }
        policy = {
            "hard_rules": ir["rules"],
            "uncertainty_policy": ir["uncertainty_policy"],
            "intent_facets": ir["intent_facets"],
        }
        ledger, seen = Ledger(), []
        attempts = sorted(
            (a for a in pack.attempts if a["scenario_id"] == scenario_id),
            key=lambda a: int(a["replay_order"]),
        )
        for seq, attempt in enumerate(attempts, start=1):
            event = build_event(
                pack,
                attempt,
                run_id="RUN_IMPACT",
                mandate=mandate,
                approved_spend_in_period_chf=None,
                recent_authorizations=[],
                deadline_seconds=8,
                request_seq=seq,
            )
            ev = parse_event(
                event,
                history=history,
                ledger=ledger,
                merchants=pack.merchants,
                period_windows=windows,
                seen_in_run=seen,
            )
            decision = _decide(ev, policy, checks)
            if decision is Decision.APPROVE:
                ledger.record_approval(ev.authorization_id, ev.timestamp, ev.billing_amount_chf)
            elif decision is Decision.STEP_UP:
                ledger.record_step_up(ev.authorization_id, ev.timestamp, ev.billing_amount_chf)
            seen.append(ev)
            out[attempt["authorization_id"]] = decision
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detail", action="store_true", help="list every disagreement")
    args = parser.parse_args()

    pack = DataPack.load()
    history = HistoryIndex.load(data_dir() / "authorization_history.csv")
    amounts = {
        a["authorization_id"]: Money.from_value(a["billing_amount_chf"]) for a in pack.attempts
    }

    boards = {name: run(pack, history, checks) for name, (_, checks) in REGIMES.items()}
    full = boards["full mandate"]
    # The set this engine does not approve. Stated as *our* reading, because the pack ships no
    # expected decisions (`metadata.json`: contains_expected_decisions=false) and calling them
    # "the risky ones" would smuggle in an answer key nobody published.
    held = {auth_id for auth_id, d in full.items() if d is not Decision.APPROVE}
    ordinary = {auth_id for auth_id, d in full.items() if d is Decision.APPROVE}

    print(f"\n{BOLD}What the control layer adds over a spending limit{OFF}")
    print(
        f"{DIM}45 attempts · 5 scenarios · every figure from the data pack and this engine{OFF}\n"
    )
    print(
        f"{BOLD}{'regime':<16}{'approve':>8}{'step_up':>9}{'decline':>9}"
        f"{'CHF approved':>15}{'of ours held':>14}{OFF}"
    )
    for name in REGIMES:
        board = boards[name]
        counts = {d: sum(1 for x in board.values() if x is d) for d in Decision}
        approved_chf = Money.zero()
        for auth_id, decision in board.items():
            if decision is Decision.APPROVE:
                approved_chf = approved_chf + amounts[auth_id]
        # How many of the attempts this engine holds back did that regime wave through?
        leaked = sum(1 for auth_id in held if board[auth_id] is Decision.APPROVE)
        mark = GREEN if name == "full mandate" else (RED if leaked else YELLOW)
        print(
            f"{name:<16}{counts[Decision.APPROVE]:>8}{counts[Decision.STEP_UP]:>9}"
            f"{counts[Decision.DECLINE]:>9}{str(approved_chf):>15}"
            f"{mark}{leaked:>10} of {len(held)}{OFF}"
        )

    # The ordinary-purchase side of the brief: friction that buys nothing.
    print(f"\n{BOLD}Ordinary purchases{OFF} {DIM}— the ones this engine approves{OFF}")
    over_blocked: dict[str, list[str]] = {}
    for name in REGIMES:
        board = boards[name]
        blocked = sorted(a for a in ordinary if board[a] is not Decision.APPROVE)
        over_blocked[name] = blocked
        note = f"  {RED}blocks {', '.join(blocked)}{OFF}" if blocked else ""
        print(
            f"  {name:<16}{len(ordinary) - len(blocked):>3} of {len(ordinary)} "
            f"approved without friction{note}"
        )

    # Over-blocking by a limit is not a rounding error, it is a mechanism worth naming: a cap
    # that cannot see what is in the basket spends the budget on the wrong thing first.
    for auth_id in over_blocked["all limits"]:
        culprits = [
            other
            for other in sorted(held)
            if boards["all limits"][other] is Decision.APPROVE
            and other < auth_id
            and full[other] is not Decision.APPROVE
        ]
        if culprits:
            print(
                f"\n  {DIM}why {auth_id} (CHF {amounts[auth_id]}) is refused by limits "
                f"alone:{OFF}\n"
                f"  it follows {', '.join(culprits)}, which that regime approved and this engine\n"
                f"  did not. A limit cannot see what is in a basket, so it spends the rolling\n"
                f"  budget on the purchase the customer never asked for — and then refuses the\n"
                f"  one they did."
            )

    leak_chf = Money.zero()
    for auth_id in held:
        if boards["all limits"][auth_id] is Decision.APPROVE:
            leak_chf = leak_chf + amounts[auth_id]
    leaked_count = sum(1 for a in held if boards["all limits"][a] is Decision.APPROVE)
    print(
        f"\n{BOLD}The headline.{OFF} Every spending limit the customer wrote, enforced "
        f"perfectly,\nstill approves {BOLD}{leaked_count}"
        f" of the {len(held)}{OFF} attempts this engine does not — "
        f"{BOLD}CHF {leak_chf}{OFF} of spend.\n"
        f"{DIM}Those are the purchases a limit cannot see, because none of them is about the "
        f"amount.{OFF}"
    )

    if args.detail:
        print(f"\n{BOLD}Where the regimes disagree{OFF}")
        print(f"{DIM}{'auth':<8}{'CHF':>9}  {'per-order':<12}{'all limits':<12}{'full':<10}{OFF}")
        for auth_id in sorted(full):
            row = [
                boards[name][auth_id] for name in ("per-order cap", "all limits", "full mandate")
            ]
            if len(set(row)) == 1:
                continue
            print(
                f"{auth_id:<8}{str(amounts[auth_id]):>9}  "
                + "".join(f"{str(d):<12}" for d in row[:2])
                + f"{str(row[2]):<10}"
            )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
