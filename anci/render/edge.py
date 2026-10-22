"""A remote host's DNAT edge: the nft ruleset, the route file, and the apply
script + libvirt hook that (re)load them (templates in `files/`)."""

from __future__ import annotations

import shlex

from . import template

EDGE_DIR = "/etc/anci/edge"
ROUTE_FILE = f"{EDGE_DIR}/route"
RULESET_FILE = f"{EDGE_DIR}/anci-edge.nft"
APPLY_SCRIPT = "/usr/local/libexec/anci-edge"
HOOK_FILE = "/etc/libvirt/hooks/network.d/anci-edge"
TABLE = "anci_edge"
COMMENT = "anci-edge"   # tags the accept rule anci adds to libvirt's filtering


def render_ruleset(vm_ip: str) -> str:
    """The host's DNAT table — declared-then-deleted first, so `nft -f`
    replaces it atomically whether or not it already exists."""
    return template("edge.nft", TABLE=TABLE, VM_IP=vm_ip)


def render_route(owner: str, dom_name: str, vm_ip: str, bridge: str) -> str:
    q = shlex.quote
    return (
        "# rendered by anci — do not edit\n"
        f"OWNER={q(owner)}\nDOM={q(dom_name)}\nIP={q(vm_ip)}\nBRIDGE={q(bridge)}\n"
    )


def render_apply_script() -> str:
    """Idempotent: (re)load the DNAT table and let DNAT'd traffic through
    libvirt's guest filtering. `down` removes both. Must not call virsh: it
    runs inside libvirt's hook."""
    return template("edge-apply.sh", ROUTE_FILE=ROUTE_FILE, RULESET_FILE=RULESET_FILE,
                    TABLE=TABLE, COMMENT=COMMENT)


def render_hook() -> str:
    """libvirt network hook: re-apply the edge when libvirt (re)builds rules
    (the same hook as the localhost forward's)."""
    return template("libvirt-hook.sh", APPLY_SCRIPT=APPLY_SCRIPT)
