"""Orphans: units/configs/exe-dirs the conf no longer declares.

Pure given the guest inventories (`present_units`, `exe_dirs`) that the executor
gathers before planning. Stopping is *box-driven*: every anci-owned unit the
conf no longer wants is stopped and deleted, even ones it never declared — a
removed exe keeps running until the box says otherwise. Data/history of a gone
exe (its release log and app log) is reported, never deleted.
"""

from __future__ import annotations

import shlex

from .. import layout, ssh, units
from ..apply import DeleteArtifact
from ..conf import Conf
from .context import DesiredState, ConvergeContext


def desired_unit_names(conf: Conf, env_name: str) -> set[str]:
    """Every unit this box should have. A scheduled job renders **both** its
    `<exe>.service` (oneshot) and its `<exe>.timer`, so both are desired."""
    names: set[str] = {units.LOGTRIM_SERVICE, units.LOGTRIM_TIMER}   # on every box
    for exe_name in sorted(conf.exes):
        names.add(units.service_name(exe_name))
        if conf.exes[exe_name].resolve_for(env_name).schedule is not None:
            names.add(units.timer_name(exe_name))
    if conf.replicated_dbs(env_name):
        names.add(units.LITESTREAM_UNIT)
    if any(conf.exes[e].resolve_for(env_name).host for e in conf.exes):
        names.add(units.CADDY_UNIT)
    return names


def contribute(ctx: ConvergeContext) -> DesiredState:
    c = DesiredState(name="orphans")
    if ctx.present_units is None or ctx.exe_dirs is None:
        return c   # inventories not gathered: errs toward inaction
    for u in sorted(ctx.present_units - desired_unit_names(ctx.conf, ctx.env_name)):
        c.stops.append(u)
        if u.endswith(".timer"):
            # a timer carries [Install], so it can be un-enabled; a oneshot
            # service has none and disable is a no-op (it errors)
            c.disables.append(u)
        c.deletes += [
            DeleteArtifact(f"/etc/systemd/system/{u}"),   # the systemd link
            DeleteArtifact(f"{layout.UNITS_DIR}/{u}"),     # the rendered unit
        ]
        if u == units.LITESTREAM_UNIT:
            c.deletes.append(DeleteArtifact(layout.LITESTREAM_FILE))
        if u == units.CADDY_UNIT:
            c.deletes.append(DeleteArtifact(layout.CADDY_FILE))
    gone = [d for d in ctx.exe_dirs if d not in ctx.conf.exes]
    c.deletes += [DeleteArtifact(f"{layout.EXE_DIR}/{e}", tree=True) for e in gone]
    if gone and ctx.conn is not None:
        kept = [p for e in gone for p in (f"{layout.RELEASES_DIR}/{e}", layout.log_path(e))]
        c.report = _existing(ctx.conn, kept)
    return c


def _existing(conn: ssh.Conn, paths: list[str]) -> list[str]:
    """Which of these paths exist on the box (one round trip)."""
    found = ssh.facts(conn, {p: f"[ -e {shlex.quote(p)} ] && echo yes" for p in paths})
    return [p for p in paths if p in found]
