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

from .. import addresses, errors, net, paths, ui, versions as versions_mod
from ..box import is_remote_host
from ..ssh import pin_path
from ..hypervisor import VIRSH, Hypervisor, hypervisor, libvirt_hypervisor
from ..render.cloudinit import render_network_config, render_userdata
from ..conf import Conf, Env, disk_to_qemu, mem_to_kib, mem_to_mib

IMAGES_DIR = "/var/lib/libvirt/images"
CLOUDINIT_DIR = "/var/lib/libvirt/anci-cloudinit"
SSH_PORT = 22
SHUTDOWN_TIMEOUT = 60


def _domstate(hv: Hypervisor, dom_name: str) -> str | None:
    p = hv.virsh("domstate", dom_name, check=False)
    return p.stdout.strip() if p.returncode == 0 else None


def _domain_exists(hv: Hypervisor, dom_name: str) -> bool:
    return hv.virsh("dominfo", dom_name, check=False).returncode == 0


def _domain_running(hv: Hypervisor, dom_name: str) -> bool:
    return _domstate(hv, dom_name) == "running"


def _require_daemon(hv: Hypervisor) -> None:
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
        try_=f"check `ssh {hv.login} sudo -n virsh uri` works, and that the host has libvirt installed",
        debug=(p.stderr or "").strip(),
    )


def _stop_domain(hv: Hypervisor, dom_name: str) -> bool:
    hv.virsh("shutdown", dom_name)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        if not _domain_running(hv, dom_name):
            return False
        time.sleep(1)
    hv.virsh("destroy", dom_name)
    return True


def _confirm_recreate(env_name: str, *, force: bool = False) -> bool:
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


def _ensure_image(hv: Hypervisor, url: str, dest: str, sha512: str | None) -> str:
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
    q = shlex.quote
    hv.root(f"mkdir -p {q(posixpath.dirname(path))}; umask 077; cat > {q(path)}",
            f"writing {path}", input=text)


def _ensure_disk(hv: Hypervisor, dom_name: str, base_image: str, size: str) -> str:
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
        "--memory", str(mem_to_mib(env.mem)),
        "--vcpus", str(env.vcpus),
        "--disk", f"path={disk},format=qcow2,bus=virtio",
        "--os-variant", "debian13",
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


_QEMU_UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
_MEM_UNITS = {"kib": 1, "mib": 1024, "gib": 1024 ** 2, "tib": 1024 ** 3}


def _qemu_size_bytes(size: str) -> int:
    s = size.strip().lower()
    return int(s[:-1]) * _QEMU_UNITS[s[-1]]


def _kib(el) -> int:
    unit = (el.get("unit") or "KiB").lower()
    return int(el.text) * _MEM_UNITS.get(unit, 1)


@dataclass
class DomainFacts:

    running: bool
    vcpus: int
    max_vcpus: int
    mem_kib: int
    max_mem_kib: int
    disk_path: str | None = None
    disk_bytes: int | None = None


def _disk_capacity(hv: Hypervisor, dom_name: str, path: str, running: bool) -> int:
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


def _domain_facts(hv: Hypervisor, dom_name: str, *, disk_optional: bool = False) -> DomainFacts:
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
    disk_bytes = None
    if disk_path:
        try:
            disk_bytes = _disk_capacity(hv, dom_name, disk_path, running)
        except errors.EnvError:
            if not disk_optional:
                raise
    return DomainFacts(running, cur_vcpus, max_vcpus, mem, max_mem, disk_path, disk_bytes)


@dataclass
class ResizePlan:

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
            hv.virsh("blockresize", dom_name, facts.disk_path, f"{plan.disk_bytes}B")
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


def vm_differences(conf: Conf, env_name: str) -> dict[str, tuple[str, str]]:
    env = conf.envs.get(env_name)
    if env is None:
        return {}
    hv = hypervisor(conf, env_name) if is_remote_host(env) else Hypervisor(sudo=False)
    dom_name = net.domain_name(paths.box_id(conf), env.name)
    try:
        facts = _domain_facts(hv, dom_name, disk_optional=True)
    except (errors.EnvError, ET.ParseError):
        return {}
    plan = _resize_plan(env, facts)
    out = {}
    if plan.vcpus is not None:
        out["vcpus"] = (str(facts.vcpus), str(plan.vcpus))
    if plan.mem_kib is not None:
        out["mem"] = (f"{facts.mem_kib // 1024} MiB", f"{plan.mem_kib // 1024} MiB")
    if plan.disk_bytes is not None:
        out["disk"] = (f"{facts.disk_bytes // (1024 ** 3)} GiB", f"{plan.disk_bytes // (1024 ** 3)} GiB")
    return out


def _reconcile_domain(hv: Hypervisor, env: Env, dom_name: str) -> tuple[bool, str]:
    facts = _domain_facts(hv, dom_name)
    plan = _resize_plan(env, facts)
    if not plan.changed:
        return False, _facts_summary(facts)
    _apply_resize(hv, dom_name, plan, facts)
    note = f" ({'; '.join(plan.notes)})" if plan.notes else ""
    return True, plan.describe() + note


def _require_running(env_name: str, state: str) -> None:
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
    env = conf.envs.get(env_name)
    if env is None:
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
    env = conf.envs.get(env_name)
    if env is None:
        return
    hv = libvirt_hypervisor(conf, env_name)
    _require_daemon(hv)
    dom_name = net.domain_name(paths.box_id(conf), env.name)
    if not _domain_exists(hv, dom_name):
        _require_running(env_name, "absent")
    state = _domstate(hv, dom_name) or "unknown"
    _require_running(env_name, state)
    changed, summary = _reconcile_domain(hv, env, dom_name)
    if changed:
        print(f"  {ui.green('✓')} resized {dom_name}: {summary}")


