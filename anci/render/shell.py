"""Root shell scripts converge and `up` run on a box: guest accounts and the
anci-owned directory layout."""

from __future__ import annotations

import shlex

from .. import layout
from ..layout import ANCI_GROUP, ANCI_USER


def _sh(name: str) -> str:
    return shlex.quote(name)


ANCI_ACCOUNT_CMDS = (
    f"getent group {ANCI_GROUP} >/dev/null 2>&1 || groupadd {ANCI_GROUP}",
    f"id -u {ANCI_USER} >/dev/null 2>&1 || "
    f"useradd --system --no-create-home --home-dir /var/lib/anci "
    f"--shell /usr/sbin/nologin -g {ANCI_GROUP} {ANCI_USER}",
)


def sudoers_line(name: str) -> str:
    """A user's sudoers grant (`/etc/sudoers.d/anci-<name>`)."""
    return f"{name} ALL=(ALL) NOPASSWD:ALL\n"


def authorized_keys(keys: list[str]) -> str:
    """A user's `authorized_keys`: one key per line."""
    return "".join(f"{k}\n" for k in keys)


def grant_script(name: str, keys: list[str]) -> str:
    """Root shell giving `name` access: the account (created if missing), the
    `anci` group, passwordless sudo, and exactly `keys`."""
    n = _sh(name)
    home = f"/home/{n}"
    return "\n".join([
        f"id -u {n} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash {n}",
        f"usermod -aG {ANCI_GROUP} {n}",
        f"printf '%s' {_sh(sudoers_line(name))} > /etc/sudoers.d/anci-{n}",
        f"chmod 440 /etc/sudoers.d/anci-{n}",
        f"install -d -o {n} -g {n} -m 700 {home}/.ssh",
        f"printf '%s' {_sh(authorized_keys(keys))} > {home}/.ssh/authorized_keys",
        f"chmod 600 {home}/.ssh/authorized_keys",
        f"chown {n}:{n} {home}/.ssh/authorized_keys",
    ])


def revoke_script(name: str) -> str:
    """Root shell taking `name`'s access away: group, sudo grant and keys. The
    account stays (its files may matter); with no keys it can't log in."""
    n = _sh(name)
    return "\n".join([
        f"gpasswd -d {n} {ANCI_GROUP} >/dev/null 2>&1 || true",
        f"rm -f /etc/sudoers.d/anci-{n} /home/{n}/.ssh/authorized_keys",
    ])


def render_accounts_script(users: dict[str, list[str]]) -> str:
    """Shell script that reconciles guest accounts + `authorized_keys` to `users`.

    Ensures the shared `anci` group + service user (every app runs as them),
    grants each listed user access, and revokes anyone left in the group who
    isn't listed. An adopted box's bootstrap runs it; converge does the same
    per user (see anci/inspect/user.py).
    """
    lines = ["set -e", *ANCI_ACCOUNT_CMDS]
    lines += [grant_script(name, keys) for name, keys in users.items()]
    # revoke stale group members
    lines.append("for u in $(getent group %s | cut -d: -f4 | tr ',' ' '); do" % ANCI_GROUP)
    lines.append("  case \" $u \" in")
    for name in users:
        lines.append(f"    *\" {name} \"*) ;;")
    lines.append("    *) gpasswd -d \"$u\" %s >/dev/null 2>&1 || true; "
                 "rm -f /etc/sudoers.d/anci-\"$u\" /home/\"$u\"/.ssh/authorized_keys ;;" % ANCI_GROUP)
    lines.append("  esac")
    lines.append("done")
    return "\n".join(lines) + "\n"


def render_bootstrap_script() -> str:
    """Shell script laying out the anci-owned dirs.

    No packages are installed: sqlite3/litestream/caddy ship vendored on the
    box's first converge (see `anci/converge/vendored.py`), and static files go
    through the blob store over plain ssh, so the box needs no package manager."""
    return (
        "set -e\n"
        + "\n".join(layout_commands())
        + "\n"
    )


def layout_commands() -> tuple[str, ...]:
    """Idempotent `install -d` commands that set each dir's owner + mode."""
    return (
        f"install -d -o root -g {ANCI_GROUP} -m {layout.SHARED_MODE} {' '.join(layout.SHARED_DIRS)}",
        f"install -d -o root -g root -m {layout.CONFIG_MODE} {' '.join(layout.CONFIG_DIRS)}",
        f"install -d -o {ANCI_USER} -g {ANCI_GROUP} -m {layout.LOG_MODE} {layout.LOG_DIR}",
    )


def render_layout_script() -> str:
    """Shell that ensures the base layout (safe to run repeatedly)."""
    return "set -e\n" + "\n".join(layout_commands()) + "\n"
