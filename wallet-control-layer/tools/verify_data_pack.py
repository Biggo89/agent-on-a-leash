#!/usr/bin/env python3
"""Verify the organizers' data pack against metadata.json (row counts + SHA-256).

metadata.json is the authoritative manifest. If a file here does not match, something has
been edited or truncated locally — find out before it silently changes a decision.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sandbox.fixtures import data_dir  # noqa: E402


def main() -> int:
    root = data_dir()
    manifest = json.loads((root / "metadata.json").read_text())
    print(f"data pack: {root}\npack_version: {manifest.get('pack_version')}\n")

    failures = 0
    for entry in manifest["files"]:
        path = root / entry["path"]
        if not path.exists():
            print(f"  MISSING  {entry['path']}")
            failures += 1
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ok_hash = digest == entry["sha256"]
        ok_rows = True
        if "rows" in entry and entry["format"] == "csv":
            with path.open(encoding="utf-8") as fh:
                ok_rows = sum(1 for _ in fh) - 1 == entry["rows"]
        status = "ok" if (ok_hash and ok_rows) else "FAIL"
        if status == "FAIL":
            failures += 1
            detail = []
            if not ok_hash:
                detail.append(f"sha256 {digest[:12]}… != {entry['sha256'][:12]}…")
            if not ok_rows:
                detail.append("row count mismatch")
            print(f"  {status:<8} {entry['path']}  ({'; '.join(detail)})")
        else:
            print(f"  {status:<8} {entry['path']}")

    print()
    if failures:
        print(f"✗ {failures} file(s) do not match the manifest.")
        return 1
    print(f"✓ all {len(manifest['files'])} files match the manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
