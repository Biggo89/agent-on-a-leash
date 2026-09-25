"""Instruction → Policy IR. The only place in this codebase where a model may run.

Two compilers behind one door:

    baseline.py   generic regex, no network, no key. The floor, and the mandatory fallback.
    llm.py        a model via OpenRouter or Swisscom's Apertus (LEASH_COMPILER_PROVIDER),
                  guarded by contract.py. Better wording, better questions, and it generalises
                  past the five instructions we happen to have read.

`compile_instruction()` picks between them. Which one ran is always visible on the IR, in
`compiler` and `compiler_notes` — a compiled mandate never hides its own provenance.

Spec: specs/llm-compiler.md · specs/policy-ir.md
"""

from __future__ import annotations

import os
from typing import Any

from . import baseline, llm
from .baseline import to_mandate_payload
from .contract import instruction_hash

__all__ = ["compile_instruction", "to_mandate_payload", "instruction_hash", "compiler_mode"]

MODES = ("auto", "baseline", "llm")


def compiler_mode(explicit: str | None = None) -> str:
    """`auto` (model when one is reachable), `baseline` (never call), or `llm` (must try).

    Anything that has to be deterministic — the 45-decision replay, the tune sweeps, the whole
    test suite — pins `baseline` rather than trusting the environment, so no decision the team
    reviews can ever depend on whether a key happened to be exported.
    """
    mode = (explicit or os.environ.get("LEASH_COMPILER") or "auto").strip().lower()
    return mode if mode in MODES else "auto"


def _model_available() -> bool:
    """Is there anything to call? Cheap and offline — the real check is the call itself."""
    return bool(llm.api_key())


def compile_instruction(
    instruction: str,
    *,
    mode: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Compile one cardholder instruction into a reviewable Policy IR.

    Never raises and never returns an unusable IR: with no key, no package and no network this
    still produces the baseline's mandate. That is the point — the challenge asks what happens
    when optional models fail, and the answer must be "policy authoring degrades, enforcement
    does not" (GUIDELINES.md §"Why the LLM sits only at compile time").
    """
    resolved = compiler_mode(mode)
    if resolved == "baseline":
        ir = baseline.compile_instruction(instruction)
        return {**ir, "instruction_sha256": instruction_hash(instruction), "compiler_notes": []}
    if resolved == "auto" and not _model_available():
        return llm.fallback(
            baseline.compile_instruction(instruction),
            llm.unavailable_note(),
        )
    return llm.compile_instruction(instruction, model=model)
