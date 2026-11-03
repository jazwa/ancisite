"""ssh transport: user-identity ssh + host-key pinning.

We shell out to `ssh` rather than using paramiko so that the user's ssh agent,
keys, and config are used exactly as-is. Connections log in as
the identified conf user; privileged remote operations go through `sudo -n`.

Pinning:
- local boxs (env.ip = '127.0.0.1'): no pinning — the dev machine is the trust root.
  StrictHostKeyChecking=no, UserKnownHostsFile=/dev/null.
- a box anci manages on a remote hypervisor (env.ip = the host) sits on that
  host's NAT: reached with ProxyJump through <user>@<host>, pinned like any
  remote box (the host itself is trusted via the user's own known_hosts).
- remote vms: the pin lives committed in the project repo
  (`envs/<env>/hostkey`), verified strictly on every connection. Missing pin on
  a box anci provisioned = first contact (accept-new records it; anci tells the
  user to commit it).
  Mismatch = SSHError(unreachable=False, host_key=True) → auth-class error
  telling the user how to re-pin manually (delete the pin, reconnect).
- BatchMode: anci never falls back to a password prompt; a refused key is an
  auth error.

Pitfall (bitten once, regression-tested): ssh joins everything after the
destination into a single string and hands it to the REMOTE shell. Local argv
quoting is lost. So `run()` always sends one shell-quoted command string.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import box, errors, ui
from .paths import project_root

SSH_USER = "anci"


def _mux_socket(user: str, ip: str, port: int) -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    d = Path(base) / f"anci-{os.geteuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    h = hashlib.sha256(f"{user}@{ip}:{port}".encode()).hexdigest()[:12]
    return str(d / f"mux-{h}")


def _mux_opts(user: str, ip: str, port: int) -> list[str]:
    return [
        "-o", f"ControlPath={_mux_socket(user, ip, port)}",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=10m",
    ]


class SSHError(RuntimeError):
    def __init__(
        self,
        msg: str,
        *,
        unreachable: bool = False,
        auth: bool = False,
        stderr: str = "",
        host_key: bool = False,
    ):
        super().__init__(msg)
        self.unreachable = unreachable
        self.auth = auth
        self.stderr = stderr
        self.host_key = host_key


_HOSTKEY_MARKERS = (
    "host key verification failed",
    "remote host identification has changed",
)
_AUTH_MARKERS = (
    "permission denied",
    "publickey",
    "authentication failed",
    "no such identity",
    "too many authentication failures",
)


def _classify(stderr: str) -> tuple[bool, bool]:
    s = stderr.lower()
    host_key = any(m in s for m in _HOSTKEY_MARKERS)
    auth = (not host_key) and any(m in s for m in _AUTH_MARKERS)
    return host_key, auth


def _ssh_error(msg: str, rc: int, stderr: str) -> SSHError:
    host_key, auth = _classify(stderr)
    return SSHError(f"{msg}\nstderr: {stderr.strip()}", unreachable=rc == 255,
                    auth=auth, stderr=stderr, host_key=host_key)


def _missing(binary: str) -> errors.EnvError:
    return errors.EnvError(
        f"`{binary}` isn't on your PATH",
        why="anci drives your local OpenSSH client over ssh",
        try_="install an OpenSSH client (ssh + ssh-keygen)",
    )


@dataclass
class Result:
    stdout: str
    stderr: str


@dataclass
class Conn:

    ip: str
    opts: list[str] = field(default_factory=list)
    user: str = SSH_USER
    port: int = 22

    def ssh_argv(self, remote_args: list[str]) -> list[str]:
        return ["ssh", "-p", str(self.port), *self.opts,
                f"{self.user}@{self.ip}", *remote_args]

    def argv(self, args: list[str]) -> list[str]:
        return self.ssh_argv([shlex.join(args)])


def connect(conf, env_name: str, user: str) -> Conn:
    ip = box.resolve_env_ip(conf, env_name)
    if ip is None:
        raise SSHError(f"{env_name} has no box yet (no address recorded by `up`)",
                       unreachable=True)
    env = conf.envs[env_name]
    opts = [*_mux_opts(user, ip, 22), "-o", "BatchMode=yes"]
    if box.is_remote_host(env):
        opts += ["-o", f"ProxyJump={user}@{env.ip}"]

    if env.ip == "127.0.0.1":
        opts += ["-o", "ConnectTimeout=1", "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
    else:
        pin = pin_path(conf, env_name)
        if pin.is_file():
            checking = "yes"
        else:
            pin.parent.mkdir(parents=True, exist_ok=True)
            print(f"  {ui.yellow('⚠')} first contact with {ip}: "
                  f"recording host key to {pin} — commit it to the project repo")
            checking = "accept-new"
        opts += ["-o", "ConnectTimeout=5", "-o", f"UserKnownHostsFile={pin}",
                 "-o", f"StrictHostKeyChecking={checking}"]
    return Conn(ip=ip, user=user, opts=opts)


def pin_path(conf, env_name: str) -> Path:
    return project_root(conf) / "envs" / env_name / "hostkey"


def run(conn: Conn, args: list[str], *, timeout: int = 120) -> Result:
    try:
        p = subprocess.run(conn.argv(args), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise _missing("ssh") from e
    if p.returncode != 0:
        raise _ssh_error(f"remote command failed (exit {p.returncode}): {shlex.join(args)}",
                         p.returncode, p.stderr)
    return Result(stdout=p.stdout, stderr=p.stderr)


def script(conn: Conn, script_text: str, *, timeout: int = 120) -> Result:
    return run(conn, ["bash", "-c", script_text], timeout=timeout)


def facts_script(probes: dict[str, str], preamble: str = "") -> str:
    lines = [f"{{ {preamble}\n}} >/dev/null 2>&1"] if preamble else []
    for key, snippet in probes.items():
        if "\t" in key or "\n" in key:
            raise ValueError(f"fact key {key!r} can't contain a tab or newline")
        lines.append(
            f"{{ {snippet}\n}} 2>/dev/null | while IFS= read -r l; do "
            f"printf '%s\\t%s\\n' {shlex.quote(key)} \"$l\"; done"
        )
    return "\n".join(lines) + "\n"


def parse_facts(out: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in out.splitlines():
        key, tab, value = line.partition("\t")
        if tab:
            found.setdefault(key, []).append(value)
    return found


def facts(conn: Conn, probes: dict[str, str], *, preamble: str = "",
          timeout: int = 120, root: bool = False) -> dict[str, list[str]]:
    run_ = sudo if root else script
    return parse_facts(run_(conn, facts_script(probes, preamble), timeout=timeout).stdout)


def fact(found: dict[str, list[str]], key: str) -> str | None:
    lines = found.get(key)
    return lines[0] if lines else None


def port_probe_line(port: int) -> str:
    return f"timeout 1 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/{port}'"


def sudo(conn: Conn, script_text: str, *, timeout: int = 120) -> Result:
    return script(conn, f"sudo -n bash -c {shlex.quote(script_text)}", timeout=timeout)


def test(conn: Conn, script_text: str, *, timeout: int = 30) -> bool:
    try:
        script(conn, script_text, timeout=timeout)
        return True
    except SSHError:
        return False


def put(conn: Conn, local: str, remote: str, *, private: bool = False,
        timeout: int = 600) -> None:
    umask = "umask 077; " if private else ""
    cmd = conn.ssh_argv([f"{umask}cat > {shlex.quote(remote)}"])
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        raise _missing("ssh") from e
    assert proc.stdin is not None
    try:
        with open(local, "rb") as f:
            while chunk := f.read(1 << 18):
                proc.stdin.write(chunk)
        proc.stdin.close()
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        raise SSHError(f"upload to {remote} timed out", unreachable=True) from e
    stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if rc != 0:
        raise _ssh_error(f"upload to {remote} failed (exit {rc})", rc, stderr)


def put_down(conn: Conn, remote: str, local: str, *, timeout: int = 600) -> None:
    cmd = conn.ssh_argv([f"cat {shlex.quote(remote)}"])
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise _missing("ssh") from e
    if p.returncode != 0:
        raise _ssh_error(f"download of {remote} failed", p.returncode,
                         p.stderr.decode(errors="replace"))
    Path(local).write_bytes(p.stdout)


def boot_race(e: SSHError) -> bool:
    return e.auth or e.unreachable


def wait_ready(conn: Conn, *, auth_timeout: int = 60, boot_timeout: int = 600) -> None:
    deadline = time.monotonic() + auth_timeout
    while True:
        try:
            run(conn, ["true"], timeout=15)
            break
        except (SSHError, subprocess.TimeoutExpired) as e:
            retryable = not isinstance(e, SSHError) or boot_race(e)
            if not retryable or time.monotonic() >= deadline:
                raise
            time.sleep(3)

    state = script(
        conn, "[ -e /var/lib/cloud/instance/boot-finished ] && echo 'status: done' || "
              "cloud-init status 2>/dev/null || echo 'status: unavailable'", timeout=20
    ).stdout
    if "running" not in state:
        return
    print(f"  {ui.dim('waiting for the box to finish its first boot (cloud-init)…')}")
    script(conn, "cloud-init status --wait >/dev/null 2>&1 || true", timeout=boot_timeout)
