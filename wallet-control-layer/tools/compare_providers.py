"""Compile the same instructions with each model provider; compare the Policy IRs and the time.

    make compare-providers                                  the five pack instructions
    uv run python tools/compare_providers.py --invented --runs 3
    uv run python tools/compare_providers.py --providers apertus --instruction "Buy me a bike…"

Every instruction is compiled by the deterministic baseline and then by each provider through
the real `compile_instruction(mode="llm")`, guard, safety floor and fallback included, so the
column shows exactly the IR that provider would hand the review screen. The time is the
wall-clock of that one call, in ms, which is what the customer waits for.

Two references, because the two instruction sets prove different things:

  pack       the baseline IR. The 45-decision board is built on it, and the model's policy on
             these five must enforce the same thing (specs/llm-compiler.md, "Fixtures").
             `missed` / `extra` are facet kinds the model dropped or added against it.
  invented   the expectations in tests/fixtures/invented_instructions.yaml — the same checks
             `make compile-live` asserts, reported here as a count instead of a failure.

This is the quick read. The decisive one for the pack is still `make replay-llm` with
LEASH_COMPILER_PROVIDER set, which fails if any of the 45 decisions moves.

Full IRs and timings go to out/provider-comparison-<utc time>.json (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from leash.adapters.datapack import DataPack  # noqa: E402
from leash.compile import compile_instruction, llm  # noqa: E402
from leash.service.deps import load_dotenv  # noqa: E402

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

# Read by the checks but only to word the customer message, never to decide. Two compilers
# describing the same monitor in different words enforce the same policy.
WORDING_ONLY = {"item_description"}


# --------------------------------------------------------------------------- what an IR enforces


def enforced(ir: dict[str, Any]) -> dict[str, Any]:
    """The part of an IR that moves a decision, in a form two IRs can be compared on."""
    caps = sorted(
        (str(r.get("scope", "purchase")), float(r["value"]), r.get("period_days"))
        for r in ir["rules"]
    )
    facets = {
        f["kind"]: {
            k: sorted(v) if isinstance(v, list) else v
            for k, v in sorted((f.get("require") or {}).items())
            if k not in WORDING_ONLY and v not in (None, [], "")
        }
        for f in ir["intent_facets"]
    }
    return {"caps": caps, "facets": facets, "uncertainty_policy": ir["uncertainty_policy"]}


def vs_baseline(ir: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Where a model's enforced policy departs from the one the 45-decision board runs on."""
    mine, ref = enforced(ir), enforced(baseline)
    out = []
    if mine["caps"] != ref["caps"]:
        out.append(f"caps {_caps(mine['caps'])} vs {_caps(ref['caps'])}")
    for kind in sorted(set(ref["facets"]) - set(mine["facets"])):
        out.append(f"missed {kind}")
    for kind in sorted(set(mine["facets"]) - set(ref["facets"])):
        out.append(f"extra {kind}")
    for kind in sorted(set(mine["facets"]) & set(ref["facets"])):
        if mine["facets"][kind] != ref["facets"][kind]:
            out.append(f"{kind} {mine['facets'][kind]} vs {ref['facets'][kind]}")
    if mine["uncertainty_policy"] != ref["uncertainty_policy"]:
        out.append(f"uncertainty {mine['uncertainty_policy']} vs {ref['uncertainty_policy']}")
    return out


def vs_expectations(ir: dict[str, Any], case: dict[str, Any]) -> list[str]:
    """The invented-instruction expectations, as findings rather than assertions.

    Mirrors tests/unit/test_invented_instructions.py; that file is the authority when the two
    disagree.
    """
    always, with_model = case["always"], case.get("with_model") or {}
    kinds = {f["kind"] for f in ir["intent_facets"]}
    out = []

    unused = sorted((float(r["value"]), i) for i, r in enumerate(ir["rules"]))
    used: set[int] = set()
    for cap in sorted(always["caps"], key=lambda c: float(c["value"])):
        match = next(
            (
                i
                for v, i in unused
                if i not in used
                and v <= float(cap["value"])
                and ("scope" not in cap or ir["rules"][i].get("scope") == cap["scope"])
            ),
            None,
        )
        if match is None:
            out.append(f"cap {cap['value']} not enforced")
        else:
            used.add(match)

    for kind in sorted(kinds - set(always["no_facets_beyond"])):
        out.append(f"invented {kind}")
    for kind in sorted(kinds & set(always.get("never_facets") or ())):
        out.append(f"wrong {kind}")
    if (
        always.get("uncertainty_policy")
        and ir["uncertainty_policy"] != always["uncertainty_policy"]
    ):
        out.append(f"uncertainty {ir['uncertainty_policy']}")

    if not str(ir.get("compiler", "")).startswith("llm:"):
        return out  # what follows is what a model is expected to add
    for kind in sorted(set(with_model.get("facets") or ()) - kinds):
        out.append(f"missed {kind}")
    compiled = {(float(r["value"]), r.get("scope"), r.get("period_days")) for r in ir["rules"]}
    for cap in with_model.get("scoped_caps") or ():
        if (float(cap["value"]), cap["scope"], cap.get("period_days")) not in compiled:
            out.append(f"mis-scoped {cap['value']}")
    asked = " ".join(ir["open_questions"]).lower()
    for phrase in with_model.get("mentions_question") or ():
        if phrase.lower() not in asked:
            out.append(f"never asked about {phrase!r}")
    requirements = len(ir["rules"]) + len(ir["intent_facets"])
    if len(ir["guidance"]) < (requirements + 1) // 2:
        out.append(f"guidance {len(ir['guidance'])}/{requirements}")
    return out


