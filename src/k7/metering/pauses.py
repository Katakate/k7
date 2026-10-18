"""k7d pause/resume transitions, recorded where they happen.

The k7d daemon does not report a frozen VM as paused, and the deployment
annotation is out of the agent's reach (no API token). So whoever pauses
or resumes a k7d VM on the node (k7-agent, or ``k7 --core`` there) appends
``(time, paused)`` under the CRI sandbox id here, and the node sampler
splits its windows at those times.
"""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path

from . import config
from .spool import write_atomic

FILE = "pause-transitions.json"
KEEP = 32
MAX_AGE_S = 30 * 86400


def load(root: Path | None = None) -> dict[str, list[tuple[float, bool]]]:
    path = (root or config.spool_dir()) / FILE
    if not path.exists():
        return {}
    return {k: [(float(t), bool(p)) for t, p in v] for k, v in json.loads(path.read_text()).items()}


def record(sandbox_id: str, paused: bool, t: float | None = None, root: Path | None = None) -> None:
    root = root or config.spool_dir()
    root.mkdir(parents=True, exist_ok=True)
    now = time.time() if t is None else t
    with open(root / ".pause.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(root)
        data[sandbox_id] = [*data.get(sandbox_id, []), (now, paused)][-KEEP:]
        data = {k: v for k, v in data.items() if v[-1][0] > now - MAX_AGE_S}
        write_atomic(root / FILE, json.dumps(data).encode())
