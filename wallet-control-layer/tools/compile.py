"""Compile an instruction and show what the compiler made of it.

    make compile                     the five pack instructions, deterministic compiler
    make compile-llm                 the same five, through the model, side by side
    make compile-invented            the six invented instructions (generalisation)
    uv run python tools/compile.py --instruction "Buy me a bike under CHF 900"

The side-by-side is the demo beat for the policy-authoring screen: the customer's own words on
the left, the executable permissions on the right, each one quoting the phrase it came from,
and every guard action listed underneath. Spec: specs/llm-compiler.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from leash.adapters.datapack import DataPack  # noqa: E402
from leash.compile import compile_instruction, llm  # noqa: E402
from leash.service.deps import load_dotenv  # noqa: E402

# Same as tools/serve.py and tools/probe.py: the key lives in the git-ignored .env, not in a
# shell profile, so a tool that needs it must read the file rather than hope it was exported.
load_dotenv(ROOT / ".env")

BOLD, DIM, GREEN, YELLOW, RED, OFF = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[0m",
)
INVENTED = ROOT / "tests" / "fixtures" / "invented_instructions.yaml"


def render(ir: dict[str, Any], *, indent: str = "  ") -> None:
    print(f"{indent}{DIM}compiler:{OFF} {ir.get('compiler', '?')}")

    for rule in ir["rules"]:
        scope = rule.get("scope", "purchase")
        window = f"/{rule['period_days']}d" if rule.get("period_days") else ""
        floor = f" {YELLOW}[safety floor]{OFF}" if rule.get("safety_floor") else ""
        print(
            f"{indent}{GREEN}cap{OFF}   {rule['operator']} CHF {rule['value']:>8.2f}"
            f"  {scope}{window:<4} {DIM}({rule.get('confidence', '?')}){OFF}{floor}"
            f"\n{indent}      {DIM}from “{rule.get('provenance', '—')}”{OFF}"
        )

    for f in ir["intent_facets"]:
        require = f.get("require") or {}
        detail = ", ".join(f"{k}={v}" for k, v in sorted(require.items())) or "(no parameters)"
        print(
            f"{indent}{GREEN}facet{OFF} {f['kind']:<22} {detail}"
            f"  {DIM}({f.get('confidence', '?')}){OFF}"
            f"\n{indent}      {DIM}from “{f.get('provenance', '—')}”{OFF}"
        )

    if not ir["rules"] and not ir["intent_facets"]:
        print(f"{indent}{RED}nothing enforceable{OFF}")

    for line in ir.get("guidance") or ():
        print(f"{indent}{DIM}says:{OFF} {line}")
    for question in ir.get("open_questions") or ():
        print(f"{indent}{YELLOW}asks:{OFF} {question}")
    for note in ir.get("compiler_notes") or ():
        # The guard's own record: what the model proposed and we would not enforce.
        print(f"{indent}{DIM}guard: {note}{OFF}")


def compile_one(instruction: str, *, compare: bool) -> None:
    print(f"\n{BOLD}{instruction}{OFF}")
    if compare:
        print(f"\n{DIM}── deterministic baseline ──{OFF}")
        render(compile_instruction(instruction, mode="baseline"))
        print(f"\n{DIM}── model ──{OFF}")
        render(compile_instruction(instruction, mode="llm"))
    else:
        print()
        render(compile_instruction(instruction))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instruction", help="compile one instruction given on the command line")
    ap.add_argument("--scenarios", action="store_true", help="the five pack instructions")
    ap.add_argument(
        "--invented",
        action="store_true",
        help="the six invented instructions — generalisation past the pack (TASKS.md Phase 4)",
    )
    ap.add_argument(
        "--compare",
        action="store_true",
        help="run both compilers and print them side by side (needs the provider's key)",
    )
    args = ap.parse_args()

    if args.compare and not llm.api_key():
        print(
            f"{YELLOW}No {llm.provider().key_env} — the model column will show the fallback.{OFF}",
            file=sys.stderr,
        )

    if args.instruction:
        compile_one(args.instruction, compare=args.compare)
        return 0

    if args.invented:
        for case in yaml.safe_load(INVENTED.read_text()):
            print(f"\n{DIM}{'─' * 96}{OFF}")
            print(f"{DIM}proves: {case['proves'].strip()}{OFF}")
            compile_one(case["instruction"].strip(), compare=args.compare)
            print(f"{DIM}baseline gap: {case['baseline_gap'].strip()}{OFF}")
        return 0

    pack = DataPack.load()
    for scenario_id in sorted(pack.scenarios):
        print(f"\n{DIM}{'─' * 96}{OFF}  {BOLD}{scenario_id}{OFF}")
        compile_one(pack.scenarios[scenario_id]["cardholder_instruction"], compare=args.compare)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