def _caps(caps: list[Any]) -> str:
    return "[" + ", ".join(f"{s}:{v:g}" + (f"/{d}d" if d else "") for s, v, d in caps) + "]"


# --------------------------------------------------------------------------- running


# Swisscom's gateway allows 12 500 output tokens a minute and reserves max_tokens up front, so
# back-to-back compiles hit 429 within a few calls. A 429 says nothing about the model; wait for
# the budget to refill and ask again, so the column measures the model rather than the quota.
RATE_LIMIT_WAIT_S = 15
RATE_LIMIT_RETRIES = 8


def timed_compile(instruction: str, *, provider: str | None) -> tuple[dict[str, Any], float]:
    """One compile, as the review screen would run it. Returns (IR, milliseconds)."""
    if provider is None:
        started = time.perf_counter()
        ir = compile_instruction(instruction, mode="baseline")
        return ir, (time.perf_counter() - started) * 1000
    os.environ["LEASH_COMPILER_PROVIDER"] = provider
    for _ in range(RATE_LIMIT_RETRIES):
        started = time.perf_counter()
        ir = compile_instruction(instruction, mode="llm")
        elapsed = (time.perf_counter() - started) * 1000
        if "model_error: http_429" not in (ir.get("compiler_notes") or ()):
            break
        print(f"  {DIM}{provider}: rate-limited, waiting {RATE_LIMIT_WAIT_S} s{OFF}", flush=True)
        time.sleep(RATE_LIMIT_WAIT_S)
    return ir, elapsed


def cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.instruction:
        return [{"id": "cli", "instruction": args.instruction}]
    if args.invented:
        return list(yaml.safe_load(INVENTED.read_text()))
    pack = DataPack.load()
    return [
        {"id": sid, "instruction": pack.scenarios[sid]["cardholder_instruction"]}
        for sid in sorted(pack.scenarios)
    ]


