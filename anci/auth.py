"""Authorization: access-users enforced client-side.

The model: your ssh key identifies you (matches a conf user); the conf decides
which envs that user may target. This is *convenience-grade* authorization —
the real security boundary on a box is its `authorized_keys`. What this
buys: a second user or a CI bot can't deploy to prod just because they can
ssh to the box, and the rule lives in the reviewed conf file.

Enforced on every verb that touches the guest (deploy, migrate, regen,
logs, status).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import errors
from .conf import Conf


def authorize(conf: Conf, env_name: str) -> str:
    """Who is the caller, and may they target this env?

    Returns the conf username. Raises AuthError when the key isn't in the conf
    or the user lacks access to the env.
    """
    if env_name not in conf.envs:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    user = identify_user(conf)
    if user == "?":
        raise errors.AuthError(
            "your ssh key isn't in anci.toml",
            why="anci identifies deployers by their public keys",
            try_="add your ssh public key under [user.<your-name>] in anci.toml",
        )
    if user not in conf.envs[env_name].access_users:
        raise errors.AuthError(
            f"you ({user}) don't have access to env '{env_name}'",
            why=f"access-users for '{env_name}': {', '.join(conf.envs[env_name].access_users)}",
            try_=f"ask for access: your name goes in [env.{env_name}].access-users",
        )
    return user


def identify_user(conf: Conf) -> str:
    """Which conf user is deploying? Match local public keys against the conf.

    Client-side only (the ssh key IS the identity). Falls back to "?" when no
    conf user's key matches; `auth.authorize` turns that into a hard failure.
    """
    local_keys: set[str] = set()
    try:
        out = subprocess.run(
            ["ssh-add", "-L"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0:
            local_keys.update(l.strip() for l in out.stdout.splitlines() if l.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    # ANCI_SSH_KEY points at the identity's .pub file for CI/service accounts
    # (no agent, nonstandard path); ~/.ssh/*.pub covers normal setups.
    candidates: list[Path] = []
    override = os.environ.get("ANCI_SSH_KEY")
    if override:
        candidates.append(Path(override))
    ssh_dir = Path.home() / ".ssh"
    if ssh_dir.is_dir():
        candidates.extend(sorted(ssh_dir.glob("*.pub")))
    for p in candidates:
        try:
            if p.is_file():
                local_keys.add(p.read_text().strip())
        except OSError:
            continue
    local = {_key_id(k) for k in local_keys} - {None}
    for user in conf.users.values():
        if {_key_id(k) for k in user.ssh_pub_keys} & local:
            return user.name
    return "?"


def _key_id(line: str) -> tuple[str, str] | None:
    """A public key's identity: (type, base64). The trailing comment
    (`jack@dev-machine`) is a label, not part of the key — conf and local files
    routinely differ there."""
    parts = line.split()
    return (parts[0], parts[1]) if len(parts) >= 2 else None
