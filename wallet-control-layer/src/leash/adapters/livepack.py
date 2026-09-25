"""The data pack the live API serves, written to disk in the layout of the organizers' repo.

The API does not have to serve the pack in github.com/Swiss-ai-Weeks/viseca-2026. On
2026-09-24 it began serving `saw26-hackaton-api`: every row of the repo pack kept, more rows
added, and ten team-specific scenarios (SCEN01xx) in place of SCEN0000–0004, which the API no
longer knows. The engine reads its pack from a directory, so every live entry point writes the
API's pack here first and points `LEASH_DATA_DIR` at it. The replica and the offline board keep
the repo pack, which is what they model.

Three kinds of file:

- **served** — the seven reference tables (`GET /v1/reference-data`) and the history CSV
  (`GET /v1/reference-data/authorization-history.csv`), written as the API sent them;
- **runtime** — `purchase_attempts.csv`, `purchase_attempt_items.csv` and
  `scenario_authorities.csv` are delivered one event at a time on a live run, so they are
  written header-only: the loader needs the file, and an empty table is the truth before a run;
- **carried** — `schemas/` from the repo pack. The live event was checked field for field
  against the repo's example on 2026-09-24 and matched.

`metadata.json` is written in the repo manifest's shape, so `make verify-data` checks this
directory too.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .datapack import _PROJECT_ROOT, repo_data_dir

LIVE_PACK_DIR = _PROJECT_ROOT / "out" / "live-pack"

TABLES = (
    "customers",
    "accounts",
    "cards",
    "merchants",
    "items",
    "fx_rates",
    "scenario_catalogue",
)
HISTORY = "authorization_history.csv"
# The repo's headers: the loader reads these files by column name even when they are empty.
RUNTIME_HEADERS: dict[str, tuple[str, ...]] = {
    "purchase_attempts.csv": (
        "authorization_id",
        "scenario_id",
        "replay_order",
        "authority_id",
        "card_id",
        "merchant_id",
        "timestamp",
        "amount",
        "currency",
        "billing_amount_chf",
        "items_subtotal",
        "delivery_fee",
        "channel",
        "customer_device_id",
        "authority_status",
        "card_status_at_attempt",
        "spend_in_period_before_chf",
        "recent_attempt_count_10m",
        "fulfillment_method",
        "delivery_by",
        "order_returnable",
        "order_cancellable",
        "related_authorization_id",
        "related_authorization_status",
        "purchase_description",
    ),
    "purchase_attempt_items.csv": (
        "authorization_id",
        "line_no",
        "item_id",
        "item_name",
        "item_category",
        "quantity",
        "unit_price",
        "currency",
        "item_details",
    ),
    "scenario_authorities.csv": (
        "authority_id",
        "customer_id",
        "card_id",
        "valid_from",
        "valid_until",
        "initial_status",
    ),
}


class LivePackError(RuntimeError):
    """The API's pack is incomplete or inconsistent; nothing was written."""


def is_replica(base_url: str) -> bool:
    """The offline replica serves the repo pack, so it never needs a live pack."""
    return "127.0.0.1" in base_url or "localhost" in base_url