def findings(ir: dict[str, Any], case: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    return vs_expectations(ir, case) if "always" in case else vs_baseline(ir, baseline)


# --------------------------------------------------------------------------- printing


def row(label: str, ir: dict[str, Any], ms: list[float], problems: list[str]) -> None:
    fell_back = not str(ir.get("compiler", "")).startswith("llm:") and label != "baseline"
    spread = f"({min(ms):.0f}–{max(ms):.0f})" if len(ms) > 1 else ""
    timing = f"{statistics.median(ms):>8.0f} {DIM}{spread:<13}{OFF}"
    kinds = ",".join(f["kind"] for f in ir["intent_facets"]) or "—"
    verdict = (
        f"{RED}fell back{OFF}"
        if fell_back
        else f"{GREEN}ok{OFF}"
        if not problems
        else f"{YELLOW}{len(problems)} diff{OFF}"
    )
    print(
        f"  {label:<11} {timing}  {_caps(enforced(ir)['caps']):<34} "
        f"Q{len(ir['open_questions'])} G{len(ir['guidance'])}  {verdict}  {DIM}{kinds}{OFF}"
    )
    if fell_back:
        print(f"  {'':<11} {RED}{'; '.join(ir.get('compiler_notes') or ())}{OFF}")
    for problem in problems:
        print(f"  {'':<11} {YELLOW}· {problem}{OFF}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--providers", default="openrouter,apertus", help="comma-separated, in order")
    ap.add_argument("--runs", type=int, default=1, help="compiles per provider per instruction")
    ap.add_argument("--invented", action="store_true", help="the six invented instructions")
    ap.add_argument("--instruction", help="one instruction given on the command line")
    ap.add_argument("--out", type=Path, help="where to write the JSON report")
    args = ap.parse_args()

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    unknown = [p for p in providers if p not in llm.PROVIDERS]
    if unknown:
        raise SystemExit(f"unknown provider(s) {unknown}; known: {sorted(llm.PROVIDERS)}")
    original = os.environ.get("LEASH_COMPILER_PROVIDER")

    print(f"{BOLD}Policy compiler: {' vs '.join(providers)}{OFF}  {DIM}runs={args.runs}{OFF}")
    for name in providers:
        os.environ["LEASH_COMPILER_PROVIDER"] = name
        key = "set" if llm.api_key() else f"{RED}MISSING — will fall back{OFF}"
        print(f"  {name:<11} {llm.models()[0]}  {DIM}{llm.provider().key_env}{OFF} {key}")
    print(
        f"\n  {DIM}{'':<11} {'ms':>8} {'(min–max)':<13} {'caps':<34} "
        f"Q=open questions G=guidance lines{OFF}"
    )

    report: list[dict[str, Any]] = []
    for case in cases(args):
        instruction = case["instruction"]
        print(f"\n{DIM}{'─' * 96}{OFF}\n{BOLD}{case['id']}{OFF}  {DIM}{instruction}{OFF}")
        baseline, base_ms = timed_compile(instruction, provider=None)
        row("baseline", baseline, [base_ms], findings(baseline, case, baseline))

        entry: dict[str, Any] = {
            "id": case["id"],
            "instruction": instruction,
            "baseline": {"ms": round(base_ms, 1), "ir": baseline},
            "providers": {},
        }
        for name in providers:
            runs = [timed_compile(instruction, provider=name) for _ in range(args.runs)]
            ms = [t for _, t in runs]
            last = runs[-1][0]
            signatures = {json.dumps(enforced(ir), sort_keys=True) for ir, _ in runs}
            problems = findings(last, case, baseline)
            if len(signatures) > 1:
                problems.append(f"unstable: {len(signatures)} different policies in {len(runs)}")
            row(name, last, ms, problems)
            entry["providers"][name] = {
                "ms": [round(t, 1) for t in ms],
                "compiled": [str(ir.get("compiler")) for ir, _ in runs],
                "fell_back": [not str(ir.get("compiler", "")).startswith("llm:") for ir, _ in runs],
                "distinct_policies": len(signatures),
                "findings": problems,
                "irs": [ir for ir, _ in runs],
            }
        report.append(entry)

    if original is None:
        os.environ.pop("LEASH_COMPILER_PROVIDER", None)
    else:
        os.environ["LEASH_COMPILER_PROVIDER"] = original

    summary(report, providers)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or ROOT / "out" / f"provider-comparison-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"providers": providers, "runs": args.runs, "cases": report}, indent=2)
    )
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"\n{DIM}full IRs and timings: {shown}{OFF}")
    return 0


def summary(report: list[dict[str, Any]], providers: list[str]) -> None:
    print(
        f"\n{DIM}{'═' * 96}{OFF}\n{BOLD}Summary{OFF}  "
        f"{DIM}ms per compile, over compiles that did not fall back. A diff is a departure from "
        f"the reference, not necessarily an error — `make replay-llm` decides that.{OFF}"
    )
    print(
        f"  {'provider':<11} {'compiled':>9} {'median':>8} {'mean':>8} {'min':>7} {'max':>7}"
        f"  {'diffs':>5}  {'unstable':>8}"
    )
    for name in providers:
        results = [c["providers"][name] for c in report]
        attempts = [(t, fb) for r in results for t, fb in zip(r["ms"], r["fell_back"], strict=True)]
        ms = [t for t, fb in attempts if not fb]
        diffs = sum(
            len([f for f in r["findings"] if not f.startswith("unstable")]) for r in results
        )
        unstable = sum(r["distinct_policies"] > 1 for r in results)
        timing = (
            f"{statistics.median(ms):>8.0f} {statistics.mean(ms):>8.0f} "
            f"{min(ms):>7.0f} {max(ms):>7.0f}"
            if ms
            else f"{'—':>8} {'—':>8} {'—':>7} {'—':>7}"
        )
        print(f"  {name:<11} {len(ms):>4}/{len(attempts):<4} {timing}  {diffs:>5}  {unstable:>8}")


if __name__ == "__main__":
    raise SystemExit(main())
