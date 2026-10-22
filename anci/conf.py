"""anci.toml parsing: strict, typed, small.

The conf is a nested document, not a reference graph.
See external-docs/robots.md for the format; validation rules live here.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import layout, paths
from . import validate
from .errors import ConfError


TOP_KEYS = {"anci-version", "user", "env", "exe", "db", "replication"}

# env names the CLI can't use: `anci <word> ...` — these words are top-level
# commands instead of env scope (see cli.py).
RESERVED_ENV_NAMES = {"init", "completion"}

# exe names that would collide with anci's own units (`<exe>.service`).
RESERVED_EXE_NAMES = {"caddy", "litestream", "anci-logtrim"}

USER_KEYS = {"ssh-pub-keys"}
ENV_KEYS = {"ip", "vm", "access-users", "port", "mode"}
# how anci treats an env's box: `full` = anci creates and runs the VM;
# `adopt` = someone else's box, set up over ssh
ENV_MODES = ("full", "adopt")
VM_KEYS = {"anci-version", "vcpus", "mem", "disk"}
# `host` belongs to one env (never a local one): top-level only when the conf has
# a single, non-local env; otherwise set per env
EXE_KEYS = {"source", "args", "env-vars", "schedule", "static", "host", "env"}
EXE_OVERRIDE_KEYS = {"source", "args", "env-vars", "schedule", "static", "host"}
DB_KEYS = {"migrations", "extensions", "replication", "env"}
DB_ENV_KEYS = {"replication"}
REPLICATION_KEYS = {"bucket", "path", "endpoint", "region", "access-key-env",
                    "secret-key-env", "sync-interval", "retention"}

# Go duration strings, litestream's own format ("1s", "5m", "24h", "720h").
_DURATION_RE = re.compile(r"^(\d+(\.\d+)?(ns|us|µs|ms|s|m|h))+$")
# valid environment variable names (rendered into the yaml as ${NAME})
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NAME_RE = layout.NAME_RE   # shared with the box-inventory filter (layout.is_safe_name)
# user names become Linux accounts (useradd's portable rule)
_USER_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass
class User:
    name: str
    ssh_pub_keys: list[str]


@dataclass
class Env:
    """A deployment target and its box. `vm` in the conf is this env's machine
    config, inlined (`vm = { anci-version = ..., vcpus = ... }`); there is no
    separate machine entity — one env is one box."""

    name: str
    ip: str
    access_users: list[str]
    anci_version: str
    port: int = 22
    vcpus: int = 4
    mem: str = "4gb"
    disk: str = "20gb"
    # `full` (default): anci manages the VM — provisions/resizes/stops it.
    # `adopt`: someone else's box; anci bootstraps it over ssh, no hypervisor.
    mode: str = "full"

    @property
    def adopted(self) -> bool:
        """True for a box anci adopted (`mode = 'adopt'`) rather than created."""
        return self.mode == "adopt"

    @property
    def local(self) -> bool:
        """True for a VM on this dev machine (`ip = '127.0.0.1'`)."""
        return self.ip == "127.0.0.1"


@dataclass
class ExeOverride:
    """Per-env override block: [exe.<name>.env.<env>]."""

    env: str
    source: str | None = None
    args: list[str] | None = None
    env_vars: dict[str, str] | None = None
    schedule: str | None = None
    static: str | None = None
    host: str | None = None


@dataclass
class Exe:
    name: str
    source: str
    args: list[str] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    schedule: str | None = None   # OnCalendar expression → scheduled job
    static: str | None = None     # directory shipped with each deploy (ANCI_STATIC_DIR)
    host: str | None = None       # public domain (single non-local env only; else per env)
    env_overrides: dict[str, ExeOverride] = field(default_factory=dict)

    def resolve_for(self, env_name: str) -> ResolvedExe:
        """Merge the base exe with its per-env override for `env_name`.

        Merge rules: map keys (env-vars) deep-merge, scalars replace.
        An env with no override just gets the base config.
        """
        ov = self.env_overrides.get(env_name)
        return ResolvedExe(
            name=self.name,
            env=env_name,
            source=ov.source if ov and ov.source is not None else self.source,
            target=layout.default_target(self.name),
            args=ov.args if ov and ov.args is not None else list(self.args),
            env_vars={**self.env_vars, **((ov.env_vars if ov else None) or {})},
            schedule=ov.schedule if ov and ov.schedule is not None else self.schedule,
            host=ov.host if ov and ov.host is not None else self.host,
            static=ov.static if ov and ov.static is not None else self.static,
        )


@dataclass
class ResolvedExe:
    """An exe fully resolved for one env (no remaining overrides)."""

    name: str
    env: str
    source: str
    target: str
    args: list[str]
    env_vars: dict[str, str]
    schedule: str | None = None
    host: str | None = None
    static: str | None = None


@dataclass
class Replication:
    """A db's s3-compatible replica (litestream). s3-compatible only — AWS S3,
    or anything else via its `endpoint` (R2, MinIO, …; litestream forces
    path-style addressing when an endpoint is set). `region` pins the signing
    region (AWS auto-discovers it when unset; S3-compatible stores may want an
    explicit value). `path` defaults to the db name (the key prefix inside the
    bucket); `*_env` name the env vars in the secrets file that carry the
    credentials (default: the AWS chain)."""

    bucket: str
    path: str | None = None              # key prefix; resolve_for defaults it to <env>/<db>
    endpoint: str | None = None          # non-AWS S3-compatible stores
    region: str | None = None            # signing/region; AWS auto-discovers when unset
    access_key_env: str | None = None    # env var NAME in the secrets file
    secret_key_env: str | None = None
    sync_interval: str | None = None     # Go duration, e.g. '1s'
    retention: str | None = None         # Go duration, e.g. '720h'


@dataclass
class Db:
    """A sqlite database, materialized per (vm, env) on demand.

    Dbs are never deployed — `migrate` creates/updates them.
    Every exe on an env receives ANCI_DB_<NAME>_PATH for every db.
    """

    name: str
    migrations: str                      # directory; order = filename sort
    extensions: list[str] = field(default_factory=list)
    replication: Replication | None = None   # the base; read it through resolve_for
    # [db.<name>.env.<env>]: that env's replication (None = off there)
    env_replication: dict[str, Replication | None] = field(default_factory=dict)

    def resolve_for(self, env_name: str) -> "Db":
        """The db as `env_name` sees it: its per-env replication, else the base —
        with the replica under `<env>/<db>` unless `path` is set, so two envs that
        replicate the same db never share a replica."""
        rep = self.env_replication.get(env_name, self.replication)
        if rep is not None and rep.path is None:
            rep = dataclasses.replace(rep, path=f"{env_name}/{self.name}")
        return dataclasses.replace(self, replication=rep, env_replication={})


@dataclass
class Conf:
    users: dict[str, User]
    envs: dict[str, Env]
    exes: dict[str, Exe]
    dbs: dict[str, Db] = field(default_factory=dict)
    path: str | None = None  # where this conf was loaded from (project-root anchor)

    def replicated_dbs(self, env_name: str) -> list[Db]:
        """The dbs `env_name` replicates, resolved for it (conf order)."""
        return [d for d in (db.resolve_for(env_name) for db in self.dbs.values())
                if d.replication]

    def users_for_env(self, env_name: str) -> dict[str, list[str]]:
        """conf user name -> their ssh keys, for users with access to this env
        (one guest account + `authorized_keys` per user, per box)."""
        env = self.envs[env_name]
        return {
            name: sorted(self.users[name].ssh_pub_keys)
            for name in sorted(set(env.access_users))
        }


# ---------------------------------------------------------------------------
# parsing helpers


def _only_keys(d: dict, allowed: set[str], ctx: str) -> None:
    unknown = set(d) - allowed
    if unknown:
        raise ConfError(
            f"{ctx}: unknown key(s) {sorted(unknown)}; allowed: {sorted(allowed)}"
        )


def _str(d: dict, key: str, ctx: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ConfError(f"{ctx}: '{key}' must be a non-empty string")
    return v


def _str_list(d: dict, key: str, ctx: str) -> list[str]:
    v = d.get(key)
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
        raise ConfError(f"{ctx}: '{key}' must be a non-empty string or list of strings")
    return v


def _opt_str(d: dict, key: str, ctx: str, what: str = "a non-empty string") -> str | None:
    v = d.get(key)
    if v is not None and (not isinstance(v, str) or not v):
        raise ConfError(f"{ctx}: '{key}' must be {what}")
    return v


def _str_array(d: dict, key: str, ctx: str) -> list[str] | None:
    v = d.get(key)
    if v is not None and (not isinstance(v, list) or not all(isinstance(a, str) for a in v)):
        raise ConfError(f"{ctx}: '{key}' must be an array of strings")
    return v


def _str_map(d: dict, key: str, ctx: str, hint: str = "") -> dict[str, str] | None:
    v = d.get(key)
    if v is not None and (not isinstance(v, dict) or not all(
            isinstance(k, str) and isinstance(x, str) for k, x in v.items())):
        raise ConfError(f"{ctx}: '{key}' must be a map of string to string{hint}")
    return v


_SCHEDULE = "a non-empty OnCalendar expression"
_STATIC = "a directory path (relative to anci.toml)"


def _int(d: dict, key: str, ctx: str) -> int:
    v = d.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ConfError(f"{ctx}: '{key}' must be an integer")
    return v


# ---------------------------------------------------------------------------
# size parsing


def _size_mib(value: str | int, what: str, int_unit: str) -> int:
    """'512mb' / '4gb' / a bare integer (in `int_unit`: 'MiB' or 'GiB') -> MiB.
    The one size parser, so every caller agrees on what a number means."""
    per_int = {"MiB": 1, "GiB": 1024}[int_unit]
    if isinstance(value, int):
        if value <= 0:
            raise ConfError(f"{what} must be positive, got {value}")
        return value * per_int
    v = value.strip().lower()
    for suffix, mult in (("gb", 1024), ("mb", 1)):
        if v.endswith(suffix) and v[: -len(suffix)].isdigit():
            n = int(v[: -len(suffix)])
            if n <= 0:
                raise ConfError(f"{what} must be positive, got {value!r}")
            return n * mult
    raise ConfError(f"invalid {what} {value!r}: expected like '512mb', '4gb', or integer {int_unit}")


def mem_to_mib(mem: str | int) -> int:
    """'512mb'/'4gb'/int(MiB) -> MiB.

    Used for `virt-install --memory`, whose default unit is **MiB** (not KiB —
    passing KiB here silently requests 1024x the RAM; regression-tested).
    """
    return _size_mib(mem, "mem", "MiB")


def mem_to_kib(mem: str | int) -> int:
    """'512mb'/'4gb'/int(MiB) -> KiB, for libvirt's setmem/setmaxmem. A bare
    integer is MiB here too — the same conf value must mean the same size."""
    return mem_to_mib(mem) * 1024


def disk_to_qemu(disk: str | int) -> str:
    """'20gb'/'512mb'/int(GiB) -> qemu-img size string like '20G'."""
    mib = _size_mib(disk, "disk", "GiB")
    return f"{mib // 1024}G" if mib % 1024 == 0 else f"{mib}M"


# ---------------------------------------------------------------------------
# port allocation


def port_var(exe_name: str) -> str:
    """`api` → `ANCI_PORT_API`: an exe's port, as every exe on the env sees it."""
    return "ANCI_PORT_" + re.sub(r"[^A-Za-z0-9]", "_", exe_name).upper()


