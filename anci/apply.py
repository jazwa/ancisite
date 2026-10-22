"""The diff-and-apply engine.

Desired state is expressed as artifacts (files with content/mode/owner) and
links (symlinks). One batched probe of the guest yields actual state; only the
delta is applied. No framework and no agent: just a hash-diff over plain ssh,
idempotent by construction.

Pure decision functions (`diff_files`, `diff_links`) are separated from the
ssh-executing `apply()` so they're unit-testable without a box.
"""

from __future__ import annotations

import hashlib
import posixpath
import secrets
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import errors, layout, scratch, ssh


# the only places anci ever deletes: entries *inside* its exe tree and its two unit
# dirs (never the dirs themselves), plus its two rendered configs
_DELETE_DIRS = (layout.EXE_DIR, layout.UNITS_DIR, "/etc/systemd/system")
_DELETE_FILES = (layout.LITESTREAM_FILE, layout.CADDY_FILE)


@dataclass
class FileArtifact:
    path: str
    content: str | bytes        # bytes for binaries (caddy), str for text files
    mode: str = "0644"          # e.g. "0755"; installed as root


@dataclass
class LinkArtifact:
    path: str
    target: str


@dataclass
class DeleteArtifact:
    """A path anci owns that must **not** exist — the missing third shape.

    Deploy-time reconciliation renders these for state the conf no longer
    declares (a removed unit, its systemd symlink, the exe dir of a gone exe).
    `tree=True` deletes a directory we own (`rm -rf`); deletion is data-only:
    leftovers the conf can't identify are reported, never rendered here.
    """

    path: str
    tree: bool = False

    def __post_init__(self):
        # last line of defence for `rm -rf`: only a clean absolute path inside a
        # location anci owns, however the path was derived
        if (not posixpath.isabs(self.path) or posixpath.normpath(self.path) != self.path
                or not (self.path in _DELETE_FILES
                        or any(self.path.startswith(d + "/") for d in _DELETE_DIRS))):
            raise ValueError(f"refusing to delete {self.path!r}: not a path anci owns")


@dataclass
class ApplyResult:
    changed_files: list[str] = field(default_factory=list)
    changed_links: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changed_files or self.changed_links or self.deleted)


