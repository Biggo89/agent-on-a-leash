#!/usr/bin/env python3
"""Start the decision service against the REAL sandbox — the one command for the day.

Everything that can be wrong before the demo is checked here, with a fix in the message,
because "401 Unauthorized" three requests into a live run is not a debuggable error at hour 20.

    make serve-live
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from leash.adapters.client import SandboxClient, limit_seconds  # noqa: E402
from leash.adapters.livepack import use_live_pack  # noqa: E402
from leash.compile import compiler_mode  # noqa: E402
from leash.service.deps import load_dotenv  # noqa: E402

REPLICA = "http://127.0.0.1:8099"
GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)


def fail(problem: str, fix: str) -> None:
    print(f"{RED}✗ {problem}{OFF}\n  {fix}", file=sys.stderr)
    raise SystemExit(2)


def warm_scenarios(port: int) -> None:
    """Compile every scenario with the model once, before the leash demo asks.

    The demo boots from `GET /v1/scenarios?mode=auto`, and ten model calls take ~20 s even in
    parallel — too long behind a Live switch on stage. The service caches the result, so
    doing it here, right after start, makes the first boot instant.
    """
    import httpx

    url = f"http://127.0.0.1:{port}/v1/scenarios"
    for _ in range(120):
        try:
            body = httpx.get(url, params={"mode": "auto"}, timeout=300).json()
            break
        except httpx.TransportError:
            time.sleep(0.5)
    else:
        return
    irs = [s["ir"] for s in body.get("scenarios", [])]
    fell_back = sum("fell_back_to_baseline" in (ir.get("compiler_notes") or []) for ir in irs)
    if fell_back:
        print(
            f"  {YELLOW}!{OFF} {fell_back} of {len(irs)} scenarios fell back to the baseline "
            "compiler; the demo retries them on its next boot"
        )
    else:
        print(
            f"  {GREEN}✓{OFF} {len(irs)} scenarios compiled with the model — the demo boots "
            "instantly"
        )


def main() -> int:
    load_dotenv(ROOT / ".env")
    base_url = os.environ.get("LEASH_BASE_URL", "")
    key = os.environ.get("TEAM_API_KEY", "")
    port = int(os.environ.get("LEASH_PORT", "8000"))

    if not base_url:
        fail(
            "LEASH_BASE_URL is not set.",
            f"cp .env.example .env  — it already contains the organizers' URL "
            f"({DIM}or use `make serve` for the offline replica{OFF})",
        )
    if base_url == REPLICA:
        fail(
            "LEASH_BASE_URL points at the offline replica.",
            "Use `make serve` for the replica, or set the organizers' URL in .env.",
        )
    if not key:
        fail(
            "TEAM_API_KEY is empty.",
            "Put the key the organizers issued into .env as TEAM_API_KEY=... — never commit it.",
        )

    import httpx

    print(f"{BOLD}Wallet Control Layer — decision service{OFF}")
    print(f"  sandbox   {base_url}")
    print(f"  key       {DIM}{key[:4]}…{key[-2:]} ({len(key)} chars){OFF}")
    try:
        health = httpx.get(f"{base_url.rstrip('/')}/healthz", timeout=10)
        health.raise_for_status()
        print(f"  {GREEN}✓{OFF} sandbox reachable — {health.json()}")
    except Exception as exc:
        fail(f"cannot reach {base_url}: {exc}", "Check the venue network, then retry.")

    try:
        boot = httpx.get(
            f"{base_url.rstrip('/')}/v1/bootstrap",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        if boot.status_code == 401:
            fail("the sandbox rejected the team key (401).", "Check TEAM_API_KEY in .env.")
        boot.raise_for_status()
        limits = boot.json().get("limits", {})
        print(f"  {GREEN}✓{OFF} authenticated — limits: {limits}")
        deadline = limit_seconds(boot.json(), "decision_timeout_seconds")
        if deadline is not None and int(deadline) != 8:
            print(
                f"  {YELLOW}!{OFF} the deadline is {deadline}s, not the documented 8s — "
                "re-check specs/deadline-guard.md RESERVE_MS"
            )
    except SystemExit:
        raise
    except Exception as exc:
        print(f"  {YELLOW}!{OFF} bootstrap failed ({exc}); starting anyway")

    # The sandbox serves its own pack, not the repo's (adapters/livepack.py). Loaded before
    # uvicorn imports the app, which reads the pack on its first request.
    try:
        with SandboxClient(base_url=base_url, api_key=key) as client:
            pack = use_live_pack(client)
    except Exception as exc:
        fail(f"could not load the data pack the sandbox serves: {exc}", "Retry: make live-pack")
    if "pinned" in pack:
        print(f"  {YELLOW}!{OFF} data pack pinned by LEASH_DATA_DIR={pack['pinned']}; not fetched")
    else:
        print(
            f"  {GREEN}✓{OFF} data pack {pack.get('pack_version')} — "
            f"{len(pack.get('scenario_ids', []))} scenarios, fetched → out/live-pack"
        )
        if pack.get("stale"):
            print(f"    {YELLOW}! refresh failed, using the copy on disk: {pack['stale']}{OFF}")

    if compiler_mode() != "baseline":
        threading.Thread(target=warm_scenarios, args=(port,), daemon=True).start()
        print(f"  {DIM}compiling every scenario with the model in the background (~20 s){OFF}")

    print(f"\n  {BOLD}service   http://127.0.0.1:{port}{OFF}")
    print(f"  {DIM}docs      http://127.0.0.1:{port}/docs")
    print(f"  postman   postman/leash.postman_collection.json{OFF}\n")

    import uvicorn

    uvicorn.run("leash.service.app:app", host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
