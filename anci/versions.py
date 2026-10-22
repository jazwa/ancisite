"""Bundled anci-version mapping loader."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from importlib import resources
from pathlib import Path

from . import errors

_VERSIONS_NAME = "versions.toml"


VersionError = errors.ConfError   # a bad/unknown version is a conf problem


@dataclass
class VersionEntry:
    version: str
    debian_image_url: str
    sha512: str | None
    litestream: str | None
    litestream_url: str | None
    litestream_sha256: str | None
    caddy: str | None
    caddy_url: str | None
    caddy_sha512: str | None
    sqlite: str | None
    sqlite_url: str | None
    sqlite_sha256: str | None
    sops: str | None
    # dev-machine platform ("macos-aarch64", …) -> (url, sha256)
    sops_builds: dict[str, tuple[str, str]] = field(default_factory=dict)


def load(path: Path | str | None = None) -> dict[str, VersionEntry]:
    """Load a versions map, or the bundled one when `path` is None.

    The bundled file is read through `importlib.resources`, not a filesystem
    path, so it also works from a zipapp (where the package lives inside the
    archive and a plain `open()` on `__file__`'s directory would fail).
    """
    if path is None:
        text = resources.files(__package__ or "anci").joinpath(
            _VERSIONS_NAME
        ).read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    entries: dict[str, VersionEntry] = {}
    for version, v in raw.items():
        if not isinstance(v, dict):
            raise VersionError(f"versions.toml: [{version}] must be a table")
        url = v.get("debian-image-url")
        if not isinstance(url, str) or not url:
            raise VersionError(f"versions.toml: [{version}] missing 'debian-image-url'")
        # every other field but sops-builds is an optional string under its dashed key
        optional = {}
        for f in fields(VersionEntry)[2:-1]:
            key = f.name.replace("_", "-")
            if key in v and not isinstance(v[key], str):
                raise VersionError(f"versions.toml: [{version}] '{key}' must be a string")
            optional[f.name] = v.get(key)
        entries[version] = VersionEntry(version=version, debian_image_url=url,
                                        sops_builds=_sops_builds(version, v), **optional)
    return entries


def _sops_builds(version: str, v: dict) -> dict[str, tuple[str, str]]:
    builds = v.get("sops-builds", {})
    if not isinstance(builds, dict):
        raise VersionError(f"versions.toml: [{version}] 'sops-builds' must be a table")
    out = {}
    for plat, b in builds.items():
        if not (isinstance(b, dict) and isinstance(b.get("url"), str)
                and isinstance(b.get("sha256"), str)):
            raise VersionError(
                f"versions.toml: [{version}.sops-builds.{plat}] needs 'url' and 'sha256' strings")
        out[plat] = (b["url"], b["sha256"])
    return out


def get(entries: dict[str, VersionEntry], version: str) -> VersionEntry:
    try:
        return entries[version]
    except KeyError:
        known = ", ".join(sorted(entries)) or "(none)"
        raise VersionError(
            f"unknown anci-version '{version}' (known: {known}); "
            f"update the env's anci-version or the bundled versions.toml"
        ) from None
