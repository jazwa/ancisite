"""db.<name>: a db's dirs, extension links, and the replica its litestream
state belongs to. The data itself is `migrate`'s and `regen`'s, never apply's:
a db removed from the conf is forgotten, not deleted."""

from __future__ import annotations

import os
import posixpath
import shlex

from .. import errors, layout
from ..hashes import sha256_file
from ..conf import conf_path
from .core import Change, Resource, Step, link, pairs
from .singletons import Litestream, litestream_restart


def record_target(db) -> str:
    r = db.replication
    return f"{r.endpoint or ''} {r.bucket} {r.path}"


def extension_files(conf, db) -> list:
    files = []
    for ext in db.extensions:
        p = conf_path(conf, ext)
        if not p.is_file():
            raise errors.AppError(
                f"{ext} doesn't exist",
                why=f"that's an extension listed under [db.{db.name}]",
                try_="build/obtain it, or remove it from 'extensions'",
            )
        files.append(p)
    return files


def prune_script(ext_dir: str, keep: list[str]) -> str:
    guard = "".join(f" ! -name {shlex.quote(n)}" for n in keep)
    return (f"[ -d {shlex.quote(ext_dir)} ] && "
            f"find {shlex.quote(ext_dir)} -mindepth 1 -maxdepth 1{guard} "
            f"-exec rm -rf {{}} + || true")


def paths(conf, env_name: str, db_name: str, *, shared: bool = True) -> list[tuple[str, str]]:
    db = conf.dbs[db_name]
    data = layout.db_path(db_name)
    rows = [(data, "database"), (f"{data}-wal", "write-ahead log"), (f"{data}-shm", "WAL index")]
    if db.extensions:
        rows.append((layout.ext_dir(db_name), "extensions"))
        rows += [(f"{layout.ext_dir(db_name)}/{posixpath.basename(e)}", "extension")
                 for e in db.extensions]
    if shared:
        if db.resolve_for(env_name).replication:
            rows += Litestream().paths()
        rows.append((layout.SECRETS_FILE, "secrets"))
    return rows


def migration_setup(names: list[str]):
    wanted = {f"db.{n}" for n in names}

    def select(changes: list[Change]) -> list[Change]:
        out = []
        for c in changes:
            if c.identifier in wanted:
                out.append(c)
            elif c.identifier == "vendored" and "sqlite3" in c.fields:
                out.append(Change(c.identifier, c.action, {"sqlite3": c.fields["sqlite3"]}))
        return out
    return select


class Db(Resource):
    name = "db"
    fields = frozenset({"exts", "replica", "exists"})
    rows = {"CREATE": "create", "DELETE": "forget", "exts": "exts", "replica": "replica"}
    kept_when_removed = True
    rules = (
        ("litestream/stop", "db.*/reset-replica"),
        ("db.*/reset-replica", "litestream/start"),
        ("db.*/dirs", "*/restart"),
        ("db.*/dirs", "*/arm"),
    )

    def blobs(self, target, inputs):
        return sorted({b for ident, f in target.items() if ident.startswith("db.")
                       for b in f["exts"].values()})

    def target(self, conf, env_name):
        state = {}
        for name in sorted(conf.dbs):
            db = conf.dbs[name].resolve_for(env_name)
            state[f"db.{name}"] = {
                "exts": {p.name: layout.blob_id(sha256_file(p)) for p in extension_files(conf, db)},
                "replica": record_target(db) if db.replication else None,
            }
        return state

    def probes(self):
        d, x = layout.DB_DIR, layout.EXT_DIR
        record = os.path.basename(layout.replica_record(""))
        db_file = os.path.basename(layout.db_path(""))
        return {
            "dirs": f"ls -1 {d}",
            "exts": f'cd {x} && for f in */*; do [ -L "$f" ] && echo "$f $(readlink -e "$f")"; done',
            "replica": f'cd {d} && for d in *; do [ -f "$d/{record}" ] && echo "$d $(cat "$d/{record}")"; done',
            "files": f'cd {d} && for d in *; do [ -f "$d/{db_file}" ] && echo "$d"; done',
        }

    def observed(self, found):
        replicas = pairs(found["replica"])
        files = set(found["files"])
        exts: dict[str, dict[str, str]] = {}
        for path, blob in pairs(found["exts"]).items():
            db, _, ext = path.partition("/")
            exts.setdefault(db, {})[ext] = blob.rsplit("/", 1)[-1]
        return {
            f"db.{name}": {"exts": exts.get(name, {}), "replica": replicas.get(name),
                           "exists": name in files}
            for name in layout.split_safe(found["dirs"])[0]
        }


    def steps(self, change, ctx):
        name = change.identifier.split(".", 1)[1]
        rows = self.rows_for(change)
        if "forget" in rows:
            return []
        want = ctx.target[change.identifier]
        out = []
        if "create" in rows:
            out.append(Step(f"db.{name}/dirs", "\n".join([
                f"install -d -o {layout.ANCI_USER} -g {layout.ANCI_GROUP} -m 0750 {layout.ext_dir(name)}",
                f"install -d -o {layout.ANCI_USER} -g {layout.ANCI_USER} -m 0750 {layout.db_dir(name)}",
            ])))
        if "exts" in rows or ("create" in rows and want["exts"]):
            out.append(_exts(ctx, name))
        if "replica" in rows or ("create" in rows and want["replica"]):
            out += [_reset_replica(name, want["replica"]), *litestream_restart(ctx)]
        return out


def _exts(ctx, name: str) -> Step:
    files = extension_files(ctx.conf, ctx.conf.dbs[name].resolve_for(ctx.env_name))
    ext_dir = layout.ext_dir(name)
    lines = [link(f"{ext_dir}/{p.name}", ctx.uploads.blob(p, sha256_file(p))) for p in files]
    lines.append(prune_script(ext_dir, [p.name for p in files]))
    return Step(f"db.{name}/exts", "\n".join(lines), after=(f"db.{name}/dirs",))


def drop_litestream_state(name: str) -> str:
    return f"rm -rf {shlex.quote(layout.litestream_state_dir(name))}"


def _reset_replica(name: str, target: str | None) -> Step:
    q = shlex.quote
    record = q(layout.replica_record(name))
    lines = [drop_litestream_state(name)]
    if target is None:
        lines.append(f"rm -f {record}")
    else:
        lines.append(f"printf '%s\\n' {q(target)} > {record}")
    return Step(f"db.{name}/reset-replica", "\n".join(lines), after=(f"db.{name}/dirs",))
