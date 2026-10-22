"""A local env's localhost forward: the nft ruleset, and the apply script +
libvirt hook that (re)load every env's ruleset (templates in `files/`)."""

from __future__ import annotations

from . import template

FWD_DIR = "/etc/anci/fwd"
APPLY_SCRIPT = "/usr/local/libexec/anci-fwd"
HOOK_FILE = "/etc/libvirt/hooks/network.d/anci-fwd"


def render_ruleset(table: str, group: int, vm_ip: str, bridge: str,
                   forwards: list[tuple[int, int]]) -> str:
    """One env's table — declared-then-deleted first, so `nft -f` replaces it
    atomically. `forwards`: (localhost port, VM port) pairs."""
    rules = "\n".join(f"        ip daddr 127.0.0.1 tcp dport {lport} dnat to {vm_ip}:{port}"
                      for lport, port in forwards)
    return template("fwd.nft", TABLE=table, GROUP=str(group), BRIDGE=bridge, VM_IP=vm_ip,
                    RULES=rules)


def render_apply_script() -> str:
    return template("fwd-apply.sh", FWD_DIR=FWD_DIR)


def render_hook() -> str:
    """libvirt network hook: reload the forwards when libvirt starts a network
    (the same hook as the edge's)."""
    return template("libvirt-hook.sh", APPLY_SCRIPT=APPLY_SCRIPT)
