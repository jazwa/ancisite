"""VM provisioning via libvirt/QEMU — on this dev machine or on a remote host.

Every box anci provisions lives on a *hypervisor*: this dev machine, or the remote
host at `env.ip`. Both are driven the same way — root shell commands on the
hypervisor (`Hypervisor.root`): locally through `sudo`, remotely through ssh +
passwordless sudo. virsh, the image download, the disk, cloud-init and
virt-install are all just commands run there, so there is one code path.

Idempotent `up`: download the base image once (sha512-verified), copy+resize the
disk, render cloud-init user-data + static network-config from conf, create or
start the domain, wait for the guest's ssh port. `down` shuts the domain down
cleanly (ACPI power button; a hard power-off only if the guest hangs) but keeps
its disk; `up --recreate` destroys and rebuilds from scratch. Failures raise the
error taxonomy.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .. import errors, net, paths, ui, versions as versions_mod
from ..box import is_remote, is_remote_host
from ..ssh import pin_path
from ..hypervisor import Hypervisor, hypervisor
from ..render.cloudinit import render_network_config, render_userdata
from ..conf import Conf, Env, disk_to_qemu, mem_to_kib, mem_to_mib

IMAGES_DIR = "/var/lib/libvirt/images"
CLOUDINIT_DIR = "/var/lib/libvirt/anci-cloudinit"   # a box's rendered cloud-init
SSH_PORT = 22
SHUTDOWN_TIMEOUT = 60   # seconds of ACPI shutdown before pulling the plug


# ---------------------------------------------------------------------------
# domain state


def _domstate(hv: Hypervisor, dom_name: str) -> str | None:
    """The guest's state name ("running", "paused", "shut off"…), or None when
    the domain isn't defined. Call only after _require_daemon(): a dead daemon
    and an absent domain both fail this virsh call."""
    p = hv.virsh("domstate", dom_name, check=False)
    return p.stdout.strip() if p.returncode == 0 else None


def _domain_exists(hv: Hypervisor, dom_name: str) -> bool:
    return hv.virsh("dominfo", dom_name, check=False).returncode == 0


def _domain_running(hv: Hypervisor, dom_name: str) -> bool:
    """True only for state "running" — an exact match on purpose. "paused"
    still exists (and holds its memory) but the box answers nothing."""
    return _domstate(hv, dom_name) == "running"


def _require_daemon(hv: Hypervisor) -> None:
    """Fail cleanly when libvirt (or the remote host) isn't answering, so a
    dead daemon doesn't read as "no box" to the lookups above."""
    p = hv.virsh("uri", check=False)
    if p.returncode == 0:
        return
    if hv.login is None:
        raise errors.EnvError(
            "can't reach the local libvirt daemon",
            try_="is it running? sudo systemctl start libvirtd",
            debug=(p.stderr or "").strip(),
        )
    raise errors.EnvError(
        f"can't reach libvirt on {hv.where}",
        why="anci manages the box over ssh + passwordless sudo as your conf user",
        try_=f"check `ssh {hv.login} sudo -n virsh uri` works. "
             "If it's someone else's box (no libvirt), set mode = 'adopt' for this env",
        debug=(p.stderr or "").strip(),
    )


def _stop_domain(hv: Hypervisor, dom_name: str) -> bool:
    """Stop a running box: ACPI power button first, hard power-off as fallback.

    `virsh shutdown` lets the guest's systemd run a real shutdown instead of
    `destroy`'s instant power-cut mid-write. Returns True when the power-off
    was hard (so the caller can warn about writes that may be lost).
    """
    hv.virsh("shutdown", dom_name)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        if not _domain_running(hv, dom_name):
            return False
        time.sleep(1)
    hv.virsh("destroy", dom_name)  # hung guest: the pull-the-plug behavior
    return True


