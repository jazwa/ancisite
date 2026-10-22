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
  user to commit it). An adopted box is only pinned by its first `up`, once the
  bootstrap succeeds; without the pin it isn't set up, and connecting is refused.
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
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import box, errors, ui
from .paths import project_root

SSH_USER = "anci"


def _mux_socket(user: str, ip: str, port: int) -> str:
    """Path of this conn's ssh multiplexing socket.

    One master connection per (user, box, port): the first ssh spawns it (in
    the background); every later command reuses it instead of paying a fresh
    TCP connect + auth round trip per exec (~100-200ms → ~5ms). The dir must
    be 0700 — ssh refuses mux sockets in group/world-writable paths — and
    per-uid so two local users never collide. The name is a fixed-length hash:
    a socket path is capped at 104 bytes on macOS, whose temp dir alone is ~50.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    d = Path(base) / f"anci-{os.geteuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    h = hashlib.sha256(f"{user}@{ip}:{port}".encode()).hexdigest()[:12]
    return str(d / f"mux-{h}")


def _mux_opts(user: str, ip: str, port: int) -> list[str]:
    """ssh options turning every conn into a member of one shared connection.

    ControlMaster=auto absorbs a dead stale socket: the next ssh validates the
    master with a mux ping and quietly starts a fresh one when it's gone (e.g.
    after `env down`/`up`), so nothing here needs invalidation bookkeeping.
    """
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


# ssh reports connection *and* auth failures with exit 255, so we tell them
# apart from the stderr text (the taxonomy needs the difference — one is "box
# down", the other is "your key was refused").
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
    """(host_key, auth) inferred from ssh stderr."""
    s = stderr.lower()
    host_key = any(m in s for m in _HOSTKEY_MARKERS)
    auth = (not host_key) and any(m in s for m in _AUTH_MARKERS)
    return host_key, auth


def _ssh_error(msg: str, rc: int, stderr: str) -> SSHError:
    """An SSHError classified from ssh's exit code and stderr (255 = ssh itself
    failed: connection or auth)."""
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
    """Everything needed to reach one guest. Built once per command."""

    ip: str
    opts: list[str] = field(default_factory=list)   # ssh option pairs
    user: str = SSH_USER    # login on the guest (the identified conf user)
    port: int = 22          # sshd port on the box
    # anci boots this box and its first boot (cloud-init) creates the accounts,
    # so sshd can be down or refuse the login for a while after `up`. False for
    # an adopted box: its accounts exist (and were verified) before the pin did.
    boots: bool = True

    def ssh_argv(self, remote_args: list[str]) -> list[str]:
        """`ssh` argv with trailing (raw) remote args — used for file transfer."""
        return ["ssh", "-p", str(self.port), *self.opts,
                f"{self.user}@{self.ip}", *remote_args]

    def argv(self, args: list[str]) -> list[str]:
        """Full local argv for running `args` on the guest, remote-shell-safe."""
        return self.ssh_argv([shlex.join(args)])


def connect(conf, env_name: str, user: str) -> Conn:
    """Build a Conn for an env, applying the pinning policy."""
    ip = box.resolve_env_ip(conf, env_name)
    env = conf.envs[env_name]
    opts = [*_mux_opts(user, ip, env.port), "-o", "BatchMode=yes"]
    if box.is_remote_host(env):
        # managed box on a remote hypervisor: on the host's NAT, reached through it
        opts += ["-o", f"ProxyJump={user}@{env.ip}"]

    if env.ip == "127.0.0.1":
        # local box: no pinning (see module docstring); LogLevel=ERROR keeps the
        # "Permanently added to known hosts" noise out of the output. ConnectTimeout
        # is short here — a localhost connect is instant when the box is up, so this
        # just fails fast (≈1s) when it's off, instead of the ~3s ARP timeout.
        opts += ["-o", "ConnectTimeout=1", "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
    else:
        pin = pin_path(conf, env_name)
        if pin.is_file():
            checking = "yes"
        elif env.adopted:
            # the pin is written by a successful first `up`: no pin = never set up
            raise errors.EnvError(
                f"{env_name} isn't set up",
                why=f"there's no pinned host key in {pin.relative_to(project_root(conf))}",
                try_=f"anci {env_name} up --user <login>",
            )
        else:
            # first contact: record the pin as a side effect, tell the user to
            # commit it. ssh can't create a missing parent dir itself, so a fresh
            # env's envs/<env>/ would silently fail to record the pin.
            pin.parent.mkdir(parents=True, exist_ok=True)
            print(f"  {ui.yellow('⚠')} first contact with {ip}: "
                  f"recording host key to {pin} — commit it to the project repo")
            checking = "accept-new"
        opts += ["-o", "ConnectTimeout=5", "-o", f"UserKnownHostsFile={pin}",
                 "-o", f"StrictHostKeyChecking={checking}"]
    return Conn(ip=ip, user=user, port=env.port, opts=opts, boots=not env.adopted)


def pin_path(conf, env_name: str) -> Path:
    """Project-repo pin file for an env: envs/<env>/hostkey (committed)."""
    return project_root(conf) / "envs" / env_name / "hostkey"


def env_trust(conf, env_name: str, *, stdin=None) -> list[str] | None:
    """The deliberate host-key ceremony — used by an adopted box's first `up`.

    TTY-only by default: prints the fingerprint for eyeballing against the
    provider's console and asks for confirmation. Returns the confirmed
    known_hosts lines, or None if declined. Writes nothing: the caller pins
    them (`write_pin`) once the box is set up. `stdin` is injectable for tests.
    """
    if env_name not in conf.envs:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    if stdin is None and not sys.stdin.isatty():
        raise errors.AuthError(
            "re-pinning a box's identity needs a terminal",
            why="trust decisions can't be made non-interactively",
            try_="run this in your terminal, not from a script",
        )

    ip = box.resolve_env_ip(conf, env_name)
    port = conf.envs[env_name].port
    try:
        # no `-t`: keep every key type the host offers, so the pin covers
        # whichever type ssh negotiates on a later connection
        p = subprocess.run(
            ["ssh-keyscan", "-p", str(port), ip],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as e:
        raise _missing("ssh-keyscan") from e
    keys = [
        line.strip()
        for line in p.stdout.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not keys:
        raise errors.EnvError(
            f"couldn't reach {ip} to read its host key",
            try_=f"is the box up? is port {port} reachable?",
            debug=p.stderr.strip(),
        )
    # fingerprint(s) for the human to verify against the provider's console
    try:
        subprocess.run(["ssh-keygen", "-lf", "-"], input="\n".join(keys) + "\n", text=True)
    except FileNotFoundError as e:
        raise _missing("ssh-keygen") from e

    if not ui.confirm(
        "trusting this box's host key:",
        [f"anci will bootstrap and manage {ip} over this identity",
         "verify the fingerprints above against your provider's console"],
        f"trust these keys for env '{env_name}'?",
        stdin=stdin,
    ):
        print(f"  {ui.dim('aborted — nothing changed')}")
        return None
    return keys


def write_pin(conf, env_name: str, keys: list[str]) -> Path:
    """Write an env's committed pin file from known_hosts lines; return its path."""
    pin = pin_path(conf, env_name)
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text("\n".join(keys) + "\n")
    return pin


