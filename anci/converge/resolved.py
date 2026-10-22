"""systemd-resolved: LLMNR off, on every box. resolved answers LLMNR on
0.0.0.0:5355 / [::]:5355 — on a VPS, the internet — and nothing on a box
resolves bare hostnames over multicast (anci reaches boxes by IP; apps use
unicast DNS, unaffected). A box without resolved just carries the file."""

from __future__ import annotations

from .. import layout, ssh
from ..apply import FileArtifact
from .context import AfterApply, DesiredState, ConvergeContext

DROPIN = "/etc/systemd/resolved.conf.d/anci.conf"
UNIT = "systemd-resolved.service"

CONTENT = """\
# rendered by anci — do not edit
[Resolve]
LLMNR=no
"""


def _activate(conn, result, pending=False):
    if DROPIN not in result.changed_files and not pending:
        return None
    # a restart, not a reload: reload re-reads config only on newer systemds
    ssh.sudo(conn, f"if systemctl is-active --quiet {UNIT}; then systemctl restart {UNIT}; fi")
    return None   # box hardening, not a restart worth reporting


def contribute(ctx: ConvergeContext) -> DesiredState:
    return DesiredState(
        name="resolved",
        files=[FileArtifact(path=DROPIN, content=CONTENT, mode="0644")],
        after_apply=[AfterApply(label="resolved", phase=2, run=_activate,
                                marker=f"{layout.VAR_DIR}/.restart-resolved",
                                watches=[DROPIN])],
    )
