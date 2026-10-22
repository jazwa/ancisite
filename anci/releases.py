"""The release log: append-only JSONL per exe.

One line per deploy that moved an exe's `current` or `static` (written as
soon as the links move, before the restart — a failed restart is retried
through its marker, never lost from the log):

    {"ts": "...", "env": "...", "blob": "<sha256>", "source": "...", "user": "...",
     "static": "<id>"}                     # `static` only when the exe ships static files

Lives on the guest at /var/lib/anci/releases/<exe>/releases.jsonl. Appends
(crash-safe, concurrent-safe); readers skip a torn trailing line. Trimmed in
batches: on reaching RELEASES_MAX lines it drops to the newest RELEASES_KEPT, so
the rewrite is rare — GC works on blob mtimes, not log references.
"""

from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone

from . import ssh
from .layout import ANCI_GROUP, RELEASES_DIR


RELEASES_MAX = 16384    # trim when the log reaches this many lines…
RELEASES_KEPT = 8192    # …down to the newest this many


def release_file(exe_name: str) -> str:
    return f"{RELEASES_DIR}/{exe_name}/releases.jsonl"


def append(conn: ssh.Conn, exe_name: str, *, env: str, blob: str, source: str, user: str,
           static: str | None = None) -> None:
    """Append one release record (converge calls this once the new version runs)."""
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": env,
        "blob": blob,
        "source": source,
        "user": user,
    }
    if static is not None:
        record["static"] = static
    line = json.dumps(record, separators=(",", ":"))
    # root-owned, in a setgid group-writable dir: any deployer (all have
    # passwordless sudo) can append; ownership doesn't depend on who deployed
    f = shlex.quote(release_file(exe_name))
    ssh.sudo(
        conn,
        f"install -d -o root -g {ANCI_GROUP} -m 2775 {RELEASES_DIR}/{exe_name} && "
        f"echo {shlex.quote(line)} >> {f} && "
        # trim in batches; rewritten in place, so the file keeps its owner/mode
        f'if [ "$(wc -l < {f})" -ge {RELEASES_MAX} ]; then '
        f"tail -n {RELEASES_KEPT} {f} > {f}.tmp && cat {f}.tmp > {f} && rm -f {f}.tmp; fi",
    )
