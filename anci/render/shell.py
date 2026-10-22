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


def render_accounts_script(users: dict[str, list[str]]) -> str:
    """Shell script that reconciles guest accounts + `authorized_keys` to `users`.

    Ensures the shared `anci` group + service user (every app runs as them),
    ensures one account per user (passwordless sudo, `authorized_keys` from the
    conf), and revokes anyone left in the group who is no longer in the desired
    set.
    """
    lines = ["set -e", *ANCI_ACCOUNT_CMDS]
    for name, keys in users.items():
        n = _sh(name)
        lines.append(
            f"id -u {n} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash {n}"
        )
        lines.append(f"usermod -aG {ANCI_GROUP} {n}")
        lines.append(f"echo {_sh(f'{name} ALL=(ALL) NOPASSWD:ALL')} > /etc/sudoers.d/anci-{n}")
        lines.append(f"chmod 440 /etc/sudoers.d/anci-{n}")
        lines.append(f"install -d -o {n} -g {n} -m 700 /home/{n}/.ssh")
        lines.append(f": > /home/{n}/.ssh/authorized_keys")
        for key in keys:
            lines.append(f"echo {_sh(key)} >> /home/{n}/.ssh/authorized_keys")
        lines.append(f"chmod 600 /home/{n}/.ssh/authorized_keys")
        lines.append(f"chown {n}:{n} /home/{n}/.ssh/authorized_keys")
    # revoke stale group members (future-only revocation; accounts are kept)
    lines.append("for u in $(getent group %s | cut -d: -f4 | tr ',' ' '); do"
                 % ANCI_GROUP)
    lines.append("  case \" $u \" in")
    for name in users:
        lines.append(f"    *\" {name} \"*) ;;")
    lines.append("    *) gpasswd -d \"$u\" %s >/dev/null 2>&1 || true; "
                 "rm -f /home/\"$u\"/.ssh/authorized_keys ;;" % ANCI_GROUP)
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
