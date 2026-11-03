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
    return f"{name} ALL=(ALL) NOPASSWD:ALL\n"


def authorized_keys(keys: list[str]) -> str:
    return "".join(f"{k}\n" for k in keys)


def grant_script(name: str, keys: list[str]) -> str:
    n = _sh(name)
    home = f"/home/{n}"
    return "\n".join([
        f"if id -u {n} >/dev/null 2>&1; then [ \"$(id -u {n})\" -ge 1000 ] || "
        f"{{ echo {_sh(f'{name} is a system account on the box: give the user another name')} >&2; exit 1; }}",
        f"else useradd --create-home --shell /bin/bash {n}; fi",
        f"usermod -aG {ANCI_GROUP} {n}",
        f"printf '%s' {_sh(sudoers_line(name))} > /etc/sudoers.d/anci-{n}",
        f"chmod 440 /etc/sudoers.d/anci-{n}",
        f"install -d -o {n} -g {n} -m 700 {home}/.ssh",
        f"printf '%s' {_sh(authorized_keys(keys))} > {home}/.ssh/authorized_keys",
        f"chmod 600 {home}/.ssh/authorized_keys",
        f"chown {n}:{n} {home}/.ssh/authorized_keys",
    ])


def revoke_script(name: str) -> str:
    n = _sh(name)
    return "\n".join([
        f"gpasswd -d {n} {ANCI_GROUP} >/dev/null 2>&1 || true",
        f"rm -f /etc/sudoers.d/anci-{n} /home/{n}/.ssh/authorized_keys",
    ])


def render_accounts_script(users: dict[str, list[str]]) -> str:
    lines = ["set -e", *ANCI_ACCOUNT_CMDS]
    lines += [grant_script(name, keys) for name, keys in users.items()]
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
    return (
        "set -e\n"
        + "\n".join(layout_commands())
        + "\n"
    )


def layout_commands() -> tuple[str, ...]:
    return (
        f"install -d -o root -g {ANCI_GROUP} -m {layout.SHARED_MODE} {' '.join(layout.SHARED_DIRS)}",
        f"install -d -o root -g root -m {layout.CONFIG_MODE} {' '.join(layout.CONFIG_DIRS)}",
        f"install -d -o {ANCI_USER} -g {ANCI_GROUP} -m {layout.LOG_MODE} {layout.LOG_DIR}",
    )


def render_layout_script() -> str:
    return "set -e\n" + "\n".join(layout_commands()) + "\n"
