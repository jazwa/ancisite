"""cloud-init for a box's first boot: user-data (accounts) + network-config
(the static address). Emitted as JSON — valid YAML, so quoting is correct by
construction (an ssh key comment can't break the document)."""

from __future__ import annotations

import json


def render_userdata(users: dict[str, list[str]]) -> str:
    """One guest account per conf user: their own account + `authorized_keys`
    + passwordless sudo, in the shared `anci` group that owns `/var/lib/anci`.
    The shared `anci` *service* user is created later, idempotently, by
    `up`/`deploy`/`migrate` — not here."""
    if not users:
        raise ValueError("no users: refusing to render a guest nobody can reach")
    doc = {
        "groups": ["anci"],
        "users": [
            {"name": name, "groups": ["anci"], "sudo": "ALL=(ALL) NOPASSWD:ALL",
             "shell": "/bin/bash", "ssh_authorized_keys": list(keys)}
            for name, keys in users.items()
        ],
        "ssh_pwauth": False,
        # no packages: sqlite3/litestream/caddy ship vendored on the first
        # converge, and static files go through the blob store over plain ssh
    }
    return "#cloud-config\n" + json.dumps(doc, indent=2) + "\n"


def render_network_config(ip_cidr: str, mac: str, gateway: str) -> str:
    """network-config (v2): a static address, matched by MAC so it survives NIC
    renames — the static IP is why anci never needs to discover a lease."""
    doc = {"network": {"version": 2, "ethernets": {"nic": {
        "match": {"macaddress": mac},
        "dhcp4": False,
        "addresses": [ip_cidr],
        "routes": [{"to": "default", "via": gateway}],
        "nameservers": {"addresses": [gateway]},
    }}}}
    return json.dumps(doc, indent=2) + "\n"
