"""One mutating run per box at a time.

`converge` (every deploy / reconcile / up) and `migrate` hold the box's lock
for their whole run, so two deployers (or a deploy and a migrate) never
interleave their prepare/apply/restart/gc steps.

The lock is an `flock` held by a root shell on the box that lives exactly as
long as one ssh session: anci keeps that session's stdin open while it works
and closes it to release. If anci dies (Ctrl-C, kill, a dropped connection),
the session ends, the shell's `cat` sees EOF and the lock is gone — there is
no stale lock to clean up. Root-owned files under `/run/lock` (tmpfs, cleared
on boot); root because every deployer has passwordless sudo, and it sidesteps
sticky-dir ownership rules between different login users.

Re-entrant within one anci process (`migrate` ends in a `converge`).
"""

from __future__ import annotations

import shlex
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone

from . import errors, ssh, ui

LOCK = "/run/lock/anci.lock"
WHO = "/run/lock/anci.who"
WAIT_S = 600

_held: dict[tuple[str, int], int] = {}   # (ip, port) -> nesting depth


def _script(who: str) -> str:
    return f"""set -e
exec 9>>{LOCK}
if ! flock -n 9; then
  printf 'busy\\t%s\\n' "$(cat {WHO} 2>/dev/null)"
  flock -w {WAIT_S} 9 || {{ echo timeout; exit 3; }}
fi
printf '%s\\n' {shlex.quote(who)} > {WHO}
echo ok
cat >/dev/null
"""


def _acquire(conn: ssh.Conn, user: str, what: str) -> subprocess.Popen:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    cmd = conn.argv(["sudo", "-n", "bash", "-c", _script(f"{user} ({what}, since {ts})")])
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as e:
        raise ssh._missing("ssh") from e
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line == "ok":
            return proc
        if line.startswith("busy\t"):
            holder = line.split("\t", 1)[1] or "another anci run"
            print(f"  {ui.dim(f'waiting for {holder} to finish in this env…')}")
        elif line == "timeout":
            break
    rc = proc.wait()
    stderr = proc.stderr.read() if proc.stderr else ""
    if rc == 3:
        raise errors.EnvError(
            "another anci run kept this env busy for 10 minutes",
            why="only one deploy / reconcile / migrate runs on an env at a time",
            try_="wait for it to finish, then re-run",
        )
    raise ssh._ssh_error(f"couldn't take the env's lock (exit {rc})", rc, stderr)


def _release(proc: subprocess.Popen) -> None:
    try:
        if proc.stdin is not None:
            proc.stdin.close()      # `cat` sees EOF → the shell exits → flock released
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()                 # the session dying releases it too
        proc.wait()


@contextmanager
def hold(conn: ssh.Conn, user: str, what: str, *, wait_ready: bool = False):
    """Hold the box's lock for the `with` block (waiting up to WAIT_S for it).
    `wait_ready`: if the box can't be reached or refuses the login and may still
    be booting, wait for it (`ssh.wait_ready`) and try once more."""
    key = (conn.ip, conn.port)
    if key in _held:
        _held[key] += 1
        try:
            yield
        finally:
            _held[key] -= 1
        return
    try:
        proc = _acquire(conn, user, what)
    except ssh.SSHError as e:
        if not (wait_ready and ssh.boot_race(conn, e)):
            raise
        ssh.wait_ready(conn)
        proc = _acquire(conn, user, what)
    _held[key] = 1
    try:
        yield
    finally:
        del _held[key]
        _release(proc)
