"""Databases: migrate, litestream, regen, and the dev-machine-side verbs.

Design points:
- a db is materialized per env **on demand by `migrate`** — deploy
  never touches db files, it only injects their paths as env vars
- pending migrations = glob order minus rows in the guest's `_anci_migrations`
  table; each migration is wrapped in a transaction (migrations must NOT
  contain BEGIN/COMMIT — anci owns the transaction)
- extensions ship to the content-addressed blob store and are symlinked into
  the db's ext *directory* before migrations run (one may be `.load`ed); the
  app is pointed at that directory, and an extension dropped from the conf is
  pruned from it — by the same apply pass every verb ends in (`anci/inspect/`)
- litestream is a pinned binary pushed like any artifact; one unit per env,
  one yaml listing all of that env's dbs; replicas go to an s3-compatible
  store, named by its endpoint
- secrets: you keep `.anci/secrets.<env>.env` locally (git-ignored); anci is a
  courier — it uploads the file mode 600 if present
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from .. import auth, boxlock, errors, paths, scratch, ssh, ui, units
from ..inspect import observed_state
from ..inspect import exe as exe_mod
from ..inspect.db import drop_litestream_state, extension_files, migration_setup
from ..inspect.run import run_steps
from ..render import litestream
from ..layout import ANCI_GROUP, ANCI_USER
from .. import secrets as secrets_mod
from ..conf import conf_path
from .. import layout
from ..conf import Conf, Db


BOOKKEEPING_TABLE = "_anci_migrations"

SQLITE_TIMEOUT_MS = 10_000


def sqlite3_cmd(db_file: str, *, bail: bool = False) -> str:
    return (f"sudo -n -u {ANCI_USER} sqlite3{' -bail' if bail else ''} "
            f"-cmd '.timeout {SQLITE_TIMEOUT_MS}' {db_file}")


def list_migrations(db) -> list[tuple[str, Path]]:
    d = Path(db.migrations)
    if not d.is_dir():
        raise errors.ConfError(
            f"migrations directory '{db.migrations}' doesn't exist",
            why=f"that's what [db.{db.name}] points at in anci.toml",
            try_="create it, or fix 'migrations' in anci.toml",
        )
    files = sorted(d.glob("*.sql"), key=lambda p: p.name)
    return [(p.stem, p) for p in files]


def anchored_migrations(conf, db) -> list[tuple[str, Path]]:
    d = conf_path(conf, db.migrations)
    if not d.is_dir():
        raise errors.ConfError(
            f"migrations directory '{db.migrations}' doesn't exist",
            why=f"that's what [db.{db.name}] points at in anci.toml",
            try_="create it (relative to your anci.toml), or fix 'migrations' in anci.toml",
        )
    return [(p.stem, p) for p in sorted(d.glob("*.sql"), key=lambda p: p.name)]


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sqlite(conn: ssh.Conn, db_file: str, sql: str, *, check: bool = True) -> str:
    cmd = f"{sqlite3_cmd(db_file, bail=True)} {shlex.quote(sql)}"
    return ssh.script(conn, cmd if check else f"{cmd} 2>/dev/null || true").stdout


def _bookkeeping_columns(conn, db_file) -> set[str] | None:
    out = _sqlite(conn, db_file, f"SELECT name FROM pragma_table_info('{BOOKKEEPING_TABLE}');",
                  check=False)
    return {line.strip() for line in out.splitlines() if line.strip()} or None


_USER_TABLES_SQL = (
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name NOT LIKE 'sqlite_%' "
    f"AND name != '{BOOKKEEPING_TABLE}' "
    "AND name NOT LIKE '\\_litestream\\_%' ESCAPE '\\' "
    "ORDER BY name;"
)


def _user_tables(conn, db_file) -> list[str]:
    out = _sqlite(conn, db_file, _USER_TABLES_SQL, check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _applied_checksums(conn, db_file) -> dict[str, str | None]:
    out = _sqlite(conn, db_file,
                  f"SELECT name||char(9)||coalesce(checksum,'') FROM {BOOKKEEPING_TABLE};",
                  check=False)
    applied: dict[str, str | None] = {}
    for line in out.splitlines():
        name, _, checksum = line.partition("\t")
        if name.strip():
            applied[name.strip()] = checksum.strip() or None
    return applied


def _upgrade_bookkeeping(conn, db_file) -> None:
    _sqlite(conn, db_file, f"ALTER TABLE {BOOKKEEPING_TABLE} ADD COLUMN checksum TEXT;")


def _delete_bookkeeping(conn, db_file, names: list[str]) -> None:
    quoted = ",".join(_q(n) for n in names)
    _sqlite(conn, db_file, f"DELETE FROM {BOOKKEEPING_TABLE} WHERE name IN ({quoted});")


def _set_checksums(conn, db_file, checksums: dict[str, str]) -> None:
    for name, checksum in checksums.items():
        _sqlite(conn, db_file, f"UPDATE {BOOKKEEPING_TABLE} SET checksum = {_q(checksum)} "
                               f"WHERE name = {_q(name)};")


def _migration_script(
    db_name: str, pending: list[tuple[str, Path]], ext_paths: list[str],
    checksums: dict[str, str],
) -> str:
    lines = []
    for ext in ext_paths:
        lines.append(f".load {ext}")
    lines.append("PRAGMA auto_vacuum=INCREMENTAL;")
    lines.append("PRAGMA journal_mode=WAL;")
    lines.append(
        f"CREATE TABLE IF NOT EXISTS {BOOKKEEPING_TABLE} "
        f"(name TEXT PRIMARY KEY, checksum TEXT NOT NULL, ts TEXT NOT NULL);"
    )
    for name, _ in pending:
        lines.append("BEGIN;")
        lines.append(f".read {mig_remote(db_name, name)}")
        lines.append(
            f"INSERT INTO {BOOKKEEPING_TABLE} (name, checksum, ts) "
            f"VALUES ({_q(name)}, {_q(checksums[name])}, datetime('now'));"
        )
        lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


MIG_DIR = f"/tmp/.anci-mig-{scratch.RUN_ID}"


def mig_remote(db_name: str, name: str) -> str:
    return f"{MIG_DIR}/{shq_name(db_name)}-{shq_name(name)}.sql"


def shq_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", name)


@dataclass
class _DbRun:
    name: str
    db: Db
    file: str
    migrations: list[tuple[str, Path]]
    checksums: dict[str, str]
    ext_paths: list[str] = field(default_factory=list)
    pending: list[tuple[str, Path]] = field(default_factory=list)


def migrate(conf: Conf, db_name: str | None, env_name: str, *, baseline: bool = False) -> int:
    names = [db_name] if db_name is not None else sorted(conf.dbs)
    if not names:
        raise errors.ConfError("there are no dbs to migrate",
                               try_="add a [db.<name>] table to anci.toml")
    for name in names:
        if name not in conf.dbs:
            raise errors.ConfError(
                f"db '{name}' isn't in anci.toml",
                why=f"defined dbs: {', '.join(sorted(conf.dbs)) or '(none)'}",
                try_=f"add [db.{name}] to anci.toml",
            )
    if env_name not in conf.envs:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    runs = []
    for name in names:
        migrations = anchored_migrations(conf, conf.dbs[name])
        db = conf.dbs[name].resolve_for(env_name)
        runs.append(_DbRun(name=name, db=conf.dbs[name], file=layout.db_path(name),
                           migrations=migrations,
                           checksums={n: _checksum(path) for n, path in migrations},
                           ext_paths=[f"{layout.ext_dir(name)}/{p.name}"
                                      for p in extension_files(conf, db)]))
    user = auth.authorize(conf, env_name)
    conn = ssh.connect(conf, env_name, user)
    ssh.wait_ready(conn)
    from ..inspect.run import converge

    with boxlock.hold(conn, user, f"migrate {db_name or 'all dbs'}"):
        label = db_name or "migrations"
        print(f"\n  {ui.bold(label)} {ui.dim('→')} {ui.bold(env_name)} {ui.dim(f'({conn.ip})')}\n")
        pipe = ui.Pipeline()
        converge(conf, env_name, select=migration_setup(names), pipe=pipe,
                 label=f"setting up {', '.join(names)}")
        _check_history(conn, env_name, runs, pipe, baseline=baseline)
        _apply(conn, env_name, runs, pipe)
        _verify(conn, runs, pipe)
        converge(conf, env_name, pipe=pipe)
        pipe.done()
    return 0


def _read_history(conn: ssh.Conn, runs: list[_DbRun]) -> dict[str, tuple]:
    def query(r: _DbRun, sql: str) -> str:
        return f"{sqlite3_cmd(r.file, bail=True)} {shlex.quote(sql)}"
    probes = {}
    for r in runs:
        probes[f"{r.name} cols"] = query(
            r, f"SELECT name FROM pragma_table_info('{BOOKKEEPING_TABLE}');")
        probes[f"{r.name} tables"] = query(r, _USER_TABLES_SQL)
        probes[f"{r.name} applied"] = query(
            r, f"SELECT name||char(9)||coalesce(checksum,'') FROM {BOOKKEEPING_TABLE};")
    found = ssh.facts(conn, probes)
    history = {}
    for r in runs:
        cols = {c.strip() for c in found.get(f"{r.name} cols", []) if c.strip()} or None
        tables = [t.strip() for t in found.get(f"{r.name} tables", []) if t.strip()]
        applied: dict[str, str | None] = {}
        for line in found.get(f"{r.name} applied", []):
            name, _, checksum = line.partition("\t")
            if name.strip():
                applied[name.strip()] = checksum.strip() or None
        history[r.name] = (cols, tables, applied)
    return history


def _check_history(conn: ssh.Conn, env_name: str, runs: list[_DbRun], pipe: ui.Pipeline,
                   *, baseline: bool) -> None:
    history = _read_history(conn, runs)
    for r in runs:
        cols, tables, applied = history[r.name]
        if cols is None:
            if tables:
                raise errors.EnvError(
                    f"{r.name} has a schema but no anci migration bookkeeping",
                    why=f"anci only migrates dbs it created (checksums need a history); "
                        f"found table(s): {', '.join(tables)}",
                    try_="recreate the db, or start from a fresh one",
                )
            applied = {}
        elif "checksum" not in cols:
            if not baseline:
                raise errors.EnvError(
                    f"{r.name}'s migration bookkeeping predates checksums",
                    why="anci can't verify the applied history without them",
                    try_=f"upgrade it: anci {env_name} migrate {r.name} --baseline",
                )
            _upgrade_bookkeeping(conn, r.file)
            applied = _applied_checksums(conn, r.file)

        missing = sorted(n for n in applied if n not in r.checksums)
        modified = sorted(n for n in applied
                          if n in r.checksums and applied[n] != r.checksums[n])
        if baseline and (missing or modified):
            step = pipe.step(f"baselining {r.name}'s migration history")
            if missing:
                _delete_bookkeeping(conn, r.file, missing)
            if modified:
                _set_checksums(conn, r.file, {n: r.checksums[n] for n in modified})
            step.done(f"{len(modified)} updated, {len(missing)} forgotten")
            for n in modified:
                print(f"  {ui.yellow('⚠')} accepted the current {n}")
            for n in missing:
                print(f"  {ui.yellow('⚠')} forgot {n} (its file is gone)")
            applied = _applied_checksums(conn, r.file)
        if missing and not baseline:
            raise errors.EnvError(
                f"{r.name}: {len(missing)} applied migration(s) are no longer on disk",
                why="\n".join(missing),
                try_=f"restore the file(s), or "
                     f"`anci {env_name} migrate {r.name} --baseline` to forget them",
            )
        if modified and not baseline:
            raise errors.EnvError(
                f"{r.name}: {len(modified)} migration(s) changed after they were applied",
                why="\n".join(modified),
                try_=f"add a new migration instead, or "
                     f"`anci {env_name} migrate {r.name} --baseline` to accept the files",
            )
        r.pending = [(n, path) for n, path in r.migrations if n not in applied]


def _heredoc(path: str, content: bytes) -> bytes:
    delim = "ANCI_EOF_" + hashlib.sha256(content).hexdigest()[:16]
    while delim.encode() in content:
        delim += "_"
    body = content if content.endswith(b"\n") or not content else content + b"\n"
    return f"cat > {shlex.quote(path)} <<'{delim}'\n".encode() + body + f"{delim}\n".encode()


def _bundle(runs: list[_DbRun], cleanup: str) -> bytes:
    todo = [r for r in runs if r.pending]
    out = [b"set -e", b"umask 022",
           f"rm -rf {MIG_DIR}; mkdir {MIG_DIR}".encode(),
           f"trap 'rm -rf {MIG_DIR} {cleanup} || true' EXIT".encode()]
    for r in todo:
        for name, path in r.pending:
            out.append(_heredoc(mig_remote(r.name, name), path.read_bytes()).rstrip(b"\n"))
        script = _migration_script(r.name, r.pending, r.ext_paths, r.checksums)
        out.append(_heredoc(f"{MIG_DIR}/{shq_name(r.name)}.script", script.encode()).rstrip(b"\n"))
    for r in todo:
        out.append(f"{sqlite3_cmd(r.file, bail=True)} < {MIG_DIR}/{shq_name(r.name)}.script "
                   f"|| {{ echo 'anci-failed {r.name}' >&2; exit 1; }}".encode())
    return b"\n".join(out) + b"\n"


def _apply(conn: ssh.Conn, env_name: str, runs: list[_DbRun], pipe: ui.Pipeline) -> None:
    step = pipe.step("applying migrations")
    todo = [r for r in runs if r.pending]
    if not todo:
        total = sum(len(r.migrations) for r in runs)
        step.skip(f"up to date ({total} applied)")
        return
    remote = scratch.remote("mig-bundle")
    bundle = _bundle(runs, remote)
    step.note(f"{ui.human_size(len(bundle))} in 1 file")
    local = scratch.local("mig-bundle")
    local.write_bytes(bundle)
    try:
        ssh.put(conn, str(local), remote)
    finally:
        local.unlink(missing_ok=True)
    try:
        ssh.script(conn, f"bash {remote}")
    except ssh.SSHError as e:
        if e.unreachable or e.auth or e.host_key:
            raise
        failed_db = next((r for r in todo if f"anci-failed {r.name}" in (e.stderr or "")), None)
        if failed_db is None:
            raise
        now = _applied_checksums(conn, failed_db.file)
        done = [n for n, _ in failed_db.pending if n in now]
        failed = next((n for n, _ in failed_db.pending if n not in now), "?")
        before = [r.name for r in todo[:todo.index(failed_db)]]
        why = [f"applied in this run before it: {', '.join(done)}" if done
               else f"nothing was applied to {failed_db.name} in this run; it's as it was"]
        if before:
            why.append(f"dbs fully migrated before it: {', '.join(before)}")
        raise errors.EnvError(
            f"{failed_db.name}: migration {failed} failed — its transaction was rolled back",
            why="\n".join(why),
            try_=f"fix {failed}.sql and re-run: anci {env_name} migrate",
            debug=e.stderr or str(e),
        ) from None
    count = sum(len(r.pending) for r in todo)
    if len(todo) == 1:
        detail = f"{count} applied: {', '.join(n for n, _ in todo[0].pending)}"
    else:
        detail = f"{count} applied: " + ", ".join(f"{r.name} {len(r.pending)}" for r in todo)
    step.done(detail)


def _verify(conn: ssh.Conn, runs: list[_DbRun], pipe: ui.Pipeline) -> None:
    step = pipe.step("verify")
    probes = {}
    for r in runs:
        if r.pending:
            probes[f"{r.name} quick"] = f"{sqlite3_cmd(r.file)} 'PRAGMA quick_check;'"
            probes[f"{r.name} optimize"] = f"{sqlite3_cmd(r.file)} 'PRAGMA optimize=0x10002;'"
            probes[f"{r.name} fk"] = f"{sqlite3_cmd(r.file)} 'PRAGMA foreign_key_check;'"
        probes[f"{r.name} count"] = (f"{sqlite3_cmd(r.file)} "
                                     f"'SELECT COUNT(*) FROM {BOOKKEEPING_TABLE};'")
    found = ssh.facts(conn, probes)
    for r in runs:
        if not r.pending:
            continue
        if found.get(f"{r.name} quick") != ["ok"]:
            raise errors.EnvError(
                f"{r.name}: the integrity check failed after its migrations",
                why="\n".join(found.get(f"{r.name} quick", [])[:5]) or "sqlite3 reported nothing",
                try_=f"look at the db on the box (`anci <env> info db {r.name}`)",
            )
        violations = found.get(f"{r.name} fk", [])
        if violations:
            tables = sorted({row.split("|")[0] for row in violations})
            raise errors.EnvError(
                f"{r.name}: foreign key violations in {', '.join(tables)}",
                why=f"{len(violations)} row(s) reference a parent row that doesn't exist "
                    f"(the migrations themselves were applied)",
                try_="find them with `PRAGMA foreign_key_check;` and fix them in a new migration",
            )
    total = sum(int(found.get(f"{r.name} count", ["0"])[0] or 0) for r in runs)
    step.done(f"{total} migrations on file")


def _restore_script(db_name: str, timestamp: str | None = None) -> str:
    db_file = layout.db_path(db_name)
    at = f"-timestamp {shlex.quote(timestamp)} " if timestamp else ""
    return "\n".join([
        "set -e",
        f"set -a; [ -f {layout.SECRETS_FILE} ] && . {layout.SECRETS_FILE}; set +a",
        f"systemctl stop {units.LITESTREAM_UNIT} 2>/dev/null || true",
        f"trap 'systemctl start {units.LITESTREAM_UNIT} 2>/dev/null || true' EXIT",
        f"rm -f {db_file}.restore",
        f"{layout.LITESTREAM_BIN} restore -config {layout.LITESTREAM_FILE} "
        f"{at}-o {db_file}.restore {db_file}",
        f"rm -f {db_file}-wal {db_file}-shm",
        f"mv -f {db_file}.restore {db_file}",
        drop_litestream_state(db_name),
        f"chown -R {ANCI_USER}:{ANCI_GROUP} {layout.db_dir(db_name)}",
    ]) + "\n"


def _parse_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise errors.ConfError(
            f"--timestamp '{value}' isn't a timestamp with a timezone",
            try_="use RFC3339, e.g. --timestamp 2026-10-14T15:00:00Z",
        )
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan_script(db_name: str, timestamp: str | None = None) -> str:
    db_file = layout.db_path(db_name)
    at = f"-timestamp {shlex.quote(timestamp)} " if timestamp else ""
    return "\n".join([
        "set -e",
        f"set -a; [ -f {layout.SECRETS_FILE} ] && . {layout.SECRETS_FILE}; set +a",
        f"echo live=$(( $(stat -c%s {db_file} 2>/dev/null || echo 0) "
        f"+ $(stat -c%s {db_file}-wal 2>/dev/null || echo 0) ))",
        f"{layout.LITESTREAM_BIN} restore -config {layout.LITESTREAM_FILE} "
        f"{at}-dry-run -json -o {db_file}.restore {db_file}",
    ]) + "\n"


def _parse_plan(stdout: str) -> tuple[int, int, str | None] | None:
    live_line, _, rest = stdout.partition("\n")
    if not live_line.startswith("live="):
        return None
    try:
        live = int(live_line[len("live="):])
        files = json.loads(rest)["files"]
        total = sum(int(f["size"]) for f in files)
    except (ValueError, KeyError, TypeError):
        return None
    stamps = [f["timestamp"] for f in files if isinstance(f.get("timestamp"), str)]
    return live, total, max(stamps) if stamps else None


def db_regen(conf: Conf, db_name: str | None, env_name: str, *, force: bool = False,
             timestamp: str | None = None) -> None:
    if db_name is None:
        dbs_ = sorted(conf.replicated_dbs(env_name), key=lambda d: d.name)
        if not dbs_:
            raise errors.ConfError(
                f"no db is replicated on env '{env_name}'",
                try_=f"add a [db.<name>.replication] or [db.<name>.env.{env_name}.replication] "
                     "table (bucket = '…')",
            )
    else:
        db = conf.dbs.get(db_name)
        if db is None:
            raise errors.ConfError(f"db '{db_name}' isn't in anci.toml")
        db = db.resolve_for(env_name)
        if not db.replication:
            raise errors.ConfError(
                f"db '{db_name}' has no replication configured on env '{env_name}'",
                try_=f"add a [db.{db_name}.replication] or [db.{db_name}.env.{env_name}.replication] "
                     "table (bucket = '…')",
            )
        dbs_ = [db]
    at = _parse_timestamp(timestamp) if timestamp else None
    if not force and not sys.stdin.isatty():
        raise errors.EnvError(
            "regen replaces the live db — it needs a yes from a terminal",
            why="an irreversible data decision can't be made non-interactively",
            try_="run it interactively, or pass --force to confirm it in a script",
        )

    def restore_failed(db, e: ssh.SSHError, restored: list[str] = ()) -> errors.EnvError:
        why = ("the live db is unchanged; nothing in the replica at or before "
               f"{at}, or litestream couldn't read it" if at else
               "the live db is unchanged; litestream couldn't read the replica")
        if restored:
            why += f"\nalready restored in this run: {', '.join(restored)}"
        return errors.EnvError(
            f"couldn't restore {db.name} from {litestream.replica_target(db)}",
            why=why,
            try_=(f"try a later time, or restore the latest: anci {env_name} regen {db.name}"
                  if at else
                  "check the replica's bucket/credentials (secrets file), then re-run"),
            debug=e.stderr or str(e),
        )

    user = auth.authorize(conf, env_name)
    conn = ssh.connect(conf, env_name, user)
    pipe = ui.Pipeline()

    plans = {}
    for db in dbs_:
        target = litestream.replica_target(db)
        step = pipe.step("checking replica" if len(dbs_) == 1 else f"checking {db.name}'s replica")
        try:
            plans[db.name] = _parse_plan(ssh.sudo(conn, _plan_script(db.name, at), timeout=300).stdout)
        except ssh.SSHError as e:
            if e.unreachable or e.auth or e.host_key:
                raise
            step.fail()
            raise restore_failed(db, e) from None
        step.done(target)

    def sizes(plan) -> str:
        live, replica, newest = plan
        newest_s = f", newest {newest}" if newest else ""
        return (f"the replica holds at least {ui.human_size(replica)} "
                f"(compressed{newest_s}); the live db is {ui.human_size(live)}")

    if len(dbs_) == 1:
        db, target = dbs_[0], litestream.replica_target(dbs_[0])
        details = [f"{layout.db_path(db.name)} on {env_name} is replaced with"]
        details.append(f"the replica ({target}) as of {at} — everything written after "
                       f"that is lost for good" if at else f"the last replica ({target})")
        if plans[db.name] is not None:
            details.append(sizes(plans[db.name]))
    else:
        details = [f"every replicated db on {env_name} is replaced with its "
                   + (f"replica as of {at} — everything written after that is lost for good"
                      if at else "last replica")]
        for db in dbs_:
            line = f"{db.name} ← {litestream.replica_target(db)}"
            if plans[db.name] is not None:
                line += f": {sizes(plans[db.name])}"
            details.append(line)
    names = ", ".join(db.name for db in dbs_)
    if not ui.confirm("regen REPLACES the live db:" if len(dbs_) == 1 else
                      "regen REPLACES the live dbs:", details,
                      f"regen {names}? continue?", force=force):
        print(f"  {ui.dim('aborted — nothing changed')}")
        return

    with boxlock.hold(conn, user, f"regen {db_name or 'all dbs'}"):
        step = pipe.step("stopping apps")
        on_box = observed_state(conn)
        run_steps(conn, exe_mod.stop_steps(on_box))
        step.done()
        restored: list[str] = []
        try:
            for db in dbs_:
                target = litestream.replica_target(db)
                step = pipe.step("restoring from replica" if len(dbs_) == 1
                                 else f"restoring {db.name}")
                try:
                    ssh.sudo(conn, _restore_script(db.name, at), timeout=1800)
                except ssh.SSHError as e:
                    if e.unreachable or e.auth or e.host_key:
                        raise
                    step.fail()
                    raise restore_failed(db, e, restored) from None
                step.done(target)
                restored.append(db.name)
        finally:
            step = pipe.step("restarting apps")
            run_steps(conn, exe_mod.start_steps(conf, env_name, on_box))
            step.done()
    pipe.done()


def local_schema(conf: Conf, db_name: str, env_name: str) -> Path:
    db = conf.dbs.get(db_name)
    if db is None:
        raise errors.ConfError(f"db '{db_name}' isn't in anci.toml")
    migrations = anchored_migrations(conf, db)
    scratch = paths.project_root(conf) / ".artifact" / "db" / f"{db_name}.{env_name}.sql"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.unlink(missing_ok=True)
    conn = sqlite3.connect(scratch)
    try:
        conn.execute(
            f"CREATE TABLE {BOOKKEEPING_TABLE} "
            f"(name TEXT PRIMARY KEY, checksum TEXT NOT NULL, ts TEXT NOT NULL)"
        )
        for name, path in migrations:
            sql = path.read_text()
            conn.executescript(
                f"BEGIN;\n{sql}\n;\nINSERT INTO {BOOKKEEPING_TABLE} (name, checksum, ts) "
                f"VALUES ({_q(name)}, {_q(_checksum(path))}, datetime('now'));\nCOMMIT;\n"
            )
    finally:
        conn.close()
    return scratch


SOPS_UNCHANGED = 200


def secrets_edit(conf: Conf, env_name: str) -> None:
    if env_name not in conf.envs:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    if not sys.stdin.isatty():
        raise errors.ConfError(
            "editing secrets needs a terminal ($EDITOR)",
            try_="run this in your terminal",
        )
    user = auth.authorize(conf, env_name)

    sops = secrets_mod.sops_binary(
        conf, env_name
    )
    encrypted = paths.secrets_encrypted(conf, env_name)
    encrypted.parent.mkdir(parents=True, exist_ok=True)
    recipients = secrets_mod.conf_recipients(conf, env_name)
    env = secrets_mod.sops_env()

    secrets_mod.sync_recipients(conf, env_name, sops=sops, env=env)
    age = ",".join(" ".join(k.split()[:2]) for k in recipients)
    p = subprocess.run([str(sops), "edit", "--age", age, str(encrypted)], env=env)
    if p.returncode == SOPS_UNCHANGED:
        print(f"  {ui.dim('no changes — nothing saved')}")
        return
    if p.returncode != 0:
        raise errors.InternalError(
            "sops exited with an error",
            debug=f"exit {p.returncode}",
        )
    print(f"  {ui.green('✓')} secrets saved (encrypted to {len(recipients)} keys) → {encrypted}")
    print(f"  {ui.dim(f'next: anci {env_name} reconcile  (sends the secrets to the env and restarts what uses them)')}")
