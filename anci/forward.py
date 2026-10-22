"""A local env's localhost forward: its services at http://<project>.localhost:<port>.

Browsers treat `*.localhost` as a secure context (Secure cookies work over plain
http) and resolve it to this machine. Kernel NAT on the dev machine — nftables,
no process — rewrites the connections this machine makes to 127.0.0.1:<port> to
the VM. anci reserves 20000–20999; each local env takes a group of 10 ports, one
per app port slot (8000, 8010, … 8090), so adding an exe never changes the rules
and only an env's first converge needs sudo. Each env also gets a canary port
(21000 + its group) forwarded to the VM's sshd: something always listens there,
so connecting to it says whether the forward is loaded, without root.

Everything is a file on the dev machine (root-owned, world-readable — checking
them needs no sudo):

- /etc/anci/fwd/<box>-<env>.nft          one env's ruleset; its header records its
                                         group (the registry of taken groups)
- /usr/local/libexec/anci-fwd            loads every ruleset, after enabling
                                         route_localnet on its bridge
- /etc/libvirt/hooks/network.d/anci-fwd  re-runs it when libvirt starts a network
                                         (boot) — nft tables and sysctls don't persist
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

from . import box, errors, paths, ui
from .conf import Conf, allocate_ports
from .render.fwd import APPLY_SCRIPT, FWD_DIR, HOOK_FILE, render_apply_script, render_hook, render_ruleset

BASE = 20000
GROUP_SIZE = 10      # = allocate_ports' max exes per env: one port per slot
GROUPS = 100
CANARY_BASE = 21000  # + the env's group → the VM's sshd (see healthy)
APP_BASE, APP_STRIDE = 8000, 10   # allocate_ports' defaults

_GROUP_RE = re.compile(r"^# group (\d+)$", re.M)
_BRIDGE_RE = re.compile(r"^# bridge (\S+)$", re.M)


def _key(conf: Conf, env_name: str) -> str:
    return f"{paths.box_id(conf)}-{env_name}"


def ruleset_path(conf: Conf, env_name: str, fwd_dir: str = FWD_DIR) -> Path:
    return Path(fwd_dir) / f"{_key(conf, env_name)}.nft"


def _table(conf: Conf, env_name: str) -> str:
    return "anci_fwd_" + re.sub(r"[^a-z0-9]", "_", _key(conf, env_name).lower())


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _group_of(path: Path) -> int | None:
    m = _GROUP_RE.search(_read(path) or "")
    return int(m.group(1)) if m else None


def group(conf: Conf, env_name: str, fwd_dir: str = FWD_DIR) -> int:
    """The env's port group: the one its ruleset records, else the lowest free one."""
    mine = ruleset_path(conf, env_name, fwd_dir)
    own = _group_of(mine)
    if own is not None:
        return own
    taken = {_group_of(p) for p in Path(fwd_dir).glob("*.nft") if p != mine}
    for g in range(GROUPS):
        if g not in taken:
            return g
    raise errors.EnvError(
        f"no localhost ports left for env '{env_name}'",
        why=f"anci's {GROUPS} groups of localhost ports ({BASE}–{BASE + GROUPS * GROUP_SIZE - 1}) "
            f"are taken by other local envs",
        try_=f"remove an old env's ruleset from {fwd_dir}/",
    )


def _slots(conf: Conf, env_name: str) -> dict[str, int]:
    """exe -> its app port slot (0–9) on the VM."""
    return {exe: (port - APP_BASE) // APP_STRIDE
            for (exe, env), port in allocate_ports(conf).items() if env == env_name}


def ports_for(conf: Conf, env_name: str, grp: int) -> dict[str, int]:
    """exe -> its localhost port, for the env's port group."""
    return {exe: BASE + grp * GROUP_SIZE + slot for exe, slot in _slots(conf, env_name).items()}


def installed_ports(conf: Conf, env_name: str, fwd_dir: str = FWD_DIR) -> dict[str, int]:
    """exe -> localhost port as installed, or {} when the env has no forward yet."""
    if not conf.envs[env_name].local:
        return {}
    grp = _group_of(ruleset_path(conf, env_name, fwd_dir))
    return {} if grp is None else ports_for(conf, env_name, grp)