def _confirm_recreate(env_name: str, *, force: bool = False) -> bool:
    """The y/N gate for `--recreate` — the one command that deletes the disk.

    `--force` is the explicit yes for scripts (same flag `regen` uses).
    Without a terminal and without --force this refuses: an irreversible disk
    decision can't be made from an unattended stdin.
    """
    return ui.confirm(
        "--recreate is a HARD RESET:",
        ["IT DELETES THE BOX'S DISK — ALL DEPLOYED EXES, ALL DATABASES,",
         "ALL SECRETS, LOGS AND RELEASE HISTORY WILL BE GONE. THERE IS NO UNDO."],
        f"recreate {env_name}? continue?",
        force=force,
        no_tty_error=errors.EnvError(
            "--recreate deletes the box's disk — it needs a yes from a terminal",
            why="an irreversible disk decision can't be made non-interactively",
            try_="run it interactively, or pass --force to confirm it in a script",
        ),
    )


# ---------------------------------------------------------------------------
# provisioning steps (all run on the hypervisor)


def _ensure_image(hv: Hypervisor, url: str, dest: str, sha512: str | None) -> str:
    """The base image, downloaded once and sha512-verified. Returns the ui
    step detail: "cached" or "downloaded"."""
    q = shlex.quote
    verify = (f'echo {q(sha512)}"  $d.tmp" | sha512sum -c --status - '
              f'|| {{ rm -f "$d.tmp"; echo "sha512 mismatch for $d" >&2; exit 3; }}\n'
              if sha512 else "")
    out = hv.root(
        "set -e\n"
        f"d={q(dest)}\n"
        '[ -f "$d" ] && { echo cached; exit 0; }\n'
        'mkdir -p "$(dirname "$d")"\n'
        f'curl -fsSL -o "$d.tmp" {q(url)}\n'
        f"{verify}"
        'mv "$d.tmp" "$d"\n'
        "echo downloaded\n",
        "fetching the VM image",
    )
    return "cached" if "cached" in out else "downloaded"


def _put(hv: Hypervisor, path: str, text: str) -> None:
    """Write a root-owned file (mode 600) on the hypervisor."""
    q = shlex.quote
    hv.root(f"mkdir -p {q(posixpath.dirname(path))}; umask 077; cat > {q(path)}",
            f"writing {path}", input=text)


def _ensure_disk(hv: Hypervisor, dom_name: str, base_image: str, size: str) -> str:
    """Copy the base image to this box's disk and resize it once. Never touches
    an existing disk."""
    q = shlex.quote
    disk = f"{IMAGES_DIR}/{dom_name}.qcow2"
    hv.root(f'set -e; d={q(disk)}; [ -f "$d" ] && exit 0; '
            f'cp --reflink=auto {q(base_image)} "$d"; qemu-img resize "$d" {q(size)} >/dev/null',
            "creating the box's disk")
    return disk


def _virt_install(hv: Hypervisor, env: Env, dom_name: str, disk: str,
                  user_data: str, network_config: str, mac: str) -> None:
    argv = [
        "virt-install", "--connect", "qemu:///system",
        "--name", dom_name,
        "--memory", str(mem_to_mib(env.mem)),  # virt-install --memory is MiB (not KiB!)
        "--vcpus", str(env.vcpus),
        "--disk", f"path={disk},format=qcow2,bus=virtio",
        "--os-variant", "debian13",
        # Deterministic MAC: anchors the guest's netplan interface match (see net.py).
        "--network", f"network=default,model=virtio,mac={mac}",
        "--graphics", "vnc", "--noautoconsole", "--import",
        "--cloud-init", f"user-data={user_data},network-config={network_config}",
    ]
    p = hv.run(shlex.join(argv))
    if p.returncode == 0:
        return
    output = ((p.stderr or "") + (p.stdout or ""))[-2000:]
    if "cannot allocate memory" in output.lower():
        raise errors.EnvError(
            f"{hv.where} couldn't give the box its {env.mem} of memory",
            why="something else is using it (or there isn't enough free)",
            try_=f"lower 'mem' under [env.{env.name}]'s vm config in anci.toml, "
                 "or free memory there",
            debug=output,
        )
    if "password is required" in output or p.returncode == 255:
        raise hv.failure(p, "running virt-install")
    raise errors.EnvError("the box didn't start",
                          try_="re-run with --debug for the raw output", debug=output)


