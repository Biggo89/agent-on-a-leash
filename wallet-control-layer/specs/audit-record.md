# Spec: the audit record

**Status:** agreed   **Owner:** Nakya   **Objective:** O5 — explain every decision

*Success (TASKS.md O5): a judge reads one audit record and can say what we permitted, what
evidence we used, and why.* That sentence is the acceptance test for this file.

## Storage — `DESIGN`

One **append-only JSONL** file: `$LEASH_AUDIT_PATH`, default `out/audit/decisions.jsonl`.
One JSON object per line, UTF-8, `\n`-terminated, written with `O_APPEND` and flushed per
record.

Append-only is the point, so the file is **never rewritten**:

- A decision writes a `decision` record.
- A human resolution of a step-up writes a **separate** `resolution` record later.
- An at-least-once redelivery writes a **separate** `replay` record.

Reading `GET /v1/audit/{authorization_id}` folds the decision record together with any later
records for the same authorization. Nothing is mutated in place, so the file can be tailed,
shipped, or diffed, and a crash truncates at most the last line.

An in-memory index (`authorization_id → byte offset`) is rebuilt by scanning the file at
startup. A malformed line is skipped and counted, never fatal: an unreadable audit trail must
not take the decision path down with it.

## Record — `decision`

```json
{
  "schema": "leash.audit.v1",
  "type": "decision",
  "authorization_id": "AU0001-1A2B3C",
  "source_authorization_id": "AU0001",
  "run_id": "RUN_...", "scenario_id": "SCEN0000",
  "decided_at": "2026-09-24T09:00:00.123456Z",
  "decision": "approve",
  "reason_codes": ["within_per_order_limit", "merchant_familiar"],
  "customer_message": "Approved: CHF 20.00 at ...",
  "evidence": [{"field": "billing_amount_chf", "value": "20.00"}],
  "checks": [
    {"check_id": "per_order_limit", "verdict": "pass",
     "reason_code": "within_per_order_limit", "detail": "…",
     "evidence": [{"field": "…", "value": "…"}], "concern_weight": null}
  ],
  "score": {"concern_score": 0.0, "step_up_threshold": 2.0},
  "policy": {"uncertainty_policy": "ask", "hard_rules": [], "intent_facets": []},
  "enrichment": {"merchant_prior_approvals": 6, "device_prior_approvals": 12, "…": "…"},
  "timing": {"latency_ms": 1.9, "deadline_at": "…", "budget_ms": 7810.0, "guard_tripped": false},
  "inputs_digest": "sha256 of the exact event we saw",
  "engine_version": "leash-0.1.0",
  "upstream": {"submitted": true, "http_status": 200, "platform_status": "approved",
               "idempotent": false, "error": null}
}
```

Field notes:

- **`checks` carries every check, including the passes.** `decision-rules.md` §5: *"All checks
  always run… a judge asking 'what else did you look at?' needs an answer."* Recording only
  what fired would hide the size of the evidence set.
- **`concern_weight`** is `null` unless the verdict is `concern`; it is read from
  `evaluator.CONCERN_WEIGHTS` at write time so the record shows the arithmetic behind
  `concern_score`, not just its total.
- **`inputs_digest`** proves what we saw without copying the payload into the log. The raw
  event is deliberately *not* stored: it contains untrusted merchant text, and an audit
  trail is not the place to re-serve it.
- **`upstream`** separates *our* decision from *the platform's* recording of it. A 408 or a
  409 means the record stands and the platform's state differs — the most important thing to
  be able to prove after the fact.
- `customer_message` is copied verbatim, so the record shows exactly what the cardholder was
  told.

## Record — `resolution`

```json
{"schema": "leash.audit.v1", "type": "resolution",
 "authorization_id": "…", "resolved_at": "…", "outcome": "approve",
 "customer_message": "…", "resolved_by": "customer",
 "upstream": {"submitted": true, "http_status": 200, "error": null}}
```

`outcome` ∈ `{approve, decline}` (`technical_details.md` §5). Written after
`POST /v1/authorizations/{id}/resolve` returns, so the record reflects what the platform
accepted, not what we intended.

## Record — `replay`

```json
{"schema": "leash.audit.v1", "type": "replay", "authorization_id": "…",
 "observed_at": "…", "decision": "approve", "reason_codes": ["…", "idempotent_replay"],
 "upstream": {"submitted": true, "http_status": 200, "idempotent": true, "error": null}}
```

Written when at-least-once delivery hands us an authorization we have already decided
(`decision-rules.md` §7): the stored decision is resubmitted unchanged with
`idempotent_replay` appended, and **never re-evaluated** — state may have moved since.

## Failure mode

The audit writer never raises into the decision path. A write failure is logged with the
`authorization_id` and the decision still goes to the platform: losing the record is bad,
losing the decision is worse.

## Open questions

- [ ] Retention/rotation is out of scope for a 36-hour event; the file is expected to hold
      low hundreds of records.
