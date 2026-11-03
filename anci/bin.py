"""Vendoring: fetch pinned third-party binaries, verify, cache.

One helper for every binary anci fetches onto the dev machine: caddy, litestream,
sops. Hashes come from the bundled version map (upstream-published checksums
where available); a mismatch refuses the artifact outright (pitfall #3).

Caches under ~/.local/share/anci/bin/<name> (see anci/paths.py). sops is
special: it stays on the dev machine (decryption is client-side); caddy/litestream
are pushed onward to the guest as ordinary artifacts.
"""

from __future__ import annotations

import hashlib
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from . import errors, ui


def local_platform() -> str:
    system = platform.system()
    os_name = {"Linux": "linux", "Darwin": "macos"}.get(system, system.lower())
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return f"{os_name}-{arch}"


def vendor(
    url: str,
    sha512: str | None,
    sha256: str | None,
    name: str,
    artifacts_dir: Path,
    *,
    extract: str | None = None,
) -> Path:
    dest = artifacts_dir / name
    if dest.is_file():
        return dest

    if extract is None:
        _download(url, dest)
        _verify(dest, sha512, sha256)
        dest.chmod(0o755)
        return dest

    archive = artifacts_dir / f"{name}.archive"
    if not archive.is_file():
        _download(url, archive)
    _verify(archive, sha512, sha256)
    dest.write_bytes(_member(archive, extract, url, name))
    dest.chmod(0o755)
    return dest


def _member(archive: Path, name_in_archive: str, url: str, name: str) -> bytes:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            try:
                return zf.read(name_in_archive)
            except KeyError:
                pass
    else:
        with tarfile.open(archive) as tf:
            member = tf.extractfile(name_in_archive)
            if member is not None:
                return member.read()
    raise errors.EnvError(
        f"{name} archive is missing its binary",
        debug=f"url: {url}",
    )


def _download(url: str, dest: Path) -> None:
    print(f"  {ui.dim(f'downloading {url}')} (this might take a while)")
    urllib.request.urlretrieve(url, dest)


def _verify(path: Path, sha512: str | None, sha256: str | None) -> None:
    expected, algo = (sha512, hashlib.sha512()) if sha512 else (sha256, hashlib.sha256())
    if not expected:
        return
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            algo.update(chunk)
    if algo.hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise errors.EnvError(
            f"the downloaded artifact is corrupted: {path.name}",
            why="it doesn't match the hash pinned in anci's version map",
            try_="just retry — if it persists, the pinned hash or the URL is wrong",
            debug=f"expected {expected}, got {algo.hexdigest()}",
        )
