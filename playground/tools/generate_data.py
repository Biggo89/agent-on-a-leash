#!/usr/bin/env python3
"""Generate the playground dataset by running the real Wallet Control Layer engine offline.

Mirrors `wallet-control-layer/tools/replay.py` but drives events straight from the data pack
instead of over HTTP, and dumps every check result, evidence line and customer message.

    cd playground && uv run --project ../wallet-control-layer python tools/generate_data.py

Writes `assets/data.js`. The 45 decisions it produces are verified identical to the project's
own regression baseline (`wallet-control-layer/out/baseline.json`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent / "wallet-control-layer"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from leash.adapters.datapack import DataPack, data_dir
from leash.adapters.history import HistoryIndex
from leash.adapters.parse import parse_event
from leash.compile import compile_instruction, to_mandate_payload
from leash.domain.evaluator import CHECKS, CONCERN_WEIGHTS, STEP_UP_THRESHOLD, evaluate
from leash.domain.score import score as trust_score
from leash.domain.ledger import Ledger
from leash.domain.policy import period_windows
from leash.domain.types import Decision, EnrichedEvent
from sandbox.fixtures import build_event

pack = DataPack.load()
history = HistoryIndex.load(data_dir() / "authorization_history.csv")

out: dict = {
    "config": {
        "concern_weights": CONCERN_WEIGHTS,
        "step_up_threshold": STEP_UP_THRESHOLD,
        "checks": [c.__name__ for c in CHECKS],
    },
    "scenarios": [],
}

for scenario_id in sorted(pack.scenarios):
    scenario = pack.scenarios[scenario_id]
    instruction = scenario["cardholder_instruction"]
    ir = compile_instruction(instruction, mode="baseline")
    windows = period_windows(ir["rules"])
    period_days = windows[0] if windows else None

    mandate = {
        "mandate_id": f"TM_{scenario_id}",
        "status": "active",
        "customer_id": "CU0001",
        "card_id": "CA0001",
        "instruction": instruction,
        "hard_rules": ir["rules"],
        "uncertainty_policy": ir["uncertainty_policy"],
        "profile_id": "PR0001",
    }

    ledger = Ledger()
    seen: list[EnrichedEvent] = []
    events = []

    for seq, attempt in enumerate(pack.scenario_attempts(scenario_id), start=1):
        mandate["card_id"] = attempt["card_id"]
        event = build_event(
            pack,
            attempt,
            run_id="RUN_PLAYGROUND",
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
        record = evaluate(
            ev,
            {
                "hard_rules": event["mandate"]["hard_rules"],
                "uncertainty_policy": event["mandate"]["uncertainty_policy"],
                "intent_facets": ir["intent_facets"],
            },
        )
        # The shortest window is the one a single-window mandate has, which is every mandate
        # in the pack. specs/decision-rules.md §9.
        primary = ev.enrichment.primary_window()
        window_before = str(primary[1]) if primary else "0.00"
        if record.decision is Decision.APPROVE:
            ledger.record_approval(ev.authorization_id, ev.timestamp, ev.billing_amount_chf)
        elif record.decision is Decision.STEP_UP:
            ledger.record_step_up(ev.authorization_id, ev.timestamp, ev.billing_amount_chf)
        seen.append(ev)

        concern_score = sum(
            CONCERN_WEIGHTS.get(r.reason_code or "", 0.0)
            for r in record.check_results
            if str(r.verdict) == "concern"
        )
        events.append({
            "id": attempt["authorization_id"],
            "scenario_id": scenario_id,
            "decision": str(record.decision),
            "reason_codes": list(record.reason_codes),
            "customer_message": record.customer_message,
            "amount": f"{float(attempt['amount']):.2f}",
            "currency": attempt["currency"],
            "billing_amount_chf": str(ev.billing_amount_chf),
            "merchant_id": ev.merchant_id,
            "merchant_name": ev.merchant_name,
            "merchant_category": ev.merchant_category,
            "merchant_country": ev.merchant_country,
            "timestamp": attempt["timestamp"],
            "device_id": ev.customer_device_id,
            "purchase_description": attempt["purchase_description"],
            "order_returnable": ev.order_returnable,
            "recent_attempt_count_10m": ev.recent_attempt_count_10m,
            "related_authorization_id": attempt["related_authorization_id"] or None,
            "related_authorization_status": attempt["related_authorization_status"] or None,
            "window_before_chf": window_before,
            "concern_score": concern_score,
            # The same evidence as one number, with every deduction pointing at a
            # check above. specs/trust-score.md.
            "trust": trust_score(record),
            "latency_ms": round(record.latency_ms, 3),
            "enrichment": {
                "merchant_prior_approvals": ev.enrichment.merchant_prior_approvals,
                "merchant_prior_approvals_customer": ev.enrichment.merchant_prior_approvals_customer,
                "device_prior_approvals": ev.enrichment.device_prior_approvals,
                "merchant_lookalike_of": ev.enrichment.merchant_lookalike_of,
                "merchant_lookalike_name": ev.enrichment.merchant_lookalike_name,
                "is_duplicate_of": ev.enrichment.is_duplicate_of,
                "is_requote_of": ev.enrichment.is_requote_of,
                "same_merchant_recent": [
                    {"id": auth_id, "chf": str(amount)}
                    for auth_id, amount in ev.enrichment.same_merchant_recent
                ],
                "night_hours": ev.enrichment.night_hours,
                "manipulations": [
                    {"label": m.label, "field": m.source_field, "excerpt": m.excerpt}
                    for m in ev.enrichment.manipulations
                ],
            },
            "items": [
                {
                    "name": i.get("item_name"),
                    "category": i.get("item_category"),
                    "quantity": i.get("quantity"),
                    "unit_price": i.get("unit_price"),
                    "currency": i.get("currency"),
                    "details": i.get("item_details"),
                }
                for i in ev.items
            ],
            "checks": [
                {
                    "check_id": r.check_id,
                    "verdict": str(r.verdict),
                    "reason_code": r.reason_code,
                    "detail": r.detail,
                    "follow_up": r.follow_up,
                    "weight": CONCERN_WEIGHTS.get(r.reason_code or "")
                    if str(r.verdict) == "concern" else None,
                    "evidence": [{"field": e.field, "value": e.value} for e in r.evidence],
                }
                for r in record.check_results
            ],
        })

    out["scenarios"].append({
        "id": scenario_id,
        "name": scenario["scenario_name"],
        "instruction": instruction,
        "control_question": scenario["control_question"],
        "theme": scenario["control_theme"],
        "rationale": scenario["short_rationale"],
        "ir": {
            "rules": ir["rules"],
            "intent_facets": ir["intent_facets"],
            "guidance": ir.get("guidance", []),
            "open_questions": ir.get("open_questions", []),
            "uncertainty_policy": ir["uncertainty_policy"],
            "compiler": ir.get("compiler"),
        },
        "period_days": period_days,
        "events": events,
    })

dest = HERE.parent / "assets" / "data.js"
dest.write_text(
    "/* Generated by tools/generate_data.py — real engine output, do not hand-edit. */\n"
    "window.WCL_DATA = " + json.dumps(out, separators=(",", ":")) + ";\n"
)
tot = sum(len(s["events"]) for s in out["scenarios"])
from collections import Counter

c = Counter(e["decision"] for s in out["scenarios"] for e in s["events"])
print(f"wrote {dest} — {tot} events, {dict(c)}, {dest.stat().st_size:,} bytes")
