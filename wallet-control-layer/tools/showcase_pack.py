#!/usr/bin/env python3
"""Write the repo pack plus the scenarios in showcase/ to out/showcase-pack. `make showcase-pack`

The showcase targets (`sandbox-showcase`, `serve-showcase`, `replay-showcase`) run this before
they start, so a row edited in showcase/ is always what runs. See showcase/README.md.

    make showcase-pack
    LEASH_DATA_DIR=out/showcase-pack make verify-data    # the written files against their manifest
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from leash.adapters.showcase import ShowcasePackError, build  # noqa: E402


def main() -> int:
    try:
        result = build()
    except ShowcasePackError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    out = Path(result["out"]).relative_to(ROOT)
    print(f"✓ showcase pack written to {out}")
    print(f"  base: {result['base']}")
    for name, ids in result["added"].items():
        print(f"  + {len(ids)} row(s) in {name}: {', '.join(ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
