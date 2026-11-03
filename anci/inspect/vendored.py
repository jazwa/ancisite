"""vendored: the pinned third-party binaries (caddy, litestream, sqlite3), on
every box.

They're shipped whether or not the conf uses them yet, so their upload (tens of
MB, from the dev machine) lands on a box's first run — its `up` — instead of
surprising a later `migrate` or `deploy` that first turns on replication or a
`host`. Each is a blob in the store, linked into /usr/local/bin. Caddy is
skipped on a local env, which never has a `host`.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from .. import bin as bin_mod, layout, paths, versions as versions_mod
from ..hashes import sha256_file
from .core import Resource, Step, link
from .singletons import CADDY_MARKER, LITESTREAM_MARKER, caddy_restart, litestream_restart


@dataclass(frozen=True)
class Binary:
    dest: str
    url: str
    name: str
    extract: str
    sha256: str | None = None
    sha512: str | None = None

    def fetch(self) -> Path:
        return bin_mod.vendor(self.url, self.sha512, self.sha256, self.name,
                              paths.bin_cache_dir(), extract=self.extract)


def binaries(conf, env_name: str) -> dict[str, Binary]:
    env = conf.envs[env_name]
    e = versions_mod.get(versions_mod.load(), env.anci_version)
    out = {}
    if e.caddy_url and not env.local:
        out["caddy"] = Binary(layout.CADDY_BIN, e.caddy_url, f"caddy-{e.caddy}", "caddy",
                              sha512=e.caddy_sha512)
    if e.litestream_url:
        out["litestream"] = Binary(layout.LITESTREAM_BIN, e.litestream_url,
                                   f"litestream-{e.litestream}", "litestream",
                                   sha256=e.litestream_sha256)
    if e.sqlite_url:
        out["sqlite3"] = Binary(layout.SQLITE_BIN, e.sqlite_url, f"sqlite3-{e.sqlite}", "sqlite3",
                                sha256=e.sqlite_sha256)
    return out


_DESTS = {"caddy": layout.CADDY_BIN, "litestream": layout.LITESTREAM_BIN, "sqlite3": layout.SQLITE_BIN}


class Vendored(Resource):
    name = "vendored"
    fields = frozenset(_DESTS)
    rows = {f: f for f in _DESTS}
    rules = (("vendored/*", "*/restart"), ("vendored/*", "*/start"))

    def blobs(self, target, inputs):
        return sorted(v for v in target.get("vendored", {}).values() if v)

    def target(self, conf, env_name):
        return {"vendored": {f: layout.blob_id(sha256_file(b.fetch()))
                             for f, b in binaries(conf, env_name).items()}}

    def probes(self):
        return {f: f"readlink -e {shlex.quote(dest)}" for f, dest in _DESTS.items()}

    def observed(self, found):
        return {"vendored": {f: os.path.basename(found[f][0]) if found[f] else None
                             for f in _DESTS}}

    def steps(self, change, ctx):
        out = []
        for f in sorted(self.rows_for(change)):
            b = binaries(ctx.conf, ctx.env_name)[f]
            local = b.fetch()
            mark = {"caddy": CADDY_MARKER, "litestream": LITESTREAM_MARKER}.get(f)
            touch = f"touch {mark}\n" if mark and f in ctx.observed else ""
            out.append(Step(f"vendored/{f}", touch + link(b.dest, ctx.uploads.blob(local, sha256_file(local)))))
            if f == "caddy" and "caddy" in ctx.target and "caddy" in ctx.observed:
                out.append(caddy_restart())
            if f == "litestream" and "litestream" in ctx.target and "litestream" in ctx.observed:
                out += litestream_restart(ctx)
        return out
