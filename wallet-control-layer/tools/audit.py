#!/usr/bin/env python3
"""Read the append-only decision trail. `make audit`

python tools/audit.py                       the last 20 records
python tools/audit.py --limit 100
python tools/audit.py AU0016-1A2B3C         one authorization, folded and pretty-printed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from leash.audit.log import AuditLog  # noqa: E402

SYMBOL = {
    "approve": "\033[32m✓\033[0m",
    "decline": "\033[31m✗\033[0m",
    "step_up": "\033[33m?\033[0m",
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("authorization_id", nargs="?", help="one authorization, folded")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--type", choices=["decision", "resolution", "replay"])
    ap.add_argument("--path", help="override $LEASH_AUDIT_PATH")
    args = ap.parse_args()

    audit = AuditLog(args.path) if args.path else AuditLog()
    if not audit.path.exists():
        print(f"no audit trail at {audit.path} yet — start a run first", file=sys.stderr)
        return 1

    if args.authorization_id:
        record = audit.get(args.authorization_id)
        if record is None:
            print(f"no record for {args.authorization_id}", file=sys.stderr)
            return 1
        print(json.dumps(record, indent=2))
        return 0

    print(f"\033[2m{audit.path} — {len(audit)} record(s), {audit.malformed} malformed\033[0m\n")
    for record in reversed(audit.tail(args.limit, type_=args.type)):
        kind = record.get("type", "?")
        decision = str(record.get("decision") or record.get("outcome") or "")
        codes = ",".join(record.get("reason_codes", []))
        upstream = record.get("upstream", {})
        flag = (
            ""
            if upstream.get("http_status") in (200, 201)
            else f" \033[31m[{upstream.get('http_status')} {upstream.get('error') or ''}]\033[0m"
        )
        print(
            f"  {SYMBOL.get(decision, ' ')} {kind:<10} {record.get('authorization_id', ''):<26}"
            f" {decision:<9} \033[2m{codes}\033[0m{flag}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
