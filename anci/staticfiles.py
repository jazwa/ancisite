"""An exe's static files: scanning a directory into a versioned, shippable tree.

A version's id is the hash of a *manifest* — one `<path>\\0<sha256>\\n` line per
file, sorted — so the same files give the same id on every machine, whatever
their mtimes, modes or walk order. Each file is shipped to the box's
content-addressed blob store by its own sha256 (so an unchanged or shared file
is never re-uploaded or stored twice); the version itself is a tree of symlinks
into that store (see `anci/converge/run.py`).

Only regular files and directories are shipped. Symlinks, devices and names a
shell could misread are refused up front; empty directories aren't shipped.
Modes aren't kept: the box installs every file 0644, every dir 0755.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import errors, layout
from .hashes import sha256_file


@dataclass
class Scanned:
    id: str
    root: Path
    n_files: int
    n_bytes: int
    entries: list[tuple[str, str]]
def bad_name(rel: str) -> str | None:
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return "isn't valid UTF-8"
    if "\n" in rel or "\r" in rel:
        return "contains a newline"
    if rel.startswith("/"):
        return "is absolute"
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return "has an empty, '.' or '..' component"
    return None


def manifest_id(entries: list[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for rel, sha in sorted(entries):
        h.update(f"{rel}\0{sha}\n".encode())
    return layout.blob_id(h.hexdigest())


def _refuse(root: Path, rel: str, why: str) -> errors.AppError:
    return errors.AppError(
        f"can't ship {root / rel}: it {why}",
        why="static files are shipped as plain files and directories only",
        try_="remove or rename it (copy a symlink's target in instead)",
    )


def _walk(root: Path) -> list[str]:
    out: list[str] = []

    def visit(d: Path, prefix: str) -> None:
        with os.scandir(d) as it:
            for e in it:
                rel = prefix + e.name
                why = bad_name(rel)
                if why:
                    raise _refuse(root, rel, why)
                mode = e.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    raise _refuse(root, rel, "is a symlink")
                if stat.S_ISDIR(mode):
                    visit(Path(e.path), rel + "/")
                elif stat.S_ISREG(mode):
                    out.append(rel)
                else:
                    raise _refuse(root, rel, "isn't a regular file or directory")

    visit(root, "")
    return sorted(out)


def scan(root: Path) -> Scanned:
    entries, n_bytes = [], 0
    for rel in _walk(root):
        p = root / rel
        entries.append((rel, sha256_file(p)))
        n_bytes += p.stat().st_size
    return Scanned(id=manifest_id(entries), root=root, n_files=len(entries),
                   n_bytes=n_bytes, entries=entries)
