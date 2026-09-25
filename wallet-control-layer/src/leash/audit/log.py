"""Append-only JSONL audit trail. See specs/audit-record.md.

The file is never rewritten. A decision appends a ``decision`` line; a human resolution and an
at-least-once redelivery append their own lines later. Reading an authorization folds them
together, so the trail can be tailed, shipped or diffed, and a crash truncates at most the
last line.

The writer never raises into the decision path: losing a record is bad, losing the decision
the record was about is worse.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("leash.audit")

DEFAULT_PATH = Path("out/audit/decisions.jsonl")


def audit_path() -> Path:
    return Path(os.environ.get("LEASH_AUDIT_PATH", str(DEFAULT_PATH)))


class AuditLog:
    """One file, one lock, an in-memory index of ``authorization_id -> byte offsets``."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else audit_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._offsets: dict[str, list[int]] = {}
        self._order: list[int] = []  # append order, for the tail endpoints
        self.malformed = 0
        self._reindex()

    # ------------------------------------------------------------------ reading

    def _reindex(self) -> None:
        """Rebuild the index by scanning the file. A bad line is counted and skipped."""
        self._offsets.clear()
        self._order.clear()
        if not self.path.exists():
            return
        with self.path.open("rb") as fh:
            offset = 0
            for raw in fh:
                try:
                    obj = json.loads(raw)
                    auth_id = str(obj["authorization_id"])
                except (ValueError, KeyError, TypeError):
                    self.malformed += 1
                else:
                    self._offsets.setdefault(auth_id, []).append(offset)
                    self._order.append(offset)
                offset += len(raw)

    def _read_at(self, offset: int) -> dict[str, Any] | None:
        with self.path.open("rb") as fh:
            fh.seek(offset)
            raw = fh.readline()
        try:
            obj: dict[str, Any] = json.loads(raw)
        except ValueError:
            return None
        return obj

    def records_for(self, authorization_id: str) -> list[dict[str, Any]]:
        """Every line written for one authorization, in the order it was written."""
        with self._lock:
            offsets = list(self._offsets.get(authorization_id, ()))
        return [r for o in offsets if (r := self._read_at(o)) is not None]

    def get(self, authorization_id: str) -> dict[str, Any] | None:
        """The decision, with any later resolution and replay lines folded in.

        Folding happens on read; the file itself keeps the separate lines, which is what
        makes it an audit trail rather than a mutable table.
        """
        records = self.records_for(authorization_id)
        if not records:
            return None
        decision = next((r for r in records if r.get("type") == "decision"), None)
        if decision is None:
            # A resolution or replay whose decision line predates this file. Report what we
            # have rather than 404-ing on evidence that exists.
            return {
                "authorization_id": authorization_id,
                "decision_record": None,
                "resolutions": [r for r in records if r.get("type") == "resolution"],
                "replays": [r for r in records if r.get("type") == "replay"],
            }
        folded = dict(decision)
        folded["resolutions"] = [r for r in records if r.get("type") == "resolution"]
        folded["replays"] = [r for r in records if r.get("type") == "replay"]
        folded["final_status"] = _final_status(decision, folded["resolutions"])
        return folded

    def tail(self, limit: int = 50, *, type_: str | None = None) -> list[dict[str, Any]]:
        """The most recent lines, newest first."""
        with self._lock:
            offsets = list(reversed(self._order))
        out: list[dict[str, Any]] = []
        for offset in offsets:
            record = self._read_at(offset)
            if record is None or (type_ is not None and record.get("type") != type_):
                continue
            out.append(record)
            if len(out) >= limit:
                break
        return out

    def raw_lines(self, authorization_id: str) -> str:
        """The untouched JSONL for one authorization — the 'show me the trail' moment."""
        return "\n".join(
            json.dumps(r, separators=(",", ":")) for r in self.records_for(authorization_id)
        )

    def __len__(self) -> int:
        return len(self._order)

    # ------------------------------------------------------------------ writing

    def append(self, record: dict[str, Any]) -> bool:
        """Append one line. Returns False on failure — never raises at the caller."""
        try:
            line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
            payload = line.encode("utf-8")
            with self._lock:
                offset = self.path.stat().st_size if self.path.exists() else 0
                # O_APPEND so concurrent writers (run loop + service thread) interleave
                # whole lines rather than corrupting each other.
                with self.path.open("ab") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                auth_id = str(record.get("authorization_id", ""))
                self._offsets.setdefault(auth_id, []).append(offset)
                self._order.append(offset)
            return True
        except Exception as exc:  # an unwritable trail must not take the decision path down
            log.error("audit append failed for %s: %s", record.get("authorization_id"), exc)
            return False


def _final_status(decision: dict[str, Any], resolutions: list[dict[str, Any]]) -> str:
    """What actually happened, after any human answer.

    A step-up is not an outcome — it is a question. Only the resolution closes it.
    """
    if resolutions:
        return "approved" if resolutions[-1].get("outcome") == "approve" else "declined"
    verdict = decision.get("decision")
    if verdict == "step_up":
        return "awaiting_customer"
    return "approved" if verdict == "approve" else "declined"