def _wait_for_ssh_port(hv: Hypervisor, ip: str, dom_name: str, timeout_s: int = 420) -> None:
    """Poll TCP:22 from the hypervisor until the guest answers (no auth check —
    that needs user keys). The box is on the hypervisor's NAT, so the probe runs
    there — the dev machine can't always reach it."""
    probe = f"timeout 3 bash -c 'cat < /dev/null > /dev/tcp/{ip}/{SSH_PORT}'"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if hv.run(probe).returncode == 0:
            return
        time.sleep(3)
    raise errors.EnvError(
        f"your box isn't answering after {timeout_s}s",
        why="the first boot runs cloud-init and can be slow — but this may be stuck",
        try_=f"watch it boot: {hv.hint(f'virsh console {dom_name}')}",
    )


# ---------------------------------------------------------------------------
# VM resize reconciliation: bring vcpus / mem / disk to the conf
#
# `up` and `migrate` (via `vm_reconcile`) call this. Live where the
# hypervisor allows it; config-only (next restart) when a maximum has to be
# raised; disk grows only. The guest-side filesystem grow rides converge (see
# `converge/disk.py`).

_QEMU_UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
_MEM_UNITS = {"kib": 1, "mib": 1024, "gib": 1024 ** 2, "tib": 1024 ** 3}


def _qemu_size_bytes(size: str) -> int:
    """'20G'/'512M' (qemu-img form) -> bytes."""
    s = size.strip().lower()
    return int(s[:-1]) * _QEMU_UNITS[s[-1]]


def _kib(el) -> int:
    unit = (el.get("unit") or "KiB").lower()
    return int(el.text) * _MEM_UNITS.get(unit, 1)


@dataclass
class DomainFacts:
    """The domain's live/persistent size facts, as read from libvirt."""

    running: bool
    vcpus: int
    max_vcpus: int
    mem_kib: int
    max_mem_kib: int
    disk_path: str | None = None
    disk_bytes: int | None = None


def _disk_capacity(hv: Hypervisor, dom_name: str, path: str, running: bool) -> int:
    """Block-device capacity in bytes.

    While running, read it via `virsh domblkinfo` — qemu holds the image lock,
    so `qemu-img info` on the file would fail. Stopped, the file is unlocked.
    """
    if running:
        out = hv.virsh("domblkinfo", dom_name, path).stdout
        for line in out.splitlines():
            if line.strip().lower().startswith("capacity:"):
                return int(line.split(":", 1)[1].strip())
        raise errors.EnvError(
            f"couldn't read the size of the box's disk ({path})",
            why="`virsh domblkinfo` returned nothing usable",
            try_="re-run with --debug for the raw output",
        )
    out = hv.root(f"qemu-img info --output=json {shlex.quote(path)}",
                  f"reading the size of the box's disk ({path})")
    return int(json.loads(out)["virtual-size"])


def _domain_facts(hv: Hypervisor, dom_name: str) -> DomainFacts:
    root = ET.fromstring(hv.virsh("dumpxml", dom_name).stdout)
    running = _domain_running(hv, dom_name)
    vcpu = root.find("vcpu")
    max_vcpus = int(vcpu.text) if vcpu is not None else 1
    cur_vcpus = int(vcpu.get("current", max_vcpus)) if vcpu is not None else max_vcpus
    mem_el = root.find("memory")
    max_mem = _kib(mem_el) if mem_el is not None else 0
    cmem = root.find("currentMemory")
    mem = _kib(cmem) if cmem is not None else max_mem
    disk_path = None
    for d in root.findall(".//disk"):
        if d.get("device") == "disk":
            src = d.find("source")
            if src is not None and src.get("file"):
                disk_path = src.get("file")
                break
    disk_bytes = _disk_capacity(hv, dom_name, disk_path, running) if disk_path else None
    return DomainFacts(running, cur_vcpus, max_vcpus, mem, max_mem, disk_path, disk_bytes)


