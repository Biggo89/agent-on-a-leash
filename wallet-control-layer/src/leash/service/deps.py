"""Wiring: read the environment, load the read-only data, build one Supervisor.

Also loads ``.env`` by hand. The team key belongs in a git-ignored file rather than a shell
export a non-developer has to remember on the day, and that is not worth a dependency.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from ..adapters.client import SandboxClient
from ..adapters.datapack import DataPack, data_dir
from ..adapters.history import HistoryIndex
from ..audit.log import AuditLog
from ..runtime.supervisor import ENGINE_VERSION, Supervisor

log = logging.getLogger("leash.service")

REPLICA_URL = "http://127.0.0.1:8099"


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a .env file. Existing variables always win."""
    env = path or Path(os.environ.get("LEASH_ENV_FILE", ".env"))
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #")[0].strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


@lru_cache(maxsize=1)
def supervisor() -> Supervisor:
    load_dotenv()
    base_url = os.environ.get("LEASH_BASE_URL", REPLICA_URL)
    key = os.environ.get("TEAM_API_KEY", "")
    if base_url != REPLICA_URL and not key:
        # Fail loudly here rather than with a 401 on the first decision of the demo.
        log.warning(
            "LEASH_BASE_URL points at %s but TEAM_API_KEY is empty — "
            "authenticated calls will be refused",
            base_url,
        )

    pack = DataPack.load()
    history = HistoryIndex.load(data_dir() / "authorization_history.csv")
    audit = AuditLog()
    log.info(
        "engine %s → %s (%d merchants, %d audit records on disk)",
        ENGINE_VERSION,
        base_url,
        len(pack.merchants),
        len(audit),
    )
    return Supervisor(
        SandboxClient(base_url=base_url, api_key=key or "dev-key"),
        pack=pack,
        history=history,
        audit=audit,
    )