_DESC_FIELD_RE = re.compile(r"(\w+)=(\S+)")

# every anci box on the host and its description, one `<name>\t<description>`
# line each — a single round trip however many boxes there are
_REGISTRY_SCRIPT = f"""set -o pipefail
{VIRSH} list --all --name | while read -r d; do
  case "$d" in anci-*) printf '%s\\t%s\\n' "$d" "$({VIRSH} desc "$d" | tr '\\n' ' ')" ;; esac
done
"""


def _registry(hv: Hypervisor) -> dict[str, str]:
    out: dict[str, str] = {}
    p = hv.run(_REGISTRY_SCRIPT)
    if p.returncode != 0:
        return out
    for line in p.stdout.splitlines():
        fields = dict(_DESC_FIELD_RE.findall(line.partition("\t")[2]))
        if fields.get("ip"):
            out[fields["ip"]] = f"{fields.get('box_id', '?')}/{fields.get('env', '?')}"
    return out


def _stamp(hv: Hypervisor, dom_name: str, box_id: str, env_name: str, ip: str) -> None:
    hv.virsh("desc", dom_name, f"anci box_id={box_id} env={env_name} ip={ip}", check=False)


def _assign_address(hv: Hypervisor, conf: Conf, env_name: str) -> tuple[str, bool]:
    """This env's address on the host, and whether it was newly picked.

    The recorded address wins; then the one stamped on this env's existing VM (a
    box made before addresses were recorded, or a record that wasn't committed);
    otherwise the first address no anci box on the host holds.
    """
    registry = _registry(hv)
    me = f"{paths.box_id(conf)}/{env_name}"
    ip = addresses.recorded(conf, env_name)
    if ip is None:
        ip = next((a for a, owner in registry.items() if owner == me), None)
        if ip is None:
            ip = net.first_free(registry)
            if ip is None:
                raise errors.EnvError(
                    f"no free address for {env_name} on {hv.where}",
                    why=f"anci's {net.STATIC_POOL} addresses there are all taken by other boxes",
                    try_="remove a box you no longer use",
                )
            return ip, True
    owner = registry.get(ip)
    if owner and owner != me:
        raise errors.EnvError(
            f"the address {ip} is already claimed by {owner}",
            why="two boxes can't share an address on one host",
            try_=f"delete {addresses.address_path(conf, env_name)} and re-run "
                 f"`anci {env_name} up` to pick a free one",
        )
    return ip, False


def _managed_env(conf: Conf, env_name: str) -> Env:
    env = conf.envs.get(env_name)
    if env is None:
        raise errors.ConfError(
            f"env '{env_name}' isn't in anci.toml",
            why=f"defined envs: {', '.join(sorted(conf.envs)) or '(none)'}",
            try_=f"add [env.{env_name}] to anci.toml",
        )
    return env


def env_up(conf: Conf, env_name: str, *, recreate: bool = False,
           force: bool = False, pipe: ui.Pipeline | None = None) -> str | None:
    env = _managed_env(conf, env_name)
    hv = hypervisor(conf, env_name)
    _require_daemon(hv)
    entry = versions_mod.get(versions_mod.load(), env.anci_version)

    bid = paths.box_id(conf)
    dom_name = net.domain_name(bid, env.name)
    mac = net.stable_mac(bid, env.name)
    ip, new_address = _assign_address(hv, conf, env_name)
    recorded_at = addresses.record(conf, env_name, ip)
    where = "" if hv.login is None else f" on {hv.where}"
    print(
        f"\n  {ui.bold(env_name)}  "
        f"{ui.dim(f'{env.vcpus} vcpus · {env.mem} · {env.disk} · stack {env.anci_version}')}"
    )
    print(f"  {ui.dim(f'address {ip}{where}')}")
    if new_address and is_remote_host(env):
        print(f"  {ui.dim(f'recorded in {recorded_at.relative_to(paths.project_root(conf))} — commit it')}")
    print()

    own = pipe is None
    pipe = pipe or ui.Pipeline()
    base_image = f"{IMAGES_DIR}/{entry.debian_image_url.rsplit('/', 1)[-1]}"
    step = pipe.step("base image")
    with ui.Spinner(f"fetching the VM image{where}"):
        detail = _ensure_image(hv, entry.debian_image_url, base_image, entry.sha512)
    step.done(detail)

    user_data = render_userdata(conf.users_for_env(env_name))
    network_config = render_network_config(f"{ip}/24", mac, net.DEFAULT_GATEWAY)

    exists = _domain_exists(hv, dom_name)
    if exists and recreate and not _confirm_recreate(env_name, force=force):
        print(f"  {ui.dim('aborted — nothing changed')}")
        return None
    step = pipe.step("creating box")
    after_step: list[str] = []
    if exists and recreate:
        if _domain_running(hv, dom_name):
            hv.virsh("destroy", dom_name)
        hv.virsh("undefine", dom_name)
        hv.root(f"rm -f {shlex.quote(f'{IMAGES_DIR}/{dom_name}.qcow2')}", "deleting the box's disk")
        exists = False
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

    step = pipe.step("resizing box")
    changed, summary = _reconcile_domain(hv, env, dom_name)
    if changed:
        step.done(summary)
    else:
        step.skip(f"matches ({summary})")

    step = pipe.step("booting")
    with ui.Spinner("waiting for the box to answer ssh"):
        _wait_for_ssh_port(hv, ip, dom_name)
    step.done("reachable")
    if own:
        pipe.done()
    return ip


def env_down(conf: Conf, env_name: str) -> None:
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