# ---------------------------------------------------------------------------
# connection-level helpers


def run(conn: Conn, args: list[str], *, timeout: int = 120) -> Result:
    """Run a command (argv list) on the guest. Raises SSHError on failure."""
    try:
        p = subprocess.run(conn.argv(args), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise _missing("ssh") from e
    if p.returncode != 0:
        raise _ssh_error(f"remote command failed (exit {p.returncode}): {shlex.join(args)}",
                         p.returncode, p.stderr)
    return Result(stdout=p.stdout, stderr=p.stderr)


def script(conn: Conn, script_text: str, *, timeout: int = 120) -> Result:
    """Run a shell script line on the guest (via bash -c)."""
    return run(conn, ["bash", "-c", script_text], timeout=timeout)


def facts_script(probes: dict[str, str], preamble: str = "") -> str:
    """One script running every probe; each output line comes back as
    `<key>\t<line>`. A probe's stderr and exit status are ignored. `preamble`
    (shell functions the probes share) runs first, its output discarded."""
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
    """`<key>\t<line>` output → key -> lines, in order."""
    found: dict[str, list[str]] = {}
    for line in out.splitlines():
        key, tab, value = line.partition("\t")
        if tab:
            found.setdefault(key, []).append(value)
    return found


def facts(conn: Conn, probes: dict[str, str], *, preamble: str = "",
          timeout: int = 120, root: bool = False) -> dict[str, list[str]]:
    """Run shell probes on the box in one round trip: key -> its stdout lines
    (missing = it printed nothing). The one way anci reads box state.
    `root`: the whole script runs under one `sudo` (not one per probe)."""
    run_ = sudo if root else script
    return parse_facts(run_(conn, facts_script(probes, preamble), timeout=timeout).stdout)


def fact(found: dict[str, list[str]], key: str) -> str | None:
    """A probe's first output line, or None when it printed nothing."""
    lines = found.get(key)
    return lines[0] if lines else None


def port_probe_line(port: int) -> str:
    """The shell snippet for "is this guest TCP port answering" (bash /dev/tcp)."""
    return f"timeout 1 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/{port}'"


def sudo(conn: Conn, script_text: str, *, timeout: int = 120) -> Result:
    """Run a shell script on the guest as root (passwordless sudo)."""
    return script(conn, f"sudo -n bash -c {shlex.quote(script_text)}", timeout=timeout)


def test(conn: Conn, script_text: str, *, timeout: int = 30) -> bool:
    """Run a shell script; return True iff it exits 0 (no exception)."""
    try:
        script(conn, script_text, timeout=timeout)
        return True
    except SSHError:
        return False


def put(conn: Conn, local: str, remote: str, *, private: bool = False,
        timeout: int = 600) -> None:
    """Upload a file by streaming it over ssh stdin (no scp / SFTP subsystem
    needed). `private`: created owner-only (0600) on the box — for anything
    that may carry secrets before it's installed."""
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
    """Download a file from the guest over plain ssh (no scp / SFTP subsystem)."""
    cmd = conn.ssh_argv([f"cat {shlex.quote(remote)}"])
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise _missing("ssh") from e
    if p.returncode != 0:
        raise _ssh_error(f"download of {remote} failed", p.returncode,
                         p.stderr.decode(errors="replace"))
    Path(local).write_bytes(p.stdout)


def boot_race(conn: Conn, e: SSHError) -> bool:
    """Could `e` just be `conn`'s box still booting (so waiting may fix it)?"""
    return conn.boots and (e.auth or e.unreachable)


def wait_ready(conn: Conn, *, auth_timeout: int = 60, boot_timeout: int = 600) -> None:
    """Block until the guest is actually usable, not just listening on TCP:22.

    A freshly `env up`'d box answers on port 22 *before* cloud-init has created the
    conf account or finished installing packages, so the next command would race
    it (auth refused, or a dpkg lock). Wait for auth, then let cloud-init reach
    "done" — `cloud-init status --wait` is what blocks through the package install.
    """
    deadline = time.monotonic() + auth_timeout
    while True:
        try:
            run(conn, ["true"], timeout=15)
            break
        except (SSHError, subprocess.TimeoutExpired) as e:
            # auth (and the port) can lag a fresh boot; retry those, for a while
            retryable = not isinstance(e, SSHError) or boot_race(conn, e)
            if not retryable or time.monotonic() >= deadline:
                raise
            time.sleep(3)

    # boot-finished (cloud-init's last stage) answers instantly; `cloud-init
    # status` costs ~200 ms, so it only runs while that file is missing
    state = script(
        conn, "[ -e /var/lib/cloud/instance/boot-finished ] && echo 'status: done' || "
              "cloud-init status 2>/dev/null || echo 'status: unavailable'", timeout=20
    ).stdout
    if "running" not in state:
        return  # already done / errored / no cloud-init — nothing to wait for
    print(f"  {ui.dim('waiting for the box to finish its first boot (cloud-init)…')}")
    script(conn, "cloud-init status --wait >/dev/null 2>&1 || true", timeout=boot_timeout)