def sha256_file(path: Path) -> str:
    """sha256 of a dev-machine-side file, streaming (`deploy` uses it for the release source)."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def guest_sha(conn: ssh.Conn, path: str) -> str | None:
    """sha256 of one guest file, or None if missing — `probe` for a single
    path (as root, so root-only files like the secrets file read too)."""
    return probe(conn, [path], [])[0][path]


# ---------------------------------------------------------------------------
# pure decision functions


def _sha(content: str | bytes) -> str:
    data = content.encode() if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def diff_files(
    expected: list[FileArtifact], actual_hashes: dict[str, str | None]
) -> list[FileArtifact]:
    """Files whose expected content-hash differs from actual (or missing)."""
    out = []
    for f in expected:
        if actual_hashes.get(f.path) != _sha(f.content):
            out.append(f)
    return out


def diff_links(
    expected: list[LinkArtifact], actual_targets: dict[str, str | None]
) -> list[LinkArtifact]:
    """Links whose actual target differs from expected (or missing)."""
    return [l for l in expected if actual_targets.get(l.path) != l.target]


def diff_deletes(
    expects: list["DeleteArtifact"], exists: dict[str, str | bool | None]
) -> list["DeleteArtifact"]:
    """Deletions whose path **still exists** on the guest (already gone → no-op)."""
    return [d for d in expects if exists.get(d.path, False)]


# ---------------------------------------------------------------------------
# guest probing


def probe(
    conn: ssh.Conn, files: list[str], links: list[str], deletes: list[str] | None = None
) -> tuple[dict[str, str | None], dict[str, str | None], dict[str, str | None]]:
    """Actual state of files (sha256 or None), links (target or None), and of
    existence paths (size as a string, or None when absent) — one round trip,
    under one `sudo` (root-only files hash too).

    Every command is failure-tolerant (missing files/links are reported, not
    raised). `deletes` is the *existence check*: a delete path that's already
    gone means no delete is applied; a blob is judged by its size (its name is
    its hash).
    """
    q = shlex.quote
    probes: dict[str, str] = {}
    for p in files:
        probes[f"f {p}"] = f"sha256sum {q(p)} | cut -c1-64"
    for l in links:
        probes[f"l {l}"] = f"readlink {q(l)}"
    for p in deletes or []:
        probes[f"e {p}"] = f"stat -c %s {q(p)}"
    found = ssh.facts(conn, probes, root=True)
    hashes = {p: ssh.fact(found, f"f {p}") for p in files}
    targets = {l: ssh.fact(found, f"l {l}") for l in links}
    present = {p: ssh.fact(found, f"e {p}") for p in deletes or []}
    return hashes, targets, present


# ---------------------------------------------------------------------------
# applying


def _write_temp(name: str, content: str | bytes) -> str:
    # 0600: an artifact may be the decrypted secrets file
    return str(scratch.write_private(f"apply-{name}", content))


def verify_upload(conn: ssh.Conn, remote: str, expected_sha: str) -> None:
    """Hash-verify an uploaded temp file before it is installed (pitfall #3);
    remove it and raise on a mismatch. Shared by `apply` and the blob upload."""
    out = ssh.script(conn, f"sha256sum {shlex.quote(remote)}").stdout.split()
    if not out or out[0] != expected_sha:
        ssh.script(conn, f"rm -f {shlex.quote(remote)}")
        raise errors.EnvError(
            "an uploaded file didn't survive the trip",
            why="the bytes on the box don't match the ones on your dev machine",
            try_="just retry",
        )


def apply(
    conn: ssh.Conn,
    files: list[FileArtifact],
    links: list[LinkArtifact],
    probe_result: tuple[dict[str, str | None], dict[str, str | None], dict[str, str | None]] | None = None,
    deletes: list[DeleteArtifact] | None = None,
) -> ApplyResult:
    """Probe (unless given), upload/fix/remove the delta, as root. Returns what
    changed. `deletes` carries the reconciliation delta (paths that must not
    exist); `probe_result`'s third element says which of them still exist, so an
    already-absent path is a no-op rather than an `rm`.
    """
    q = shlex.quote
    result = ApplyResult()
    deletes = deletes or []
    if probe_result is None:
        probe_result = probe(conn, [f.path for f in files], [l.path for l in links],
                             [d.path for d in deletes])
    hashes, targets, del_exists = probe_result

    for i, f in enumerate(diff_files(files, hashes)):
        local = _write_temp(f"{i}.tmp", f.content)
        remote_tmp = scratch.remote(f"apply-{i}")
        try:
            ssh.put(conn, local, remote_tmp, private=True)
            verify_upload(conn, remote_tmp, _sha(f.content))
            ssh.sudo(conn, f"install -d {q(posixpath.dirname(f.path))} && "
                           f"install -m {f.mode} {q(remote_tmp)} {q(f.path)} && rm -f {q(remote_tmp)}")
        finally:
            Path(local).unlink(missing_ok=True)
        result.changed_files.append(f.path)

    for l in diff_links(links, targets):
        # atomic swap: a new link beside the old one, renamed over it — the path
        # never goes missing (`ln -sfn` alone unlinks, then links)
        tmp = f"{l.path}.anci-{secrets.token_hex(4)}"
        ssh.sudo(conn, f"ln -sfn {q(l.target)} {q(tmp)} && mv -T {q(tmp)} {q(l.path)}")
        result.changed_links.append(l.path)

    for d in diff_deletes(deletes, del_exists):
        # rm -f is already idempotent; a directory we own gets rm -rf
        ssh.sudo(conn, f"rm -rf {q(d.path)}" if d.tree else f"rm -f {q(d.path)}")
        result.deleted.append(d.path)

    return result