def allocate_ports(conf: Conf, base: int = 8000, exe_stride: int = 10) -> dict[tuple[str, str], int]:
    """Deterministically assign one localhost port per (exe, env).

    Scheduled jobs (`exe.schedule`) serve no HTTP, so they get no port and
    consume no slot — adding one never shifts a serving exe's port.
    Each env is its own box, so ports need only be unique within an env; exes
    per env are ordered by name. port = base + exe_index*exe_stride.
    Deterministic so the same conf always renders the same units.
    """
    ports: dict[tuple[str, str], int] = {}
    for env in sorted(conf.envs.values(), key=lambda e: e.name):
        exe_names = sorted(
            n for n, e in conf.exes.items()
            if e.resolve_for(env.name).schedule is None
        )
        if len(exe_names) > exe_stride:
            raise ConfError(
                f"env '{env.name}': too many exes for port allocation (max {exe_stride})"
            )
        for xi, exe_name in enumerate(exe_names):
            ports[(exe_name, env.name)] = base + exe_stride * xi
    return ports


# ---------------------------------------------------------------------------
# loaders


def _parse_user(name: str, raw: dict) -> User:
    ctx = f"[user.{name}]"
    if not isinstance(raw, dict):
        raise ConfError(f"{ctx} must be a table")
    _only_keys(raw, USER_KEYS, ctx)
    return User(name=name, ssh_pub_keys=_str_list(raw, "ssh-pub-keys", ctx))


