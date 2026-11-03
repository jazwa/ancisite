"""cloud-init for a box's first boot: user-data (accounts) + network-config
(the static address). Emitted as JSON — valid YAML, so quoting is correct by
construction (an ssh key comment can't break the document)."""

from __future__ import annotations

import json

from .shell import sudoers_line


def render_userdata(users: dict[str, list[str]]) -> str:
    if not users:
        raise ValueError("no users: refusing to render a guest nobody can reach")
    doc = {
        "groups": ["anci"],
        "users": [
            {"name": name, "groups": ["anci"],
             "shell": "/bin/bash", "ssh_authorized_keys": list(keys)}
            for name, keys in users.items()
        ],
        "write_files": [
            {"path": f"/etc/sudoers.d/anci-{name}", "content": sudoers_line(name),
             "permissions": "0440"}
            for name in users
        ],
        "ssh_pwauth": False,
    }
    return "#cloud-config\n" + json.dumps(doc, indent=2) + "\n"


def render_network_config(ip_cidr: str, mac: str, gateway: str) -> str:
    doc = {"network": {"version": 2, "ethernets": {"nic": {
        "match": {"macaddress": mac},
        "dhcp4": False,
        "addresses": [ip_cidr],
        "routes": [{"to": "default", "via": gateway}],
        "nameservers": {"addresses": [gateway]},
    }}}}
    return json.dumps(doc, indent=2) + "\n"
