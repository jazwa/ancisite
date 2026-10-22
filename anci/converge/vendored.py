"""The vendored binaries (sqlite3, litestream, caddy), on every box.

They're shipped whether or not the conf uses them yet, so their upload (tens of
MB, from the dev machine) lands on a box's first converge — its `up` — instead
of surprising a later `migrate` or `deploy` that first turns on replication or
a `host`. Vendoring them is also what lets anci set up a box without reaching
for its package manager: `sqlite3` (the db engine `migrate` drives) ships here
instead of an `apt install`. The `litestream` and `caddy` reconcilers own their
units and configs, and only start the daemon when it's used. Caddy is skipped
on a local env, which never has a `host`.
"""

from __future__ import annotations

from .. import layout, versions as versions_mod
from ..versions import VersionEntry
from .context import DesiredState, ConvergeContext, Download


def sqlite_download(entry: VersionEntry) -> Download | None:
    """The pinned sqlite3 binary for `entry`, or None when the version pins
    none. `migrate` reuses this to self-heal a box that's missing it."""
    if not entry.sqlite_url:
        return None
    return Download(
        url=entry.sqlite_url, sha256=entry.sqlite_sha256,
        name=f"sqlite3-{entry.sqlite}", dest=layout.SQLITE_BIN, extract="sqlite3",
    )


def contribute(ctx: ConvergeContext) -> DesiredState:
    env = ctx.conf.envs[ctx.env_name]
    entry = versions_mod.get(versions_mod.load(), env.anci_version)
    downloads = []
    # a version without a URL is only an error once the tool is used (the
    # litestream/caddy reconcilers raise it; migrate raises for sqlite3)
    if entry.litestream_url:
        downloads.append(Download(
            url=entry.litestream_url, sha256=entry.litestream_sha256,
            name=f"litestream-{entry.litestream}", dest=layout.LITESTREAM_BIN,
            extract="litestream",
        ))
    sqlite = sqlite_download(entry)
    if sqlite:
        downloads.append(sqlite)
    if entry.caddy_url and not env.local:
        downloads.append(Download(
            url=entry.caddy_url, sha512=entry.caddy_sha512,
            name=f"caddy-{entry.caddy}", dest=layout.CADDY_BIN, extract="caddy",
        ))
    return DesiredState(name="vendored", downloads=downloads)
