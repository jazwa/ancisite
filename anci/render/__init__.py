"""Artifact rendering — every file anci writes, one module per family.

    systemd.py    app units, job oneshot + timer, the caddy/litestream units
    cloudinit.py  cloud-init user-data + network-config (a box's first boot)
    caddy.py      the Caddyfile
    litestream.py litestream.yaml
    shell.py      the root scripts converge/`up` run (accounts, layout)
    edge.py       a remote host's DNAT edge (nft ruleset, scripts)
    fwd.py        a local env's localhost forward (nft ruleset, scripts)

Pure functions of conf-resolved data: golden-testable without a box. YAML is
emitted as JSON (valid YAML, quoting correct by construction); longer scripts
live as real files under `files/`, filled by `template()`.
"""

from __future__ import annotations

import re
from importlib import resources

_PLACEHOLDER = re.compile(r"@[A-Z_]+@")


def template(name: str, **values: str) -> str:
    """`files/<name>` with each `@KEY@` replaced by `values[KEY]`."""
    text = resources.files(__package__).joinpath("files", name).read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    left = _PLACEHOLDER.search(text)
    if left:
        raise KeyError(f"template {name}: no value for {left.group()}")
    return text
