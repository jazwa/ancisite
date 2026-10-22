"""Cross-reference validation for `anci.toml`.

Parsing (`conf.py`) enforces *shape* — required keys, types, unknown-key
rejection. This module owns the references *between* parts of the conf, and
between the conf and the local project, so a conf that points at a user,
version, box, or secret that doesn't exist fails fast, before any command does
work.

Everything here is offline — no ssh, no SDK, no store.

- `validate_conf(conf)`: pure conf cross-references. Called by `conf.load()`,
  so every env command gets it; `init` never loads a conf.
- `validate_env(conf, env_name)`: the conf plus the local secrets for one env.
  Called by the CLI before a verb does work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import ConfError
from . import net, paths
from . import versions as versions_mod

if TYPE_CHECKING:
    from .conf import Conf


def validate_conf(conf: "Conf") -> None:
    """Cross-references that live entirely inside the conf. Pure."""
    known_versions = versions_mod.load()
    boxes: dict[str, str] = {}   # resolved address -> env name (one env = one box)
    for env in conf.envs.values():
        address = env.ip if env.adopted else net.env_ip(paths.box_id(conf), env.name)
        if address in boxes:
            raise ConfError(
                f"[env.{env.name}] and [env.{boxes[address]}] are the same box "
                f"({address}) — one env is one box, so they can't share an address"
            )
        boxes[address] = env.name
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
    """Does any secrets tier provide `names` for this env?

    The encrypted tier can't be read offline (SOPS + a key), so its *presence*
    is the check; the plaintext tier is parsed for the named keys.
    """
    if paths.secrets_encrypted(conf, env_name).is_file():
        return True   # encrypted: presence is all we can verify here
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
    """Cross-references between this env's conf and the local project.

    Today: every replicated db that *names* credential env vars must have a
    secrets tier providing them. Named-but-missing creds leave the replica
    unauthenticated, and the failure otherwise surfaces only later — the status
    probe even pays a ~12s SDK metadata timeout before giving up.
    """
    if env_name not in conf.envs:
        return  # the CLI reports unknown envs itself
    missing: dict[tuple[str, ...], list[str]] = {}   # credential names -> dbs naming them
    for db in sorted(conf.dbs.values(), key=lambda d: d.name):
        rep = db.resolve_for(env_name).replication
        if rep is None or not (rep.access_key_env and rep.secret_key_env):
            continue  # no named creds = the default AWS chain; nothing to check
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