@dataclass
class ResizePlan:
    """What to change. `*_max` fields are config-only; `*_config` forces the
    current value to config-only too (a max was raised, so it can't go live)."""

    vcpus: int | None = None
    vcpus_max: int | None = None
    vcpus_config: bool = False
    mem_kib: int | None = None
    mem_max_kib: int | None = None
    mem_config: bool = False
    disk_bytes: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any((self.vcpus, self.vcpus_max, self.mem_kib, self.mem_max_kib,
                    self.disk_bytes))

    def describe(self) -> str:
        parts = []
        if self.vcpus is not None:
            parts.append(f"{self.vcpus} vcpus")
        if self.mem_kib is not None:
            parts.append(f"{self.mem_kib // 1024} MiB")
        if self.disk_bytes is not None:
            parts.append(f"{self.disk_bytes // (1024 ** 3)} GiB disk")
        return ", ".join(parts)


def _resize_plan(env: Env, facts: DomainFacts) -> ResizePlan:
    """Diff the conf against the domain. Raises on a disk shrink."""
    plan = ResizePlan()
    if env.vcpus != facts.vcpus:
        plan.vcpus = env.vcpus
        if env.vcpus > facts.max_vcpus:
            plan.vcpus_max = env.vcpus
            plan.vcpus_config = True
            plan.notes.append("vcpus max raised — takes effect on next restart")
    target_mem = mem_to_kib(env.mem)
    if target_mem != facts.mem_kib:
        plan.mem_kib = target_mem
        if target_mem > facts.max_mem_kib:
            plan.mem_max_kib = target_mem
            plan.mem_config = True
            plan.notes.append("memory max raised — takes effect on next restart")
    if facts.disk_path and facts.disk_bytes is not None:
        target = _qemu_size_bytes(disk_to_qemu(env.disk))
        if target < facts.disk_bytes:
            raise errors.EnvError(
                f"can't shrink the disk to {env.disk}",
                why=f"the box's disk is already {facts.disk_bytes // (1024 ** 3)} GiB"
                    " (disks only grow)",
                try_="raise 'disk' under the env's vm config, or use --recreate",
            )
        if target > facts.disk_bytes:
            plan.disk_bytes = target
    return plan


def _set_live_or_config(hv: Hypervisor, cmd: str, dom_name: str, value: int,
                        live: bool, what: str, notes: list[str]) -> None:
    """`virsh setvcpus|setmem`; if the live change isn't supported, config-only."""
    try:
        hv.virsh(cmd, dom_name, str(value),
                 *(["--live", "--config"] if live else ["--config"]))
    except errors.EnvError:
        if not live:
            raise
        hv.virsh(cmd, dom_name, str(value), "--config")
        notes.append(f"{what}: live change isn't supported here — takes effect on next restart")


def _apply_resize(hv: Hypervisor, dom_name: str, plan: ResizePlan, facts: DomainFacts) -> None:
    if plan.vcpus_max is not None:
        hv.virsh("setvcpus", dom_name, str(plan.vcpus_max), "--maximum", "--config")
    if plan.mem_max_kib is not None:
        hv.virsh("setmaxmem", dom_name, str(plan.mem_max_kib), "--config")
    if plan.vcpus is not None:
        _set_live_or_config(hv, "setvcpus", dom_name, plan.vcpus,
                            facts.running and not plan.vcpus_config, "vcpus", plan.notes)
    if plan.mem_kib is not None:
        _set_live_or_config(hv, "setmem", dom_name, plan.mem_kib,
                            facts.running and not plan.mem_config, "memory", plan.notes)
    if plan.disk_bytes is not None:
        if facts.running:
            hv.virsh("blockresize", dom_name, facts.disk_path, str(plan.disk_bytes))
        else:
            hv.root(f"qemu-img resize {shlex.quote(facts.disk_path)} {plan.disk_bytes}",
                    f"growing the box's disk ({facts.disk_path})")


