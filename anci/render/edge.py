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
COMMENT = "anci-edge"


def render_ruleset(vm_ip: str) -> str:
    return template("edge.nft", TABLE=TABLE, VM_IP=vm_ip)


def render_route(owner: str, dom_name: str, vm_ip: str, bridge: str) -> str:
    q = shlex.quote
    return (
        f"OWNER={q(owner)}\nDOM={q(dom_name)}\nIP={q(vm_ip)}\nBRIDGE={q(bridge)}\n"
    )


def render_apply_script() -> str:
    return template("edge-apply.sh", ROUTE_FILE=ROUTE_FILE, RULESET_FILE=RULESET_FILE,
                    TABLE=TABLE, COMMENT=COMMENT)


def render_hook() -> str:
    return template("libvirt-hook.sh", APPLY_SCRIPT=APPLY_SCRIPT)
