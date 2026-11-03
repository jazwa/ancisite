"""Guest addressing: deterministic static IPs for managed boxes.

Simplification over the original design: instead of DHCP + lease-file scraping
(+ a virsh fallback), anci *assigns* each box a static address at provision
time, rendered into the guest via cloud-init network-config. Nothing is ever
discovered — `up` prints the IP it assigned, and every later command (deploy,
etc.) knows the address from the box name alone, with no root and no
libvirt connection.

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

DEFAULT_SUBNET = "192.168.122"
DEFAULT_GATEWAY = "192.168.122.1"
STATIC_BASE = 50
STATIC_POOL = 64


def sanitize_domain(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._+-]", "-", name)


def domain_name(box_id: str, env_name: str) -> str:
    return "anci-" + sanitize_domain(f"{box_id}-{env_name}")


def env_ip(box_id: str, env_name: str) -> str:
    h = hashlib.sha256(f"anci-ip:{box_id}:{env_name}".encode()).digest()
    return f"{DEFAULT_SUBNET}.{STATIC_BASE + h[0] % STATIC_POOL}"


def stable_mac(box_id: str, env_name: str) -> str:
    h = hashlib.sha256(f"anci-mac:{box_id}:{env_name}".encode()).digest()
    return f"52:54:00:{h[0]:02x}:{h[1]:02x}:{h[2]:02x}"