def _facts_summary(facts: DomainFacts) -> str:
    parts = [f"{facts.vcpus} vcpus", f"{facts.mem_kib // 1024} MiB"]
    if facts.disk_bytes is not None:
        parts.append(f"{facts.disk_bytes // (1024 ** 3)} GiB disk")
    else:
        parts.append("disk ?")
    return ", ".join(parts)


def _reconcile_domain(hv: Hypervisor, env: Env, dom_name: str) -> tuple[bool, str]:
    """Read → plan → apply the VM size diff. Returns (changed, summary)."""
    facts = _domain_facts(hv, dom_name)
    plan = _resize_plan(env, facts)
    if not plan.changed:
        return False, _facts_summary(facts)
    _apply_resize(hv, dom_name, plan, facts)
    note = f" ({'; '.join(plan.notes)})" if plan.notes else ""
    return True, plan.describe() + note


def _require_running(env_name: str, state: str) -> None:
    """Refuse `migrate` when the box isn't running."""
    if state == "running":
        return
    if state == "absent":
        raise errors.EnvError(
            f"there's no box for {env_name} yet",
            why="migrate converges an existing box; it doesn't create one",
            try_=f"anci {env_name} up",
        )
    raise errors.EnvError(
        f"the box is {state}, not running",
        why="migrate converges a running box; it doesn't start it",
        try_=f"anci {env_name} up",
    )


def require_running(conf: Conf, env_name: str) -> None:
    """Refuse a managed box that isn't running, fast — so `migrate` fails at once
    instead of waiting out ssh's retries. One read-only `virsh domstate`: no
    resize, and locally no sudo (a `libvirt` group member may read domain
    state). When the state can't be read (no permission, daemon down) this says
    nothing and ssh reports the problem. Adopted boxes: no-op."""
    env = conf.envs.get(env_name)
    if env is None or is_remote(env):
        return
    dom_name = net.domain_name(paths.box_id(conf), env.name)
    args = ["virsh", "--connect", "qemu:///system", "--quiet", "domstate", dom_name]
    if is_remote_host(env):
        p = hypervisor(conf, env_name).run(shlex.join(args))
    else:
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return
    if p.returncode == 0:
        _require_running(env_name, p.stdout.strip() or "unknown")
    elif "failed to get domain" in (p.stderr or ""):
        _require_running(env_name, "absent")


def vm_reconcile(conf: Conf, env_name: str) -> None:
    """Resize a managed box to the conf, and refuse one that isn't running.
    Adopted boxes: no-op (no hypervisor)."""
    env = conf.envs.get(env_name)
    if env is None or is_remote(env):
        return
    hv = hypervisor(conf, env_name)
    _require_daemon(hv)
    dom_name = net.domain_name(paths.box_id(conf), env.name)
    if not _domain_exists(hv, dom_name):
        _require_running(env_name, "absent")
    state = _domstate(hv, dom_name) or "unknown"
    _require_running(env_name, state)
    changed, summary = _reconcile_domain(hv, env, dom_name)
    if changed:
        print(f"  {ui.green('✓')} resized {dom_name}: {summary}")


# ---------------------------------------------------------------------------
# hypervisor-side registry: which box owns which address (collision detection)
#
# Each box's identity is stamped into its libvirt domain <description>, so `up`
# can refuse to create a box on an address another project already claimed —
# the one collision a per-name hash can't rule out.

