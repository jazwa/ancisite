"""Shared builders for the inspect/apply tests, and running a resource's real
probes against a box laid out in a temp dir.

`CONF` is a minimal valid conf (one user, one local env, one exe, one db, no
replication/host). Tests append the sections they need."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from anci import ssh
from anci.conf import load


def run_probes(resource, env: dict | None = None) -> dict[str, list[str]]:
    """The resource's probes, run by bash the way `ssh.facts` runs them on the box."""
    probes = resource.probes()
    out = subprocess.run(["bash", "-c", ssh.facts_script(probes)],
                         capture_output=True, text=True, check=True, env=env).stdout
    found = ssh.parse_facts(out)
    return {key: found.get(key, []) for key in probes}


def write(path: str, content: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)


def link(path: str, target: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path)


CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.app]
source = './app'

[db.main-db]
migrations = './m'
"""

# A distinct-enough ELF header so sha256 differs between test binaries.
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 20


def load_conf(directory, text: str = CONF):
    p = Path(directory) / "anci.toml"
    p.write_text(text)
    return load(str(p))


def write_source(directory, name: str = "app", data: bytes = ELF) -> Path:
    p = Path(directory) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p