def _parse_env(name: str, raw: dict, default_version: str | None) -> Env:
    ctx = f"[env.{name}]"
    if not isinstance(raw, dict):
        raise ConfError(f"{ctx} must be a table")
    _only_keys(raw, ENV_KEYS, ctx)

    mode = raw.get("mode", "full")
    if mode not in ENV_MODES:
        raise ConfError(
            f"{ctx}: 'mode' must be 'full' (anci runs the VM) or 'adopt' (an existing box)")
    adopted = mode == "adopt"

    vm_ctx = f"[env.{name}.vm]"
    vm_raw = raw.get("vm")
    if vm_raw is None:
        if not adopted:
            raise ConfError(
                f"{ctx}: 'vm' is required for an anci-managed env, e.g. "
                f"vm = {{ vcpus = 2, mem = '2gb', disk = '20gb' }} "
                f"(or set mode = 'adopt' for a box someone else manages)"
            )
        vm_raw = {}
    if not isinstance(vm_raw, dict):
        raise ConfError(f"{ctx}: 'vm' must be a table of machine config")
    _only_keys(vm_raw, VM_KEYS, vm_ctx)

    # anci-version: per-env vm wins, else the top-level default
    anci_version = vm_raw.get("anci-version", default_version)
    if not isinstance(anci_version, str) or not anci_version:
        raise ConfError(
            f"{vm_ctx}: 'anci-version' is required "
            f"(set it at the top level or here)"
        )

    explicit = frozenset(k for k in ("vcpus", "mem", "disk") if k in vm_raw)
    if adopted and explicit:
        raise ConfError(
            f"{vm_ctx}: mode = 'adopt' — vcpus/mem/disk don't apply (remove them)"
        )

    port = raw.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfError(f"{ctx}: 'port' must be an integer in 1..65535")
    common = dict(name=name, ip=_str(raw, "ip", ctx),
                  access_users=_str_list(raw, "access-users", ctx),
                  anci_version=anci_version, port=port, mode=mode)
    if adopted:
        return Env(**common)   # someone else sized the box: Env's defaults stand

    missing = [k for k in ("vcpus", "mem", "disk") if k not in vm_raw]
    if missing:
        raise ConfError(
            f"{vm_ctx}: '{missing[0]}' is required for an anci-managed env "
            f"(vcpus/mem/disk size the box)"
        )
    vcpus = _int(vm_raw, "vcpus", vm_ctx)
    if vcpus <= 0:
        raise ConfError(f"{vm_ctx}: 'vcpus' must be positive")
    mem, disk = vm_raw["mem"], vm_raw["disk"]
    if isinstance(mem, bool) or not isinstance(mem, (str, int)):
        raise ConfError(f"{vm_ctx}: 'mem' must be a string like '4gb' or an integer MiB")
    if isinstance(disk, bool) or not isinstance(disk, (str, int)):
        raise ConfError(f"{vm_ctx}: 'disk' must be a string like '20gb' or an integer GiB")
    mem_to_mib(mem)
    disk_to_qemu(disk)
    return Env(**common, vcpus=vcpus, mem=mem, disk=disk)


