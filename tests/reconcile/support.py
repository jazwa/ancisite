"""Shared builders for the reconcile tests.

`CONF` is a minimal valid conf (one user, one local env, one exe, one db, no
replication/host). Tests append the sections they need.
"""

from __future__ import annotations

from pathlib import Path

from anci.conf import allocate_ports, load
from anci.converge import ConvergeContext

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


def make_ctx(conf, *, env_name: str = "dev", binaries=None, statics=None, secrets=(None, ""),
             present_units=None, exe_dirs=None, local_ports=None) -> ConvergeContext:
    return ConvergeContext(
        conf=conf,
        env_name=env_name,
        ports=allocate_ports(conf),
        binaries=binaries or {},
        statics=statics or {},
        secrets=secrets,
        local_ports=local_ports or {},
        present_units=present_units,
        exe_dirs=exe_dirs,
    )


def file_at(contribution, path: str):
    """The FileArtifact at `path` (fails the test if absent)."""
    return next(f for f in contribution.files if f.path == path)


def link_at(contribution, path: str):
    """The LinkArtifact at `path` (fails the test if absent)."""
    return next(l for l in contribution.links if l.path == path)