def _bridge(vm_ip: str) -> str:
    """The interface this machine reaches the VM through (libvirt's bridge)."""
    try:
        out = subprocess.run(["ip", "-o", "route", "get", vm_ip],
                             capture_output=True, text=True).stdout
    except FileNotFoundError:
        out = ""
    m = re.search(r"\bdev (\S+)", out)
    return m.group(1) if m else "virbr0"


def desired(conf: Conf, env_name: str, grp: int) -> dict[str, tuple[str, str]]:
    """path -> (content, mode) for the env's forward."""
    vm_ip = box.resolve_env_ip(conf, env_name)
    forwards = [(BASE + grp * GROUP_SIZE + slot, APP_BASE + APP_STRIDE * slot)
                for slot in range(GROUP_SIZE)] + [(CANARY_BASE + grp, 22)]
    return {
        str(ruleset_path(conf, env_name)): (
            render_ruleset(_table(conf, env_name), grp, vm_ip, _bridge(vm_ip), forwards), "0644"),
        APPLY_SCRIPT: (render_apply_script(), "0755"),
        HOOK_FILE: (render_hook(), "0755"),
    }


def _connects(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def healthy(conf: Conf, env_name: str, fwd_dir: str = FWD_DIR) -> bool | None:
    """Whether the env's forward is loaded in the kernel — True, False, or None
    when it can't be told (no forward installed, or the VM is down). Listing nft
    needs root, so: the route_localnet sysctl the apply script sets once the table
    loads (a reboot clears it), then the canary port, which reaches the VM's sshd
    only through the forward (a flushed ruleset fails it)."""
    ruleset = _read(ruleset_path(conf, env_name, fwd_dir))
    grp = _group_of(ruleset_path(conf, env_name, fwd_dir))
    m = _BRIDGE_RE.search(ruleset or "")
    if grp is None or not m:
        return None
    if _read(Path(f"/proc/sys/net/ipv4/conf/{m.group(1)}/route_localnet")) != "1\n":
        return False
    if _connects("127.0.0.1", CANARY_BASE + grp):
        return True
    # the canary failed: a broken forward if the VM answers directly, else the VM is down
    return False if _connects(box.resolve_env_ip(conf, env_name), 22) else None


def _have_nft() -> bool:
    # root's tools often live in sbin, which a user's PATH may lack
    path = os.environ.get("PATH", "") + ":/usr/sbin:/sbin:/usr/local/sbin"
    return shutil.which("nft", path=path) is not None


def reconcile(conf: Conf, env_name: str) -> dict[str, int]:
    """Install the env's forward if it's missing or stale, and reload it if the
    kernel lost it (a reboot whose libvirt hook didn't run); returns exe -> localhost
    port ({} for a remote env). Interactive when it writes (sudo asks for your
    password), so it runs before any progress output starts."""
    if not conf.envs[env_name].local:
        return {}
    grp = group(conf, env_name)
    files = desired(conf, env_name, grp)
    changed = [p for p, (text, _) in files.items() if _read(Path(p)) != text]
    if changed or healthy(conf, env_name) is False:
        if not _have_nft():
            raise errors.EnvError(
                "anci needs nftables (`nft`) to forward a local env to localhost",
                try_="install your distro's nftables package",
            )
        lo = BASE + grp * GROUP_SIZE
        print(f"  {ui.yellow('⚠')} forwarding {env_name} to localhost:{lo}–{lo + GROUP_SIZE - 1}")
        from .hypervisor import hypervisor   # sudo -v up front
        hv = hypervisor(conf, env_name)
        for p in changed:
            text, mode = files[p]
            q = shlex.quote
            hv.root(f"mkdir -p {q(p.rsplit('/', 1)[0])} && cat > {q(p)} && chmod {mode} {q(p)}",
                    f"writing {p}", input=text)
        hv.root(APPLY_SCRIPT, "loading the localhost forward")
    return ports_for(conf, env_name, grp)