def _parse_exe_override(exe_name: str, env_name: str, raw: dict) -> ExeOverride:
    ctx = f"[exe.{exe_name}.env.{env_name}]"
    _only_keys(raw, EXE_OVERRIDE_KEYS, ctx)
    return ExeOverride(
        env=env_name,
        source=raw.get("source"),
        args=_str_array(raw, "args", ctx),
        env_vars=_str_map(raw, "env-vars", ctx),
        schedule=_opt_str(raw, "schedule", ctx, _SCHEDULE),
        static=_opt_str(raw, "static", ctx, _STATIC),
        host=_opt_str(raw, "host", ctx, "a non-empty domain name"),
    )


def _parse_exe(name: str, raw: dict, envs: dict[str, Env]) -> Exe:
    ctx = f"[exe.{name}]"
    if not isinstance(raw, dict):
        raise ConfError(f"{ctx} must be a table")
    _only_keys(raw, EXE_KEYS, ctx)
    if "host" in raw and (len(envs) != 1 or next(iter(envs.values())).local):
        env_name = next((e for e in envs if not envs[e].local), "prod")
        raise ConfError(
            f"{ctx}: 'host' is set per env here",
            why="a public domain belongs to one env; top-level 'host' is only for a conf "
                "with a single, non-local env",
            try_=f"move it to the env that serves it, e.g. [exe.{name}.env.{env_name}] "
                 f"host = {raw['host']!r}",
        )
    env_vars = _str_map(raw, "env-vars", ctx, hint=(
        f" (note: app env vars use 'env-vars'; 'env' is reserved for "
        f"per-env subtables like [exe.{name}.env.dev])"))

    env_table = raw.get("env", {})
    if not isinstance(env_table, dict):
        raise ConfError(
            f"{ctx}: 'env' must be a table of per-env overrides "
            f"(use [exe.{name}.env.<envname>] sections; app env vars go in 'env-vars')"
        )
    overrides: dict[str, ExeOverride] = {}
    for env_name, ov_raw in env_table.items():
        if not isinstance(ov_raw, dict):
            raise ConfError(
                f"[exe.{name}.env.{env_name}] must be a table "
                f"(app env vars go in 'env-vars', e.g. env-vars = {{ KEY = 'value' }})"
            )
        # validate against the conf's envs *before* normalizing below, or a
        # typo'd env would be silently accepted as an empty override
        if env_name not in envs:
            raise ConfError(f"[exe.{name}.env.{env_name}]: env '{env_name}' is not defined")
        overrides[env_name] = _parse_exe_override(name, env_name, ov_raw)

    return Exe(
        name=name,
        source=_str(raw, "source", ctx),
        args=_str_array(raw, "args", ctx) or [],
        env_vars=env_vars or {},
        schedule=_opt_str(raw, "schedule", ctx, _SCHEDULE),
        static=_opt_str(raw, "static", ctx, _STATIC),
        host=_opt_str(raw, "host", ctx, "a non-empty domain name"),
        env_overrides=overrides,
    )


