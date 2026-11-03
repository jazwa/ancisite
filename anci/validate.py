"""Cross-reference validation for `anci.toml`.

Parsing (`conf.py`) enforces *shape* — required keys, types, unknown-key
rejection. This module owns the references *between* parts of the conf, and
between the conf and the local project, so a conf that points at a user,
version, box, or secret that doesn't exist fails fast, before any command does
work.

It also holds the conf's rules: a few general ones (no control characters,
one name rule, …) instead of one rule per field.

Everything here is offline — no ssh, no SDK, no store.

- `check_text(raw)`: rule 1, on the raw TOML. Called by `conf.load()` first.
- `validate_conf(conf)`: the other rules, then pure conf cross-references.
  Called by `conf.load()`, so every env command gets it; `init` never loads a
  conf.
- `validate_env(conf, env_name)`: the conf plus the local secrets for one env.
  Called by the CLI before a verb does work.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import TYPE_CHECKING

from .errors import ConfError
from . import paths
from . import versions as versions_mod

if TYPE_CHECKING:
    from .conf import Conf


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RESERVED_NAMES = {"root", "anci", "caddy", "litestream", "anci-logtrim",
                  "ssh", "sshd", "networking", "systemd-networkd", "init", "completion"}
ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VM_LIMITS = {"vcpus": (1, 255), "mem": (512, None), "disk": (4096, None)}
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def check_text(raw: dict) -> None:
    def walk(value, where: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(k, where)
                walk(v, f"{where}.{k}" if where else k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{where}[{i}]")
        elif isinstance(value, str) and (m := _CONTROL_RE.search(value)):
            raise ConfError(
                f"{where or 'anci.toml'}: {value!r} contains a control character ({m[0]!r})",
                why="values go into unit files, ssh keys and the Caddyfile, where a newline adds a line",
                try_="remove it",
            )
    walk(raw, "")


def _key_problem(key: str) -> str | None:
    parts = key.split()
    if len(parts) < 2:
        return "expected '<type> <base64> [comment]'"
    kind, b64 = parts[0], parts[1]
    try:
        blob = base64.b64decode(b64, validate=True)
    except binascii.Error:
        return "its key part isn't base64"
    n = int.from_bytes(blob[:4], "big")
    if blob[4:4 + n] != kind.encode():
        return f"it isn't a {kind} key (options before the key aren't allowed)"
    return None


def _check_rules(conf: "Conf") -> None:
    from .conf import disk_to_mib, mem_to_mib

    for kind, names in (("user", conf.users), ("env", conf.envs),
                        ("exe", conf.exes), ("db", conf.dbs)):
        for name in names:
            if not NAME_RE.match(name):
                raise ConfError(
                    f"[{kind}.{name}]: invalid {kind} name",
                    why="names become accounts, units, paths and env vars on the box",
                    try_="lowercase letters, digits and '-', starting with a letter, at most 32",
                )
            if name in RESERVED_NAMES:
                raise ConfError(f"[{kind}.{name}]: '{name}' is reserved",
                                why=f"reserved names: {', '.join(sorted(RESERVED_NAMES))}")

    for exe in conf.exes.values():
        for where, env_vars, host in [(f"[exe.{exe.name}]", exe.env_vars, exe.host),
                                      *((f"[exe.{exe.name}.env.{o.env}]", o.env_vars or {}, o.host)
                                        for o in exe.env_overrides.values())]:
            for key in env_vars:
                if not ENV_VAR_RE.match(key):
                    raise ConfError(f"{where}: env-var '{key}' isn't a valid name",
                                    try_="letters, digits and '_', not starting with a digit")
                if key.upper().startswith("ANCI_"):
                    raise ConfError(f"{where}: env-var '{key}' is anci's",
                                    why="anci sets the ANCI_* vars itself",
                                    try_="give it a name that doesn't start with ANCI_")
            if host is not None and not _HOST_RE.match(host):
                raise ConfError(f"{where}: host '{host}' isn't a hostname",
                                try_="lowercase letters, digits, '.' and '-', like 'app.example.com'")

    holders: dict[tuple[str, str], str] = {}
    for user in conf.users.values():
        for i, key in enumerate(user.ssh_pub_keys):
            problem = _key_problem(key)
            if problem:
                raise ConfError(f"[user.{user.name}]: ssh-pub-keys[{i}] isn't a public key",
                                why=problem,
                                try_="paste the whole line of your .pub file, e.g. ~/.ssh/id_ed25519.pub")
            other = holders.setdefault(tuple(key.split()[:2]), user.name)
            if other != user.name:
                raise ConfError(f"[user.{user.name}]: its key ssh-pub-keys[{i}] is also [user.{other}]'s",
                                why="anci tells deployers apart by their keys",
                                try_="give each user their own key")

    for env in conf.envs.values():
        sizes = {"vcpus": env.vcpus, "mem": mem_to_mib(env.mem), "disk": disk_to_mib(env.disk)}
        for field, (low, high) in VM_LIMITS.items():
            unit = "" if field == "vcpus" else " MiB"
            if sizes[field] < low or (high is not None and sizes[field] > high):
                raise ConfError(
                    f"[env.{env.name}.vm]: '{field}' is {sizes[field]}{unit}",
                    why=f"it must be at least {low}{unit}"
                        + (f" and at most {high}{unit}" if high is not None else ""),
                )


def validate_conf(conf: "Conf") -> None:
    _check_rules(conf)
    known_versions = versions_mod.load()
    for env in conf.envs.values():
        for u in env.access_users:
            if u not in conf.users:
                raise ConfError(
                    f"[env.{env.name}]: access-users references undefined user '{u}'"
                )
        if env.anci_version not in known_versions:
            known = ", ".join(sorted(known_versions)) or "(none)"
            raise ConfError(
                f"[env.{env.name}.vm]: unknown anci-version "
                f"'{env.anci_version}' (known: {known})",
                try_="update the env's anci-version or the bundled versions map",
            )


def _secrets_provide(conf: "Conf", env_name: str, names: set[str]) -> bool:
    if paths.secrets_encrypted(conf, env_name).is_file():
        return True
    plaintext = paths.secrets_plaintext(conf, env_name)
    if not plaintext.is_file():
        return False
    present: set[str] = set()
    for line in plaintext.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        present.add(line.split("=", 1)[0].strip())
    return names <= present


def validate_env(conf: "Conf", env_name: str) -> None:
    if env_name not in conf.envs:
        return
    missing: dict[tuple[str, ...], list[str]] = {}
    for db in sorted(conf.dbs.values(), key=lambda d: d.name):
        rep = db.resolve_for(env_name).replication
        if rep is None:
            continue
        names = {rep.access_key_env, rep.secret_key_env}
        if not _secrets_provide(conf, env_name, names):
            missing.setdefault(tuple(sorted(names)), []).append(db.name)
    if missing:
        raise ConfError(
            f"env '{env_name}' is missing replication credentials",
            why="\n".join(f"{', '.join(names)} (for {', '.join(dbs)})"
                          for names, dbs in missing.items()),
            try_=f"add them with `anci {env_name} secrets`, or to a plaintext "
                 f".anci/secrets.{env_name}.env",
        )
