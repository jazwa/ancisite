"""Scratch paths: one naming scheme for anci's temp files, local + guest.

Every command uploads/compiles via a temp file it then removes; the run-id
suffix (pid + random) makes concurrent invocations safe — also across machines,
whose pids can collide on a shared box — and the `.anci-` prefix keeps them out
of globs and tab-completion. One helper so the convention can't drift per call
site.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

RUN_ID = f"{os.getpid()}-{secrets.token_hex(3)}"


def local(tag: str) -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp")) / f".anci-{tag}-{RUN_ID}"


def write_private(tag: str, content: str | bytes) -> Path:
    p = local(tag)
    p.unlink(missing_ok=True)
    data = content.encode() if isinstance(content, str) else content
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return p


def remote(tag: str) -> str:
    return f"/tmp/.anci-{tag}-{RUN_ID}"


def in_dir(root: str, tag: str) -> str:
    return f"{root}/.tmp-{tag}-{RUN_ID}"