def _parse_replication(raw: dict, defaults: dict, ctx: str) -> Replication:
    """A merged replication spec: `[replication]` defaults overlaid by the
    per-db `[db.<name>.replication]` table (per-db keys win). Validation runs
    on the merged result, so a partial shared table is legal."""
    _only_keys(raw, REPLICATION_KEYS, ctx)
    merged = {**defaults, **raw}
    bucket = merged.get("bucket")
    if bucket is None:
        raise ConfError(
            f"{ctx}: 'bucket' is required "
            f"(set it in [replication] or in {ctx})"
        )
    _opt_str(merged, "bucket", ctx)
    if any(ch.isspace() or ch == "/" for ch in bucket):
        raise ConfError(f"{ctx}: bucket '{bucket}' isn't a valid bucket name "
                        f"(no spaces or '/'; a key prefix goes in 'path')")
    endpoint = merged.get("endpoint")
    access_key_env = merged.get("access-key-env")
    secret_key_env = merged.get("secret-key-env")
    if (access_key_env is None) != (secret_key_env is None):
        raise ConfError(
            f"{ctx}: 'access-key-env' and 'secret-key-env' go together "
            f"(the store needs both, or neither)"
        )
    for key in ("access-key-env", "secret-key-env", "path", "endpoint", "region"):
        _opt_str(merged, key, ctx)
    if endpoint is not None and not endpoint.startswith(("https://", "http://")):
        raise ConfError(f"{ctx}: 'endpoint' must be a URL starting with https:// "
                        f"(e.g. 'https://<account-id>.r2.cloudflarestorage.com')")
    path = merged.get("path")
    if path is not None and (path.startswith("/") or path.endswith("/")
                             or any(seg in ("", ".", "..") for seg in path.split("/"))):
        raise ConfError(f"{ctx}: 'path' must be a relative key prefix like 'prod/main' "
                        f"(no leading or trailing '/', no '.' or '..' segments)")
    for name in (access_key_env, secret_key_env):
        if name is not None and not _ENV_NAME_RE.match(name):
            raise ConfError(
                f"{ctx}: '{name}' isn't a valid env var name "
                f"(letters, digits, underscores; can't start with a digit)"
            )
    for field_name in ("sync-interval", "retention"):
        v = merged.get(field_name)
        if v is not None and (not isinstance(v, str) or not _DURATION_RE.match(v)):
            raise ConfError(
                f"{ctx}: '{field_name}' must be a Go duration "
                f"(e.g. '1s', '5m', '24h', '720h')"
            )
    return Replication(
        bucket=bucket,
        path=merged.get("path"),
        endpoint=endpoint,
        region=merged.get("region"),
        access_key_env=access_key_env,
        secret_key_env=secret_key_env,
        sync_interval=merged.get("sync-interval"),
        retention=merged.get("retention"),
    )