_DESC_RE = re.compile(r"<description>(.*?)</description>", re.S)
_DESC_FIELD_RE = re.compile(r"(\w+)=(\S+)")


def _registry(hv: Hypervisor) -> dict[str, str]:
    """`ip -> "box_id/env"` for every anci domain that carries a description."""
    out: dict[str, str] = {}
    listing = hv.virsh("list", "--all", "--name", check=False)
    if listing.returncode != 0:
        return out
    for name in listing.stdout.split():
        if not name.startswith("anci-"):
            continue
        m = _DESC_RE.search(hv.virsh("dumpxml", name, check=False).stdout or "")
        if not m:
            continue
        fields = dict(_DESC_FIELD_RE.findall(m.group(1)))
        if fields.get("ip"):
            out[fields["ip"]] = f"{fields.get('box_id', '?')}/{fields.get('env', '?')}"
    return out


def _stamp(hv: Hypervisor, dom_name: str, box_id: str, env_name: str, ip: str) -> None:
    """Record the box's identity in its domain description (best-effort)."""
    hv.virsh("desc", dom_name, f"anci box_id={box_id} env={env_name} ip={ip}", check=False)


def _claim_or_fail(hv: Hypervisor, conf: Conf, env_name: str, ip: str) -> None:
    """Hard-error if another box already claims `ip`."""
    owner = _registry(hv).get(ip)
    if owner and owner != f"{paths.box_id(conf)}/{env_name}":
        raise errors.EnvError(
            f"the address {ip} is already claimed by {owner}",
            why="two boxes can't share a static address (one env = one box)",
            try_="rename this env, or remove the other box",
        )


# ---------------------------------------------------------------------------
# the verbs


def _managed_env(conf: Conf, env_name: str) -> Env:
    env = conf.envs.get(env_name)
    if env is None:
        raise errors.ConfError(
            f"env '{env_name}' isn't in anci.toml",
            why=f"defined envs: {', '.join(sorted(conf.envs)) or '(none)'}",
            try_=f"add [env.{env_name}] to anci.toml",
        )
    if is_remote(env):
        raise errors.ConfError(f"env '{env_name}' is mode = 'adopt' — anci provisions no VM for it")
    return env


