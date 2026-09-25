#!/usr/bin/env python3
"""One process for the Leash Wallet connector demo: the decision service with the Payment
App under /app.

An MCP client such as claude.ai talks to three things — the MCP endpoint, the authorization
server's metadata and token endpoints — and all three must be reachable from the internet
when the client runs in the cloud. Mounting the app on the service means **one** public
origin, so one quick tunnel covers the whole flow:

    make sandbox                                  # terminal 1: the offline replica
    make connect                                  # terminal 2: this, on :8010
    cloudflared tunnel --url http://localhost:8010 # terminal 3: prints https://….trycloudflare.com

Then in Claude: Settings → Connectors → Add custom connector → https://….trycloudflare.com/mcp
The phone is https://….trycloudflare.com/app/ (or http://127.0.0.1:8010/app/ on this laptop).

Claude Code needs no tunnel:  claude mcp add --transport http Leash-Wallet http://127.0.0.1:8010/mcp

Port 8010, not the decision service's 8000: this process *is* a decision service, and
`make serve` or `make serve-live` is usually running beside it — a live rehearsal in one
terminal, the connector in another. Two servers on one port is the one thing that cannot
work, so the demo has its own (`LEASH_CONNECT_PORT`), and a port that is taken is reported
with its holder rather than as a traceback. `--live` points the service at the organizers'
sandbox from .env instead of the replica.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from fastapi import Request  # noqa: E402

from leash.service.deps import REPLICA_URL, load_dotenv  # noqa: E402

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
RED, YELLOW = "\033[31m", "\033[33m"
DEFAULT_PORT = 8010


def fail(problem: str, fix: str) -> None:
    print(f"{RED}✗ {problem}{OFF}\n  {fix}", file=sys.stderr)
    raise SystemExit(2)


def port_holder(port: int) -> str:
    """Who is listening on the port, for the message. Best effort; never a failure itself."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    pid = next((line[1:] for line in out.splitlines() if line.startswith("p")), "")
    name = next((line[1:] for line in out.splitlines() if line.startswith("c")), "")
    if not pid:
        return ""
    try:
        args = subprocess.run(
            ["ps", "-p", pid, "-o", "args="], capture_output=True, text=True, timeout=3
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        args = name
    return f"PID {pid}: {args[-70:] or name}"


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def main() -> int:
    # The banner must reach a log file or a pipe before uvicorn's own lines do, not at exit.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    load_dotenv(ROOT / ".env")
    if os.environ.get("LEASH_CONNECTOR_TRACE", "").strip().lower() not in ("", "0", "false"):
        # What the client sends, line by line (leash.connector.http.trace_request) and what
        # the app answers (payment_app): the log to read when a new client misbehaves.
        logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    port = int(os.environ.get("LEASH_CONNECT_PORT", str(DEFAULT_PORT)))
    local = f"http://127.0.0.1:{port}"
    live = "--live" in sys.argv
    # The replica unless asked otherwise, whatever .env says: `make serve` pins it the same way,
    # and an agent's first order must never land on the organizers' API by accident.
    if not live:
        os.environ["LEASH_BASE_URL"] = REPLICA_URL

    if not port_free(port):
        holder = port_holder(port)
        fail(
            f"port {port} is already in use{f' ({holder})' if holder else ''}.",
            f"Stop that process, or pick another port: LEASH_CONNECT_PORT={port + 10} make connect",
        )

    import httpx

    upstream = os.environ["LEASH_BASE_URL"].rstrip("/")
    try:
        httpx.get(f"{upstream}/healthz", timeout=2).raise_for_status()
    except Exception as exc:  # noqa: BLE001 — any failure here has the same fix
        if live:
            print(f"  {YELLOW}!{OFF} cannot reach {upstream} ({exc}); starting anyway")
        else:
            fail(
                f"the offline replica is not answering at {upstream} ({exc}).",
                "Run `make sandbox` in another terminal, then `make connect` again — without "
                "it the first propose_mandate fails with upstream_unreachable.",
            )
    live_pack: dict[str, object] = {}
    if live:
        # The organizers' API serves its own pack, not the repo's (adapters/livepack.py). Set
        # before the service and the phone are imported below: both read it at load.
        from leash.adapters.client import SandboxClient
        from leash.adapters.livepack import use_live_pack

        try:
            with SandboxClient(base_url=upstream) as client:
                live_pack = use_live_pack(client)
        except Exception as exc:  # noqa: BLE001
            fail(f"could not load the data pack {upstream} serves: {exc}", "Retry: make live-pack")
    # The app is mounted here, so: the connector derives the authorization server from its
    # own public base (+ /app), introspects over loopback, and the phone talks to the service
    # on the same origin — which is what lets a tunnel's https page call it without mixed content.
    # Both loopback URLs are pinned, not defaulted: .env.example's WALLET_APP_URL is the
    # two-process setup's :8081 (make payment-app), and taking it here sent every introspection
    # to a port nothing listens on — the sign-in succeeded, then every MCP call was a 503.
    os.environ["WALLET_APP_MOUNT"] = "/app"
    os.environ.setdefault("LEASH_CONNECTOR_STATE", str(ROOT / "out" / "connector.json"))
    os.environ["WALLET_APP_URL"] = f"{local}/app"
    os.environ["LEASH_SERVICE_URL"] = local
    os.environ["LEASH_SERVICE_BROWSER_URL"] = ""

    from leash.connector.http import public_base
    from leash.service.app import app
    from payment_app.server import app as phone
    from payment_app.server import metadata_document

    @app.get("/.well-known/oauth-authorization-server/app", include_in_schema=False)
    def path_aware_metadata(request: Request) -> dict[str, object]:
        """RFC 8414 puts the well-known segment *before* the issuer's path, so an issuer of
        `…/app` is discovered here; the app also answers under its own root for clients
        that try the other form."""
        return metadata_document(public_base(request) + "/app")

    app.mount("/app", phone)

    print(f"{BOLD}Leash Wallet — connector demo{OFF}")
    where = "the organizers’ sandbox" if live else "the replica (make sandbox)"
    print(f"  upstream   {upstream}   {DIM}{where}{OFF}")
    if live_pack.get("pinned"):
        print(f"  pack       LEASH_DATA_DIR={live_pack['pinned']}   {DIM}pinned, not fetched{OFF}")
    elif live_pack:
        print(f"  pack       {live_pack.get('pack_version')}   {DIM}fetched → out/live-pack{OFF}")
    print(f"  service    {local}")
    print(f"  MCP        {local}/mcp")
    print(f"  phone      {local}/app/")
    print(f"  {DIM}public:    cloudflared tunnel --url http://localhost:{port}")
    print(f"             then paste https://<tunnel>/mcp into Claude{OFF}\n")

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
