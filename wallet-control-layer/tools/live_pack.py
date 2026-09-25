#!/usr/bin/env python3
"""Write the data pack the REAL sandbox serves to out/live-pack. `make live-pack`

The live targets (`serve-live`, `probe-live`, `replay-live`, `connect.py --live`) already do
this before they start; run it on its own to see what the API serves, and how that differs
from the organizers' repo pack, without starting anything.

    make live-pack
    LEASH_DATA_DIR=out/live-pack make verify-data    # the written files against their manifest
"""

from __future__ import annotations

import csv
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from leash.adapters.client import SandboxClient  # noqa: E402
from leash.adapters.datapack import repo_data_dir  # noqa: E402
from leash.adapters.livepack import HISTORY, LIVE_PACK_DIR, TABLES, fetch, is_replica  # noqa: E402
from leash.service.deps import load_dotenv  # noqa: E402

KEYS = {
    "customers": "customer_id",
    "accounts": "account_id",
    "cards": "card_id",
    "merchants": "merchant_id",
    "items": "item_id",
    "fx_rates": "from_currency",
    "scenario_catalogue": "scenario_id",
}


def main() -> int:
    load_dotenv(ROOT / ".env")
    base_url = os.environ.get("LEASH_BASE_URL", "")
    if not base_url or is_replica(base_url):
        print(
            "LEASH_BASE_URL is not the live API — the replica serves the repo pack as it is.",
            file=sys.stderr,
        )
        return 2
    with SandboxClient(base_url=base_url) as client:
        manifest = fetch(client, LIVE_PACK_DIR)

    print(f"pack {manifest['pack_version']} from {base_url}")
    print(f"  → {LIVE_PACK_DIR.relative_to(ROOT)}  ({manifest['fetched_at']})\n")
    repo = repo_data_dir()
    print(f"  {'table':20} {'live':>6} {'repo':>6}  repo ids still served")
    for name in TABLES:
        live_ids = _ids(LIVE_PACK_DIR / f"{name}.csv", KEYS[name])
        repo_ids = _ids(repo / f"{name}.csv", KEYS[name]) if repo else set()
        kept = f"{len(repo_ids & live_ids)} of {len(repo_ids)}" if repo_ids else "-"
        print(f"  {name:20} {len(live_ids):>6} {len(repo_ids):>6}  {kept}")
    if repo:
        same = _sha(LIVE_PACK_DIR / HISTORY) == _sha(repo / HISTORY)
        print(f"  {HISTORY:20} {'identical to the repo' if same else 'differs from the repo'}")
    print(f"\n  scenarios: {', '.join(manifest['scenario_ids'])}")
    return 0


def _ids(path: Path, key: str) -> set[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return {row[key] for row in csv.DictReader(fh)}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
