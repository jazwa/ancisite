"""Content hashes: sha256, as files and blobs are named and compared everywhere."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    """sha256 of a dev-machine-side file, streaming."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(content: str | bytes) -> str:
    """sha256 of in-memory content (a rendered file)."""
    data = content.encode() if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()