def _parse_db(name: str, raw: dict, replication_defaults: dict, envs: dict[str, Env]) -> Db:
    ctx = f"[db.{name}]"
    if not isinstance(raw, dict):
        raise ConfError(f"{ctx} must be a table")
    _only_keys(raw, DB_KEYS, ctx)
    extensions = raw.get("extensions", [])
    if not isinstance(extensions, list) or not all(
        isinstance(x, str) and x for x in extensions
    ):
        raise ConfError(f"{ctx}: 'extensions' must be a list of file paths")
    replication = raw.get("replication")
    if replication is not None and not isinstance(replication, dict):
        raise ConfError(f"{ctx}: 'replication' must be a table")
    parsed_replication = (
        _parse_replication(replication, replication_defaults, f"[db.{name}.replication]")
        if replication is not None
        else None
    )
    env_replication: dict[str, Replication | None] = {}
    env_tables = raw.get("env", {})
    if not isinstance(env_tables, dict):
        raise ConfError(f"{ctx}: 'env' must hold per-env tables like [db.{name}.env.prod]")
    for env_name, ov in env_tables.items():
        ov_ctx = f"[db.{name}.env.{env_name}]"
        if not isinstance(ov, dict):
            raise ConfError(f"{ov_ctx} must be a table")
        if env_name not in envs:
            raise ConfError(f"{ov_ctx}: env '{env_name}' is not defined")
        _only_keys(ov, DB_ENV_KEYS, ov_ctx)
        if "replication" not in ov:
            continue
        rep = ov["replication"]
        if rep is False:
            env_replication[env_name] = None
        elif isinstance(rep, dict):
            # overlays the base table (and [replication]); per-env keys win
            env_replication[env_name] = _parse_replication(
                {**(replication or {}), **rep}, replication_defaults,
                f"[db.{name}.env.{env_name}.replication]")
        else:
            raise ConfError(f"{ov_ctx}: 'replication' must be a table, or false to turn it off")
    return Db(
        name=name,
        migrations=_str(raw, "migrations", ctx),
        extensions=extensions,
        replication=parsed_replication,
        env_replication=env_replication,
    )


def conf_path(conf, rel: str) -> Path:
    """Resolve a conf-relative path (source, static, migrations, extensions) anchored on
    the conf's directory — not the CWD (the project boundary, not the chair)."""
    p = Path(rel)
    return p if p.is_absolute() else (paths.project_root(conf) / p).resolve()


def find_conf(start: str | Path | None = None) -> str:
    """Git-style: walk up from the CWD (or `start`) until anci.toml is found.

    All project state (pins, secrets courier, migrations) is anchored on the
    conf's directory (the project boundary), so finding the conf means finding the
    project root — running anci from a subdirectory of your project just works.
    """
    d = Path(start or Path.cwd()).resolve()
    for d in [d, *d.parents]:
        candidate = d / "anci.toml"
        if candidate.is_file():
            return str(candidate)
    raise ConfError(
        "no anci.toml found here or in any parent directory "
        "(run anci from inside your project)"
    )


