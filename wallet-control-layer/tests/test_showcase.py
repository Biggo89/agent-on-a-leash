"""The showcase pack: the repo pack plus showcase/, and the scenario it exists for.

SCEN0900 "Three outcomes" is one errand with one purchase per verdict. These tests pin that
it shows exactly that, that adding it moves none of the repo pack's 45 decisions, and that the
builder refuses rows that would not run. See showcase/README.md and
src/leash/adapters/showcase.py.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from leash.adapters.datapack import DataPack, repo_data_dir
from leash.adapters.history import HistoryIndex
from leash.adapters.showcase import APPENDED, SHOWCASE_SOURCE, ShowcasePackError, build
from leash.domain.types import Decision
from tests.board import replay_board

REPO = repo_data_dir()
pytestmark = pytest.mark.skipif(REPO is None, reason="the organizers' data pack is not checked out")


@pytest.fixture(scope="module")
def showcase(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("packs") / "showcase-pack"
    build(out)
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_written_pack_matches_its_own_manifest(showcase: Path) -> None:
    manifest = json.loads((showcase / "metadata.json").read_text())
    for entry in manifest["files"]:
        path = showcase / entry["path"]
        assert _sha(path) == entry["sha256"], entry["path"]
        if "rows" in entry:
            assert len(path.read_text().splitlines()) - 1 == entry["rows"], entry["path"]


def test_every_repo_row_is_kept_byte_for_byte(showcase: Path) -> None:
    assert REPO is not None
    for path in REPO.rglob("*"):
        if path.is_dir() or path.name == "metadata.json":
            continue
        rel = path.relative_to(REPO)
        written = (showcase / rel).read_bytes()
        if str(rel) in APPENDED:
            assert written.startswith(path.read_bytes()), rel
        else:
            assert written == path.read_bytes(), rel


def _load(pack_dir: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[DataPack, HistoryIndex]:
    monkeypatch.setenv("LEASH_DATA_DIR", str(pack_dir))
    return DataPack.load(), HistoryIndex.load(pack_dir / "authorization_history.csv")


def test_three_outcomes_shows_one_purchase_per_verdict(
    showcase: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, history = _load(showcase, monkeypatch)
    board = replay_board(pack=pack, history=history, scenarios=["SCEN0900"])

    seen = {aid: (r.decision, [str(c) for c in r.reason_codes]) for aid, r in board.items()}
    assert seen == {
        "AU0901": (Decision.APPROVE, ["within_per_order_limit", "item_matches_request"]),
        "AU0902": (Decision.STEP_UP, ["unrequested_addon"]),
        "AU0903": (Decision.DECLINE, ["per_order_limit_exceeded"]),
    }


def test_the_extra_scenario_moves_none_of_the_repo_decisions(
    showcase: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack, history = _load(showcase, monkeypatch)
    repo_ids = sorted(s for s in pack.scenarios if s != "SCEN0900")
    with_showcase = replay_board(pack=pack, history=history, scenarios=repo_ids)
    without = replay_board()

    assert with_showcase.keys() == without.keys()
    for aid, record in without.items():
        assert with_showcase[aid].decision is record.decision, aid
        assert with_showcase[aid].reason_codes == record.reason_codes, aid


def test_the_replica_reads_each_scenarios_authority_from_the_pack() -> None:
    from sandbox.server import _authority_for

    # The table this lookup replaced, so the five repo scenarios run exactly as before.
    assert {s: _authority_for(s) for s in sorted(DataPack.load().scenarios)} == {
        "SCEN0000": "AUTH0001",
        "SCEN0001": "AUTH0002",
        "SCEN0002": "AUTH0003",
        "SCEN0003": "AUTH0004",
        "SCEN0004": "AUTH0005",
    }


def _source_copy(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    shutil.copytree(SHOWCASE_SOURCE, source, ignore=shutil.ignore_patterns("*.md"))
    return source


def test_an_id_the_repo_already_uses_is_refused(tmp_path: Path) -> None:
    source = _source_copy(tmp_path)
    attempts = source / "purchase_attempts.csv"
    attempts.write_text(attempts.read_text().replace("AU0901,", "AU0001,", 1))

    with pytest.raises(ShowcasePackError, match="AU0001"):
        build(tmp_path / "out", source=source)
    assert not (tmp_path / "out").exists()


def test_a_cart_line_that_contradicts_the_catalogue_is_refused(tmp_path: Path) -> None:
    source = _source_copy(tmp_path)
    items = source / "purchase_attempt_items.csv"
    items.write_text(items.read_text().replace("IT0062,Fragrance", "IT0001,Fragrance", 1))

    with pytest.raises(ShowcasePackError, match="differs from items.csv"):
        build(tmp_path / "out", source=source)


def test_a_wrong_event_count_is_refused(tmp_path: Path) -> None:
    source = _source_copy(tmp_path)
    catalogue = source / "scenario_catalogue.csv"
    catalogue.write_text(catalogue.read_text().replace(",showcase,3,", ",showcase,4,", 1))

    with pytest.raises(ShowcasePackError, match="event_count"):
        build(tmp_path / "out", source=source)
