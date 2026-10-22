"""The DNAT edge: public serving for a box on a remote hypervisor host.

A box anci runs on a remote libvirt host sits on that host's private `default`
NAT. To serve its `host` sites, the host forwards inbound :80/:443 (tcp, plus
udp/443 for HTTP/3) to the box in the kernel — the client address survives and
Caddy in the box is unchanged (its ACME challenges come straight through).

One public box per host: packet forwarding can't route by hostname, so a second
env claiming the host's 80/443 is a hard error.

Everything is a file on the host (root-owned, written through the host's
`Hypervisor` — ssh as the conf user + passwordless sudo):

- /etc/anci/edge/route           who owns the edge + where it points (sourceable)
- /etc/anci/edge/anci-edge.nft   the DNAT ruleset (atomic reload)
- /usr/local/libexec/anci-edge   applies both + punches libvirt's forward reject
- /etc/libvirt/hooks/network.d/anci-edge   re-runs it when libvirt rebuilds its
                                 rules (boot, daemon restart) — the nft table
                                 isn't persistent and libvirt's rules come back
"""

from __future__ import annotations

import hashlib
import shlex

from . import errors, net, paths, ssh
from .render import caddy
from .render.edge import (
    APPLY_SCRIPT, HOOK_FILE, ROUTE_FILE, RULESET_FILE,
    render_apply_script, render_hook, render_route, render_ruleset,
)
from .conf import Conf, allocate_ports
from .hypervisor import Hypervisor


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _read_owner(route_lines: list[str]) -> str | None:
    for line in route_lines:
        if line.startswith("OWNER="):
            parts = shlex.split(line[len("OWNER="):])
            return parts[0] if parts else None
    return None


def _bridge(hv: Hypervisor) -> str:
    """The `default` network's bridge (virbr0 unless someone renamed it)."""
    for line in (hv.virsh("net-info", "default", check=False).stdout or "").splitlines():
        if line.strip().lower().startswith("bridge:"):
            return line.split(":", 1)[1].strip() or "virbr0"
    return "virbr0"


def reconcile(conf: Conf, env_name: str, user: str) -> str | None:
    """Make the env's host forward :80/:443 to its box iff the box serves a
    `host`. Returns a step detail, or None when there's nothing to publish."""
    env = conf.envs[env_name]
    hv = Hypervisor(f"{user}@{env.ip}")
    bid = paths.box_id(conf)
    owner = f"{bid}/{env_name}"
    vm_ip = net.env_ip(bid, env_name)
    managed = (ROUTE_FILE, RULESET_FILE, APPLY_SCRIPT, HOOK_FILE)
    found = hv.probe({"route": f"cat {ROUTE_FILE}",
                      **{p: f"sha256sum {p} | cut -c1-64" for p in managed}})
    current_owner = _read_owner(found.get("route", []))
    if current_owner and current_owner != owner:
        raise errors.EnvError(
            f"{env.ip} already publishes {current_owner}'s box on 80/443",
            why="one public box per host (the host forwards its ports to a single box)",
            try_="remove `host` from one env's exes, or move one env to another host",
        )

    if not caddy.caddy_sites(conf, env_name, allocate_ports(conf)):
        if current_owner == owner:
            hv.root(f"{APPLY_SCRIPT} down", "removing the host's edge")
            return "removed"
        return None

    desired = {
        ROUTE_FILE: (render_route(owner, net.domain_name(bid, env_name), vm_ip, _bridge(hv)), "0644"),
        RULESET_FILE: (render_ruleset(vm_ip), "0644"),
        APPLY_SCRIPT: (render_apply_script(), "0755"),
        HOOK_FILE: (render_hook(), "0755"),
    }
    changed = [p for p, (text, _) in desired.items() if ssh.fact(found, p) != _sha(text)]
    q = shlex.quote
    for path in changed:
        text, mode = desired[path]
        hv.root(f"mkdir -p {q(path.rsplit('/', 1)[0])} && cat > {q(path)} && chmod {mode} {q(path)}",
                f"writing {path} on the host", input=text)
    if HOOK_FILE in changed:
        # libvirt only scans its hooks dir at daemon start; running guests survive
        hv.root("systemctl try-restart libvirtd virtnetworkd", "reloading libvirt's hooks")
    # always (re)apply: cheap, and heals rules libvirt rebuilt without us
    hv.root(APPLY_SCRIPT, "applying the host's edge")
    return f"80/443 → {vm_ip}" if changed else "unchanged"