def load(path: str | Path | None = None) -> Conf:
    """Load and fully validate an anci.toml. Raises ConfError on any problem.

    `path=None` triggers the git-style upward search (see find_conf).
    """
    if path is None:
        path = find_conf()
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfError(f"conf not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfError(f"{path}: invalid TOML: {e}") from None

    _only_keys(raw, TOP_KEYS, str(path))

    default_version = raw.get("anci-version")
    if default_version is not None and (
        not isinstance(default_version, str) or not default_version
    ):
        raise ConfError("'anci-version' must be a non-empty string")

    replication_defaults = raw.get("replication", {})
    if not isinstance(replication_defaults, dict):
        raise ConfError("[replication] must be a table")
    _only_keys(replication_defaults, REPLICATION_KEYS, "[replication]")
    envs = {n: _parse_env(n, v, default_version) for n, v in raw.get("env", {}).items()}
    conf = Conf(
        users={n: _parse_user(n, v) for n, v in raw.get("user", {}).items()},
        envs=envs,
        exes={n: _parse_exe(n, v, envs) for n, v in raw.get("exe", {}).items()},
        dbs={n: _parse_db(n, v, replication_defaults, envs) for n, v in raw.get("db", {}).items()},
        path=str(path),
    )

    validate.validate_conf(conf)
    _validate(conf)
    return conf


def _validate(conf: Conf) -> None:
    if not conf.envs:
        raise ConfError("conf must define at least one [env.<name>]")

    # names first: everything below (and every command) builds paths from them
    for kind, names in (("user", conf.users), ("env", conf.envs),
                        ("exe", conf.exes), ("db", conf.dbs)):
        rule = _USER_NAME_RE if kind == "user" else _NAME_RE
        for name in names:
            if not rule.match(name):
                raise ConfError(
                    f"[{kind}.{name}]: invalid {kind} name",
                    why=_NAME_WHY[kind],
                    try_=("use lowercase letters, digits, '_' or '-' (max 32, "
                          "not starting with a digit or '-')" if kind == "user" else
                          "start with a letter or digit; then letters, digits, '.', '_' or '-'"),
                )
    port_vars: dict[str, str] = {}
    for name in sorted(conf.exes):
        other = port_vars.setdefault(port_var(name), name)
        if other != name:
            raise ConfError(
                f"[exe.{name}]: its port variable {port_var(name)} is also [exe.{other}]'s",
                try_="rename one: names may not differ only in case or in '.', '_', '-'",
            )
    for exe in conf.exes.values():
        if exe.name in RESERVED_EXE_NAMES:
            raise ConfError(
                f"[exe.{exe.name}]: '{exe.name}' is reserved "
                f"(it names one of anci's own systemd units)"
            )

    for env in conf.envs.values():
        if env.adopted and env.ip == "127.0.0.1":
            raise ConfError(
                f"[env.{env.name}]: mode = 'adopt' needs the box's address, not 127.0.0.1",
                why="anci would bootstrap your own machine (accounts, sudoers) as the box",
                try_="set ip to the box's address, or drop 'mode' for a VM on this dev machine",
            )
        if env.name in RESERVED_ENV_NAMES:
            raise ConfError(
                f"[env.{env.name}]: '{env.name}' is a reserved word "
                f"(it's an anci command, not an env)"
            )
        hosts: dict[str, str] = {}
        for exe in conf.exes.values():
            resolved = exe.resolve_for(env.name)
            if resolved.host and env.local:
                raise ConfError(
                    f"[exe.{exe.name}.env.{env.name}]: a local env can't have a 'host'",
                    why="anci serves every service on a local env at "
                        "http://<project>.localhost:<port>, through the localhost forward",
                    try_="set 'host' only on remote envs",
                )
            if resolved.schedule is not None or not resolved.host:
                continue
            if resolved.host in hosts:
                raise ConfError(
                    f"[exe.{exe.name}]: host '{resolved.host}' is already used by "
                    f"'[exe.{hosts[resolved.host]}]' on env '{env.name}'",
                    try_="give each exe its own domain",
                )
            hosts[resolved.host] = exe.name

    # every replica its own place: two writers on one replica corrupt it, and a
    # restore could bring back another db's (or env's) data
    replicas: dict[tuple, tuple[str, str]] = {}
    for env in conf.envs.values():
        for db in conf.replicated_dbs(env.name):
            r = db.replication
            where = (r.endpoint or "", r.bucket, r.path)
            other = replicas.setdefault(where, (db.name, env.name))
            if other != (db.name, env.name):
                raise ConfError(
                    f"[db.{db.name}] on env '{env.name}' and [db.{other[0]}] on env "
                    f"'{other[1]}' replicate to the same place ({r.bucket}/{r.path})",
                    try_="give one its own 'path', or drop 'path' from [replication] "
                         "(each db then gets <env>/<db>)",
                )


_NAME_WHY = {
    "user": "a user name becomes a Linux account on the box",
    "env": "an env name becomes paths and commands on the box",
    "exe": "an exe name becomes its systemd unit and its on-guest path "
           "(/var/lib/anci-<exe>/<exe>)",
    "db": "a db name becomes paths and commands on the box",
}
