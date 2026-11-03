"""Where each env's box lives on its host's private network.

`up` picks the address once (the first one free on the host, see
`providers.libvirt`) and records it here; every other command reads it back,
with no libvirt connection.

- a remote env's address is committed with the project, in
  `envs/<env>/address` next to its `hostkey`, so every deployer reaches the
  same box.
- a local env's VM exists only on this machine, so its address is kept in this
  machine's state dir: `$XDG_STATE_HOME/anci/addresses/<box>-<env>`
  (default `~/.local/state/...`).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import net, paths

_ADDRESS_RE = re.compile(rf"^{re.escape(net.DEFAULT_SUBNET)}\.(\d{{1,3}})$")


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "anci" / "addresses"


def address_path(conf, env_name: str) -> Path:
    if conf.envs[env_name].local:
        return _state_dir() / f"{paths.box_id(conf)}-{env_name}"
    return paths.project_root(conf) / "envs" / env_name / "address"


def recorded(conf, env_name: str) -> str | None:
    try:
        text = address_path(conf, env_name).read_text().strip()
    except OSError:
        return None
    return text if _ADDRESS_RE.match(text) else None


def record(conf, env_name: str, ip: str) -> Path:
    path = address_path(conf, env_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ip + "\n")
    return path
