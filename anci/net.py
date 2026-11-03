"""Guest addressing: static IPs for managed boxes.

Instead of DHCP + lease-file scraping (+ a virsh fallback), anci *assigns* each
box a static address at provision time, rendered into the guest via cloud-init
network-config. `up` picks the first address no other anci box on the host
holds (`first_free`) and records it (`anci.addresses`); every later command
reads it back, with no root and no libvirt connection.

The deterministic MAC is kept only as a netplan interface-match anchor (so a
guest's NIC config survives PCI topology renames); it plays no role in
addressing.

Every managed box lives on its hypervisor's default NAT network (192.168.122.0/24). Static
assignments are drawn from the .50–.113 range to stay clear of dnsmasq's
default dynamic pool.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

DEFAULT_SUBNET = "192.168.122"
DEFAULT_GATEWAY = "192.168.122.1"
STATIC_BASE = 50
STATIC_POOL = 64


def sanitize_domain(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._+-]", "-", name)


def domain_name(box_id: str, env_name: str) -> str:
    return "anci-" + sanitize_domain(f"{box_id}-{env_name}")


def pool() -> list[str]:
    return [f"{DEFAULT_SUBNET}.{STATIC_BASE + i}" for i in range(STATIC_POOL)]


def first_free(taken: Iterable[str]) -> str | None:
    used = set(taken)
    return next((ip for ip in pool() if ip not in used), None)


def stable_mac(box_id: str, env_name: str) -> str:
    h = hashlib.sha256(f"anci-mac:{box_id}:{env_name}".encode()).digest()
    return f"52:54:00:{h[0]:02x}:{h[1]:02x}:{h[2]:02x}"
