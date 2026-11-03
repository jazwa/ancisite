"""`up` for an adopted box: adopt someone else's machine (e.g. a rented VPS).

An adopted box (`mode = 'adopt'`) is used directly — nothing is hypervised.
Its first `up` has you confirm the box's host key (the trust ceremony, closing
the TOFU gap), then runs the bootstrap over a connection verified against those
keys as a login user (`up --user`, default `root`): create one guest account per
`access-user`, the shared `anci` group + `anci` service user, lay out
`/var/lib/anci`. No packages are installed — sqlite3/litestream/caddy ship
vendored on the first converge, and static files go through the blob store over
plain ssh. The keys are pinned only once the bootstrap verifies, so a failed
first `up` leaves nothing behind.

Once the pin exists the box is set up: later `up`s skip all of that and just
converge as the conf user (which reconciles accounts + `authorized_keys` to the
conf, so adding or removing a user is an edit + `up` or a deploy). `--user` on a
pinned box reruns the bootstrap (it's idempotent) without the ceremony.
"""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path

from .. import auth, errors, layout, ssh, ui
from ..conf import Conf
from ..layout import ANCI_USER
from ..render import shell

def ensure_anci_account(conn: ssh.Conn) -> str:
    ssh.sudo(conn, "\n".join(["set -e", *shell.ANCI_ACCOUNT_CMDS]))
    return ANCI_USER


def preflight_checks() -> dict[str, str]:
    return {
        "libc": "[ -e /lib64/ld-linux-x86-64.so.2 ] || "
                "echo 'needs glibc (musl distros like Alpine are unsupported)'",
        "arch": 'case "$(uname -m)" in x86_64|amd64) ;; '
                '*) echo "$(uname -m) (anci pins amd64 artifacts)" ;; esac',
        "systemd": "command -v systemctl >/dev/null && [ -d /run/systemd/system ] "
                   "|| echo 'missing or not booted with systemd'",
        "bash": "command -v bash >/dev/null || echo missing",
        "sudo": '[ "$(id -u)" = 0 ] || sudo -n true || '
                "echo 'passwordless sudo required for the login user'",
    }


def verify_checks(users: dict[str, list[str]]) -> dict[str, str]:
    checks = {
        "group anci": "getent group anci >/dev/null || echo missing",
        f"account {ANCI_USER}": f"id -u {ANCI_USER} >/dev/null || echo missing",
    }
    for name in users:
        n = shlex.quote(name)
        checks[f"account {name}"] = f"id -u {n} >/dev/null || echo missing"
        checks[f"member {name}"] = f"id -nG {n} | grep -qw anci || echo 'not in anci group'"
        checks[f"keys {name}"] = f"[ -s /home/{n}/.ssh/authorized_keys ] || echo 'empty/missing'"
    for path in layout.SHARED_DIRS + layout.CONFIG_DIRS:
        checks[f"dir {path}"] = f"[ -d {path} ] || echo missing"
    return checks


def problems(conn: ssh.Conn, checks: dict[str, str], *, root: bool = False) -> list[str]:
    return [f"{k}: {v[0]}" for k, v in ssh.facts(conn, checks, root=root).items()]


def _checks_step(pipe: ui.Pipeline, label: str, conn: ssh.Conn, checks: dict[str, str],
                 *, ok: str, what: str, try_: str, root: bool = False) -> None:
    step = pipe.step(label)
    failed = problems(conn, checks, root=root)
    if failed:
        step.fail()
        for p in failed:
            print(f"  {ui.red('✗')} {p}")
        raise errors.EnvError(what, try_=try_)
    step.done(ok)


def up_adopt(conf: Conf, env_name: str, *, login_user: str | None = None,
             pipe: ui.Pipeline | None = None) -> str:
    env = conf.envs[env_name]
    address = env.ip
    if not env.adopted:
        raise errors.InternalError(f"env '{env_name}' isn't mode = 'adopt' — anci provisions its VM")
    auth.authorize(conf, env_name)

    pin = ssh.pin_path(conf, env_name)
    if pin.is_file():
        if login_user is None:
            print(f"\n  {ui.bold(env_name)} {ui.dim(f'→ {address} (set up; pinned in {pin.name})')}")
            return address
        print(f"\n  {ui.bold(env_name)} {ui.dim(f'→ {address} (pinned in {pin.name}; setting up as {login_user})')}\n")
        _bootstrap(conf, env_name, login_user, pin, pipe)
        return address

    login_user = login_user or "root"
    print(f"\n  {ui.bold(env_name)} {ui.dim(f'→ {address} (first contact, as {login_user})')}\n")

    keys = ssh.env_trust(conf, env_name)
    if not keys:
        raise errors.EnvError(
            f"{env_name} isn't set up — you didn't trust the box's host key",
            why="anci only bootstraps a box whose identity you've confirmed",
            try_=f"check the fingerprint with your provider, then re-run: anci {env_name} up",
        )

    with tempfile.TemporaryDirectory() as td:
        known = Path(td) / "hostkey"
        known.write_text("\n".join(keys) + "\n")
        _bootstrap(conf, env_name, login_user, known, pipe)
    pin = ssh.write_pin(conf, env_name, keys)
    print(f"  {ui.green('✓')} pinned {address} → {pin}")
    print(f"  {ui.dim('next: commit it (git add ' + str(pin) + ')')}")
    return address


def _bootstrap(conf: Conf, env_name: str, login_user: str, known_hosts: Path,
               pipe: ui.Pipeline | None = None) -> None:
    env = conf.envs[env_name]
    conn = ssh.Conn(
        ip=env.ip, user=login_user, port=env.port, boots=False,
        opts=["-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
              "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes"],
    )
    own = pipe is None
    pipe = pipe or ui.Pipeline()

    _checks_step(pipe, "preflight", conn, preflight_checks(), ok="ok",
                 what="this box isn't a supported host",
                 try_="anci needs a glibc x86_64 Linux with systemd and passwordless sudo")

    step = pipe.step("accounts")
    users = conf.users_for_env(env_name)
    ssh.sudo(conn, shell.render_accounts_script(users))
    step.done(", ".join(sorted(users)) or "(none)")

    step = pipe.step("prerequisites")
    ssh.sudo(conn, shell.render_bootstrap_script())
    step.done()


    _checks_step(pipe, "verify", conn, verify_checks(users), ok="in line", root=True,
                 what="the bootstrap left the box out of line",
                 try_=f"re-run `anci {env_name} up --user {login_user}`; if it persists, add --debug")

    if own:
        pipe.done()