def env_up(conf: Conf, env_name: str, *, recreate: bool = False,
           force: bool = False) -> str | None:
    """Provision/boot the box for an env from conf. Returns its address, or
    None when the user aborted a --recreate (nothing was touched)."""
    env = _managed_env(conf, env_name)
    hv = hypervisor(conf, env_name)
    _require_daemon(hv)
    entry = versions_mod.get(versions_mod.load(), env.anci_version)

    bid = paths.box_id(conf)
    dom_name = net.domain_name(bid, env.name)
    # Deterministic addressing: the IP is assigned, never discovered — an
    # address on the hypervisor's default NAT.
    ip = net.env_ip(bid, env.name)
    mac = net.stable_mac(bid, env.name)
    _claim_or_fail(hv, conf, env_name, ip)
    where = "" if hv.login is None else f" on {hv.where}"
    print(
        f"\n  {ui.bold(env_name)}  "
        f"{ui.dim(f'{env.vcpus} vcpus · {env.mem} · {env.disk} · stack {env.anci_version}')}"
    )
    print(f"  {ui.dim(f'assigned address {ip}{where}')}\n")

    pipe = ui.Pipeline()
    base_image = f"{IMAGES_DIR}/{entry.debian_image_url.rsplit('/', 1)[-1]}"
    step = pipe.step("base image")
    with ui.Spinner(f"fetching the VM image{where}"):
        detail = _ensure_image(hv, entry.debian_image_url, base_image, entry.sha512)
    step.done(detail)

    # Render cloud-init from conf BEFORE creating the domain; it's written to
    # the hypervisor (CLOUDINIT_DIR), where virt-install reads it.
    user_data = render_userdata(conf.users_for_env(env_name))
    network_config = render_network_config(f"{ip}/24", mac, net.DEFAULT_GATEWAY)

    exists = _domain_exists(hv, dom_name)
    # ask BEFORE the step starts: a step rewrites its own line on completion by
    # moving the cursor up one, so anything printed while it runs gets erased
    if exists and recreate and not _confirm_recreate(env_name, force=force):
        print(f"  {ui.dim('aborted — nothing changed')}")
        return None
    step = pipe.step("creating box")
    after_step: list[str] = []   # printed once the step line is final (see above)
    if exists and recreate:
        if _domain_running(hv, dom_name):
            hv.virsh("destroy", dom_name)
        hv.virsh("undefine", dom_name)
        hv.root(f"rm -f {shlex.quote(f'{IMAGES_DIR}/{dom_name}.qcow2')}", "deleting the box's disk")
        exists = False
        # the rebuilt box gets a new host key: anci rebuilt it, so the old pin is
        # stale by construction (a dev-machine VM is never pinned)
        pin = pin_path(conf, env_name)
        if is_remote_host(env) and pin.is_file():
            pin.unlink()
            after_step.append(f"  {ui.yellow('⚠')} dropped the old host key — the next connect "
                              f"re-pins it; commit the new {pin.relative_to(paths.project_root(conf))}")

    if not exists:
        ud = f"{CLOUDINIT_DIR}/{dom_name}-user-data"
        nc = f"{CLOUDINIT_DIR}/{dom_name}-network-config"
        _put(hv, ud, user_data)
        _put(hv, nc, network_config)
        disk = _ensure_disk(hv, dom_name, base_image, disk_to_qemu(env.disk))
        _virt_install(hv, env, dom_name, disk, ud, nc, mac)
        step.done()
    elif not _domain_running(hv, dom_name):
        hv.virsh("start", dom_name)
        step.done("started")
    else:
        step.skip("already running")
    for line in after_step:
        print(line)
    _stamp(hv, dom_name, bid, env_name, ip)

    # reconcile the VM size to the conf (live where possible)
    step = pipe.step("resizing box")
    changed, summary = _reconcile_domain(hv, env, dom_name)
    if changed:
        step.done(summary)
    else:
        step.skip(f"matches ({summary})")

    # Readiness: TCP:22 on the assigned address. cloud-init keeps running in the
    # background; the converge that follows waits for it (ssh.wait_ready).
    step = pipe.step("booting")
    with ui.Spinner("waiting for the box to answer ssh"):
        _wait_for_ssh_port(hv, ip, dom_name)
    step.done("reachable")
    pipe.done()
    return ip


def env_down(conf: Conf, env_name: str) -> None:
    """Stop the env's box, keep the disk (idempotent)."""
    env = _managed_env(conf, env_name)
    hv = hypervisor(conf, env_name)
    _require_daemon(hv)
    dom_name = net.domain_name(paths.box_id(conf), env.name)
    if not _domain_exists(hv, dom_name):
        print(f"  {ui.dim(f'no box for {env_name} — nothing to stop')}")
        return
    if not _domain_running(hv, dom_name):
        print(f"  {ui.dim(f'{dom_name} already stopped')}")
        return
    hard = _stop_domain(hv, dom_name)
    if _domain_running(hv, dom_name):
        raise errors.EnvError(
            f"{dom_name} isn't stopping",
            why="an ACPI shutdown and a hard power-off both failed",
            try_=f"force it manually: {hv.hint(f'virsh destroy {dom_name}')}",
        )
    if hard:
        print(
            f"  {ui.yellow('⚠')} {dom_name} didn't stop cleanly in "
            f"{SHUTDOWN_TIMEOUT}s — FORCED power-off; recent writes may not have hit the disk"
        )
    print(f"  {ui.green('✓')} stopped {dom_name} (disk kept)")