def write(
    reference: dict[str, Any],
    history_csv: bytes,
    out: Path = LIVE_PACK_DIR,
    *,
    source: str,
    schemas_from: Path | None = None,
) -> dict[str, Any]:
    """Write the pack to `out`, whole or not at all, and return its manifest.

    Built in a sibling directory and swapped in at the end, so a fetch that fails halfway
    leaves the previous pack in place rather than half of a new one.
    """
    tables = reference.get("tables")
    if not isinstance(tables, dict):
        raise LivePackError("GET /v1/reference-data carried no `tables`")
    missing = [t for t in TABLES if not isinstance(tables.get(t), list)]
    if missing:
        raise LivePackError(f"GET /v1/reference-data is missing tables: {', '.join(missing)}")
    if not tables["scenario_catalogue"]:
        raise LivePackError("the API serves no scenarios")
    # Lines minus the header: the same count `tools/verify_data_pack.py` checks.
    history_rows = max(0, len(history_csv.splitlines()) - 1)
    declared = (reference.get("history") or {}).get("rows")
    if isinstance(declared, int) and declared != history_rows:
        raise LivePackError(
            f"the history CSV has {history_rows} rows but the API declares {declared}; "
            "the download was probably truncated"
        )

    staging = out.with_name(out.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    files: list[dict[str, Any]] = []
    for name in TABLES:
        rows = tables[name]
        header = list(rows[0]) if rows else []
        files.append(_write_csv(staging, f"{name}.csv", header, rows))
    (staging / HISTORY).write_bytes(history_csv)
    files.append(_entry(staging, HISTORY, rows=history_rows))
    for name, runtime_header in RUNTIME_HEADERS.items():
        files.append(_write_csv(staging, name, list(runtime_header), []))
    schemas = schemas_from if schemas_from is not None else _repo_schemas()
    if schemas is not None and schemas.is_dir():
        shutil.copytree(schemas, staging / "schemas")
        for path in sorted((staging / "schemas").glob("*.json")):
            files.append(_entry(staging, f"schemas/{path.name}"))

    manifest = {
        "pack_version": reference.get("pack_version"),
        "classification": reference.get("classification"),
        "source": source,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scenario_ids": [str(s["scenario_id"]) for s in tables["scenario_catalogue"]],
        "note": "written by `make live-pack` from the API; runtime tables are header-only",
        "metadata_hash_scope": "metadata.json is excluded from its own file hash list",
        "files": files,
    }
    (staging / "metadata.json").write_text(json.dumps(manifest, indent=2) + "\n")

    shutil.rmtree(out, ignore_errors=True)
    staging.rename(out)
    return manifest


def fetch(client: Any, out: Path = LIVE_PACK_DIR) -> dict[str, Any]:
    """Download the pack the API serves and write it to `out`."""
    return write(
        client.reference_data(),
        client.reference_history_csv(),
        out,
        source=client.base_url,
    )


def use_live_pack(client: Any, out: Path = LIVE_PACK_DIR) -> dict[str, Any]:
    """Fetch the live pack and point this process at it, before anything loads a pack.

    A `LEASH_DATA_DIR` naming some other directory wins and nothing is fetched: someone chose
    it. One naming the repo pack does not count — `.env.example` ships exactly that, and the
    repo pack is the one thing known to be wrong against the live API — and neither does `out`
    itself, so a second call refreshes rather than reading its own output as a pin.

    A failed fetch with a pack already on disk keeps the one on disk and says so under
    `stale`, because refusing to start over a flaky reference endpoint would cost more than a
    pack a few hours old.
    """
    pinned = os.environ.get("LEASH_DATA_DIR")
    defaults = {out.resolve()} | ({repo.resolve()} if (repo := repo_data_dir()) else set())
    if pinned and Path(pinned).resolve() not in defaults:
        return {"pinned": pinned}
    try:
        manifest = fetch(client, out)
    except Exception as exc:
        if not (out / "metadata.json").is_file():
            raise
        manifest = {**json.loads((out / "metadata.json").read_text()), "stale": str(exc)}
    os.environ["LEASH_DATA_DIR"] = str(out)
    return manifest


def _write_csv(
    root: Path, name: str, header: list[str], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if v is None else str(v) for k, v in row.items()})
    (root / name).write_text(buffer.getvalue(), encoding="utf-8")
    return _entry(root, name, rows=len(rows))


def _entry(root: Path, name: str, *, rows: int | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": name,
        "format": name.rsplit(".", 1)[-1],
        "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
    }
    if rows is not None:
        entry["rows"] = rows
    return entry


def _repo_schemas() -> Path | None:
    repo = repo_data_dir()
    return repo / "schemas" if repo is not None else None
