"""The append-only trail — specs/audit-record.md.

The properties that matter are: nothing is ever rewritten, a bad line cannot take the
decision path down, and a restart can still read what was written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from leash.audit.log import AuditLog
from leash.audit.record import decision_record, replay_record, resolution_record
from leash.domain.evaluator import evaluate
from tests.builders import make_event

NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
POLICY = {"uncertainty_policy": "ask", "hard_rules": [], "intent_facets": []}


def _log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "decisions.jsonl")


def test_appends_and_reads_back(tmp_path: Path) -> None:
    audit = _log(tmp_path)
    ev = make_event(authorization_id="AU0001-X")
    record = evaluate(ev, POLICY, now=NOW)

    assert audit.append(decision_record(record, ev, POLICY, run_id="RUN_1"))
    stored = audit.get("AU0001-X")

    assert stored is not None
    assert stored["decision"] == str(record.decision)
    assert stored["run_id"] == "RUN_1"
    # Every check, passes included: the audit value is the size of the evidence set.
    assert len(stored["checks"]) == len(record.check_results)


def test_a_resolution_is_a_new_line_not_an_edit(tmp_path: Path) -> None:
    audit = _log(tmp_path)
    ev = make_event(authorization_id="AU0016-X", order_returnable="unknown")
    record = evaluate(ev, POLICY, now=NOW)
    audit.append(decision_record(record, ev, POLICY))
    audit.append(resolution_record("AU0016-X", "approve", resolved_at=NOW))

    lines = audit.path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "decision"
    assert json.loads(lines[1])["type"] == "resolution"

    folded = audit.get("AU0016-X")
    assert folded is not None
    assert len(folded["resolutions"]) == 1
    # A step-up is a question, not an outcome. Only the resolution closes it.
    assert folded["final_status"] == "approved"


def test_replay_is_recorded_separately(tmp_path: Path) -> None:
    audit = _log(tmp_path)
    ev = make_event(authorization_id="AU0001-X")
    record = evaluate(ev, POLICY, now=NOW)
    audit.append(decision_record(record, ev, POLICY))
    audit.append(
        replay_record(
            "AU0001-X", "approve", ["within_per_order_limit", "idempotent_replay"], observed_at=NOW
        )
    )
    folded = audit.get("AU0001-X")
    assert folded is not None
    assert len(folded["replays"]) == 1
    assert "idempotent_replay" in folded["replays"][0]["reason_codes"]


def test_index_survives_a_restart(tmp_path: Path) -> None:
    ev = make_event(authorization_id="AU0002-X")
    record = evaluate(ev, POLICY, now=NOW)
    first = _log(tmp_path)
    first.append(decision_record(record, ev, POLICY))

    reopened = AuditLog(tmp_path / "decisions.jsonl")  # fresh index, same file
    assert len(reopened) == 1
    assert reopened.get("AU0002-X") is not None


def test_a_malformed_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    path.write_text(
        '{"authorization_id":"AU0003-X","type":"decision","decision":"approve"}\n'
        "this is not json\n"
        '{"authorization_id":"AU0004-X","type":"decision","decision":"decline"}\n'
    )
    audit = AuditLog(path)

    assert audit.malformed == 1
    assert len(audit) == 2
    assert audit.get("AU0003-X") is not None
    assert audit.get("AU0004-X") is not None


def test_a_write_failure_does_not_raise(tmp_path: Path) -> None:
    """Losing a record is bad. Losing the decision the record was about is worse."""
    audit = _log(tmp_path)
    audit.path = tmp_path / "no-such-directory" / "decisions.jsonl"
    assert audit.append({"authorization_id": "AU0005-X", "type": "decision"}) is False


def test_tail_is_newest_first_and_filterable(tmp_path: Path) -> None:
    audit = _log(tmp_path)
    for i in range(3):
        audit.append({"authorization_id": f"AU{i}", "type": "decision", "decision": "approve"})
    audit.append({"authorization_id": "AU0", "type": "resolution", "outcome": "approve"})

    assert [r["authorization_id"] for r in audit.tail(2)] == ["AU0", "AU2"]
    assert [r["type"] for r in audit.tail(10, type_="resolution")] == ["resolution"]
