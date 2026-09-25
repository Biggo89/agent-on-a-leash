"""The live pack: what the API serves, written to disk as the repo pack's layout.

The replica serves the repo pack in the shape the real API used on 2026-09-24, so fetching
from it must give the repo pack back row for row. That round trip is the proof the conversion
loses nothing; the live API's own pack goes through exactly the same code.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from leash.adapters import livepack
from leash.adapters.client import SandboxClient, run_card
from leash.adapters.datapack import DataPack, data_dir, repo_data_dir
from leash.adapters.history import HistoryIndex
from leash.audit.log import AuditLog
from leash.domain import provenance as prov
from leash.runtime.supervisor import Supervisor
from sandbox.server import app as sandbox_app

PACK = DataPack.load()
REPO = repo_data_dir()
HISTORY = HistoryIndex.load(data_dir() / "authorization_history.csv")
KEYS = {
    "customers": "customer_id",
    "accounts": "account_id",
    "cards": "card_id",
    "merchants": "merchant_id",
    "items": "item_id",
    "fx_rates": "from_currency",
    "scenario_catalogue": "scenario_id",
}


@pytest.fixture()
def replica() -> Iterator[SandboxClient]:
    client = SandboxClient(
        base_url="http://sandbox.test",
        api_key="test-key",
        http=TestClient(
            sandbox_app,
            base_url="http://sandbox.test",
            headers={"Authorization": "Bearer test-key"},
        ),
    )
    client.reset()
    yield client
    client.reset()
    client.close()


def _from_account(rule: dict[str, Any]) -> bool:
    return any(p.get("source") == "account" for p in prov.normalise(rule.get("provenance")))


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_fetching_the_repo_pack_gives_the_repo_pack_back(
    replica: SandboxClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert REPO is not None
    out = tmp_path / "live-pack"
    manifest = livepack.fetch(replica, out)

    for name, key in KEYS.items():
        written, repo = _rows(out / f"{name}.csv"), _rows(REPO / f"{name}.csv")
        assert {r[key]: r for r in written} == {r[key]: r for r in repo}, name
    assert (out / "authorization_history.csv").read_bytes() == (
        REPO / "authorization_history.csv"
    ).read_bytes()
    # Delivered by runs, not served: present so the loader finds them, and empty.
    for name, header in livepack.RUNTIME_HEADERS.items():
        with (out / name).open(encoding="utf-8") as fh:
            assert fh.read().strip() == ",".join(header)
    assert (out / "schemas" / "authorization_event.schema.json").is_file()

    # The engine loads it like any pack, and sees the same world.
    monkeypatch.setenv("LEASH_DATA_DIR", str(out))
    loaded = DataPack.load()
    for field in ("merchants", "scenarios", "customers", "cards", "accounts", "fx"):
        assert getattr(loaded, field) == getattr(PACK, field), field
    assert loaded.attempts == []

    # The manifest verifies the way `make verify-data` checks it.
    assert manifest["scenario_ids"] == sorted(PACK.scenarios)
    on_disk = json.loads((out / "metadata.json").read_text())
    for entry in on_disk["files"]:
        digest = hashlib.sha256((out / entry["path"]).read_bytes()).hexdigest()
        assert digest == entry["sha256"], entry["path"]
        if "rows" in entry:
            with (out / entry["path"]).open(encoding="utf-8") as fh:
                assert sum(1 for _ in fh) - 1 == entry["rows"], entry["path"]


def test_a_truncated_history_writes_nothing_and_keeps_the_last_pack(
    replica: SandboxClient, tmp_path: Path
) -> None:
    out = tmp_path / "live-pack"
    livepack.fetch(replica, out)
    before = (out / "metadata.json").read_text()

    reference = replica.reference_data()
    truncated = b"\n".join(replica.reference_history_csv().splitlines()[:100])
    with pytest.raises(livepack.LivePackError, match="truncated"):
        livepack.write(reference, truncated, out, source="test")
    assert (out / "metadata.json").read_text() == before


def test_a_pack_without_scenarios_is_refused(tmp_path: Path) -> None:
    tables: dict[str, Any] = {name: [] for name in livepack.TABLES}
    with pytest.raises(livepack.LivePackError, match="no scenarios"):
        livepack.write({"tables": tables}, b"h\n", tmp_path / "p", source="test")
    with pytest.raises(livepack.LivePackError, match="missing tables: items"):
        livepack.write(
            {"tables": {k: v for k, v in tables.items() if k != "items"}},
            b"h\n",
            tmp_path / "p",
            source="test",
        )


def test_a_pin_to_another_directory_wins_but_the_repo_default_does_not(
    replica: SandboxClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert REPO is not None
    out = tmp_path / "live-pack"

    monkeypatch.setenv("LEASH_DATA_DIR", str(tmp_path / "chosen"))
    assert livepack.use_live_pack(replica, out) == {"pinned": str(tmp_path / "chosen")}
    assert not out.exists()

    # `.env.example` ships the repo pack as LEASH_DATA_DIR: that is the default, not a choice.
    monkeypatch.setenv("LEASH_DATA_DIR", str(REPO))
    manifest = livepack.use_live_pack(replica, out)
    assert manifest["scenario_ids"] and "stale" not in manifest
    assert Path(data_dir()) == out


def test_a_failed_refresh_keeps_the_pack_on_disk_and_says_so(
    replica: SandboxClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "live-pack"
    livepack.fetch(replica, out)
    # setenv, not delenv: monkeypatch only restores what it recorded, and use_live_pack sets
    # the variable itself — a delenv of an unset variable would let that leak into later tests.
    monkeypatch.setenv("LEASH_DATA_DIR", str(REPO))

    class Down:
        base_url = "http://down.test"

        def reference_data(self) -> dict[str, Any]:
            raise ConnectionError("reference endpoint down")

    manifest = livepack.use_live_pack(Down(), out)
    assert "reference endpoint down" in manifest["stale"]
    assert Path(data_dir()) == out

    # Its own output is not a pin: a second call tries to refresh it, and falls back again.
    assert "stale" in livepack.use_live_pack(Down(), out)

    monkeypatch.setenv("LEASH_DATA_DIR", str(REPO))
    with pytest.raises(ConnectionError):
        livepack.use_live_pack(Down(), tmp_path / "never-fetched")


def test_run_card_reads_both_the_live_and_the_replica_shape() -> None:
    live = {"fixture_profiles": [{"profile_id": "PROFILE_AUTH0101", "card_id": "CA1331"}]}
    assert run_card(live) == "CA1331"
    assert run_card({"profile": {"card_id": "CA0001"}}) == "CA0001"
    assert run_card({"fixture_profiles": [], "profile": None}) is None
    assert run_card({}) is None


def test_a_run_with_no_attempts_in_the_pack_still_gets_its_account_layer(
    replica: SandboxClient, tmp_path: Path
) -> None:
    """The live shape: scenarios exist, attempts do not. The card comes from the run."""
    live_like = dataclasses.replace(PACK, attempts=[])
    supervisor = Supervisor(
        replica, pack=live_like, history=HISTORY, audit=AuditLog(tmp_path / "d.jsonl")
    )
    scenario_id = sorted(PACK.scenarios)[1]
    session = supervisor.start_run(scenario_id, auto_resolve="decline")
    try:
        assert session.card_id
        account = PACK.accounts[PACK.cards[session.card_id]["account_id"]]
        ceilings = [r for r in session.policy["hard_rules"] if _from_account(r)]
        assert [r["value"] for r in ceilings] == [float(account["per_transaction_limit_chf"])]

        # A mid-run recompose rebuilds the same layer rather than dropping it.
        supervisor._refresh_sessions(session.mandate_id)
        assert any(_from_account(r) for r in session.policy["hard_rules"])
    finally:
        session.stop_requested.set()
