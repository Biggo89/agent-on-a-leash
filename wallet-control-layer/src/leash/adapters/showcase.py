"""The showcase pack: the organizers' repo pack plus the scenarios in ``showcase/``.

The repo pack is read-only (CLAUDE.md), so a scenario of our own cannot go into it. It goes
into ``showcase/`` instead, as rows in the repo's own CSV layout, and this module writes a
copy of the repo pack with those rows appended to ``out/showcase-pack``. Pointing
``LEASH_DATA_DIR`` there serves the extra scenarios through the same path as the pack's own:
the replica runs them, the engine compiles and decides them, and the demo UI lists them in
Live engine mode. Nothing reads a scenario id, so nothing else changes.

Every repo row is kept byte for byte, and only four files gain rows:

    scenario_catalogue.csv · scenario_authorities.csv ·
    purchase_attempts.csv · purchase_attempt_items.csv

Customers, cards, merchants, items and the history are the repo's, so an extra scenario has to
be written for a cardholder, a shop and products the pack already has. That is checked here,
along with every other reference, before anything is written.

`metadata.json` is rewritten with the new hashes and row counts, so `make verify-data`
checks this directory too.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from pathlib import Path
from typing import Any

from .datapack import _PROJECT_ROOT, repo_data_dir

SHOWCASE_SOURCE = _PROJECT_ROOT / "showcase"
SHOWCASE_PACK_DIR = _PROJECT_ROOT / "out" / "showcase-pack"

#: The only files an extra scenario adds rows to, with each one's key columns.
APPENDED: dict[str, tuple[str, ...]] = {
    "scenario_catalogue.csv": ("scenario_id",),
    "scenario_authorities.csv": ("authority_id",),
    "purchase_attempts.csv": ("authorization_id",),
    "purchase_attempt_items.csv": ("authorization_id", "line_no"),
}


class ShowcasePackError(RuntimeError):
    """The showcase rows are inconsistent with themselves or with the repo pack."""


def _read(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def _table(root: Path, name: str) -> list[dict[str, str]]:
    return _read((root / name).read_text(encoding="utf-8"))[1]


def _check(base: Path, merged: dict[str, list[dict[str, str]]], extra_ids: set[str]) -> None:
    """Every reference an extra row makes resolves, and every scenario is runnable."""
    problems: list[str] = []
    customers = {r["customer_id"] for r in _table(base, "customers.csv")}
    cards = {r["card_id"] for r in _table(base, "cards.csv")}
    merchants = {r["merchant_id"] for r in _table(base, "merchants.csv")}
    items = {r["item_id"]: r for r in _table(base, "items.csv")}
    scenarios = {r["scenario_id"]: r for r in merged["scenario_catalogue.csv"]}
    authorities = {r["authority_id"]: r for r in merged["scenario_authorities.csv"]}
    attempts = {r["authorization_id"]: r for r in merged["purchase_attempts.csv"]}

    for a in authorities.values():
        if a["customer_id"] not in customers:
            problems.append(f"{a['authority_id']}: unknown customer {a['customer_id']}")
        if a["card_id"] not in cards:
            problems.append(f"{a['authority_id']}: unknown card {a['card_id']}")

    for a in attempts.values():
        aid = a["authorization_id"]
        if a["scenario_id"] not in scenarios:
            problems.append(f"{aid}: unknown scenario {a['scenario_id']}")
        authority = authorities.get(a["authority_id"])
        if authority is None:
            problems.append(f"{aid}: unknown authority {a['authority_id']}")
        elif authority["card_id"] != a["card_id"]:
            problems.append(f"{aid}: card {a['card_id']} is not {a['authority_id']}'s card")
        if a["merchant_id"] not in merchants:
            problems.append(f"{aid}: unknown merchant {a['merchant_id']}")

    for line in merged["purchase_attempt_items.csv"]:
        where = f"{line['authorization_id']} line {line['line_no']}"
        if line["authorization_id"] not in attempts:
            problems.append(f"{where}: no such purchase attempt")
        item = items.get(line["item_id"])
        if item is None:
            problems.append(f"{where}: unknown item {line['item_id']}")
        elif (line["item_name"], line["item_category"]) != (
            item["item_name"],
            item["item_category"],
        ):
            # The data dictionary guarantees a cart line matches its catalogue row, and the
            # item checks and the compile guard rely on it (adapters/datapack.py).
            problems.append(f"{where}: name or category differs from items.csv {item['item_id']}")

    for sid in sorted(extra_ids):
        own = sorted(
            (a for a in attempts.values() if a["scenario_id"] == sid),
            key=lambda a: int(a["replay_order"]),
        )
        if [int(a["replay_order"]) for a in own] != list(range(1, len(own) + 1)):
            problems.append(f"{sid}: replay_order must run 1..n without gaps")
        if str(len(own)) != scenarios[sid]["event_count"]:
            problems.append(
                f"{sid}: event_count is {scenarios[sid]['event_count']} "
                f"but it has {len(own)} purchase attempts"
            )
        if len({a["authority_id"] for a in own}) != 1:
            problems.append(f"{sid}: every purchase attempt must name the same authority")

    if problems:
        raise ShowcasePackError("showcase rows rejected:\n  " + "\n  ".join(problems))


def build(
    out: Path = SHOWCASE_PACK_DIR,
    *,
    source: Path = SHOWCASE_SOURCE,
    base: Path | None = None,
) -> dict[str, Any]:
    """Write the repo pack plus `source`'s rows to `out`, whole or not at all.

    Returns what was added. Built in a sibling directory and swapped in at the end, so a
    rejected row leaves the previous showcase pack in place.
    """
    base = base or repo_data_dir()
    if base is None:
        raise ShowcasePackError("the organizers' data pack (../viseca-2026/data) was not found")

    merged: dict[str, list[dict[str, str]]] = {}
    appended_text: dict[str, str] = {}
    added: dict[str, list[str]] = {}
    for name, key in APPENDED.items():
        base_text = (base / name).read_text(encoding="utf-8")
        base_header, base_rows = _read(base_text)
        extra_path = source / name
        if not extra_path.exists():
            merged[name] = base_rows
            continue
        extra_text = extra_path.read_text(encoding="utf-8")
        extra_header, extra_rows = _read(extra_text)
        if extra_header != base_header:
            raise ShowcasePackError(
                f"{extra_path}: header must be the repo's, column for column:\n"
                f"  {','.join(base_header)}"
            )
        taken = {tuple(r[k] for k in key) for r in base_rows}
        clash = [r for r in extra_rows if tuple(r[k] for k in key) in taken]
        if clash:
            ids = ", ".join("/".join(r[k] for k in key) for r in clash)
            raise ShowcasePackError(f"{extra_path}: ids already in the repo pack: {ids}")
        merged[name] = base_rows + extra_rows
        # Appended as text, so every repo row stays exactly as the organizers wrote it.
        body = extra_text.split("\n", 1)[1] if "\n" in extra_text else ""
        appended_text[name] = base_text + ("" if base_text.endswith("\n") else "\n") + body
        added[name] = ["/".join(r[k] for k in key) for r in extra_rows]

    extra_scenarios = set(added.get("scenario_catalogue.csv", []))
    if not extra_scenarios:
        raise ShowcasePackError(f"{source}/scenario_catalogue.csv adds no scenario")
    _check(base, merged, extra_scenarios)

    staging = out.with_name(out.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(base, staging)
    for name, text in appended_text.items():
        (staging / name).write_text(text, encoding="utf-8")

    manifest = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] in appended_text:
            data = (staging / entry["path"]).read_bytes()
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            if "rows" in entry:
                entry["rows"] = len(merged[entry["path"]])
    (staging / "metadata.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if out.exists():
        shutil.rmtree(out)
    staging.rename(out)
    return {
        "out": str(out),
        "base": str(base),
        "scenarios": sorted(extra_scenarios),
        "added": added,
    }
