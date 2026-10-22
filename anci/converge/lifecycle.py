"""Stop/start a box's app units outside a converge pass (`regen` needs the
apps quiet while it restores a db, then running again)."""

from __future__ import annotations

import shlex

from .. import layout, ssh, units
from ..conf import Conf


def _present_units(conn: ssh.Conn) -> set[str]:
    """Unit *files* anci owns on the box, so every name handed to systemctl
    resolves (no missing-unit failure from a dangling symlink)."""
    listed = ssh.facts(conn, {"unit": f"ls -1 {layout.UNITS_DIR}"}).get("unit", [])
    return set(layout.split_safe(listed)[0])   # the listing is untrusted: odd names never reach systemctl


def stop_app_units(conn: ssh.Conn) -> None:
    """Stop every anci-owned app unit on the box — the conf's AND any orphan:
    for stopping, the box is the truth (a ghost writer racing a restore
    corrupts it). Mechanism units (litestream, caddy) keep running."""
    to_stop = sorted(u for u in _present_units(conn)
                     if u.endswith((".service", ".timer")) and u not in units.MECHANISM_UNITS)
    if to_stop:
        ssh.sudo(conn, "systemctl stop " + " ".join(map(shlex.quote, to_stop)))


def start_conf_units(conn: ssh.Conn, conf: Conf, env_name: str) -> None:
    """Start the conf's services and arm its jobs' timers — only those that
    exist on the box (an exe never deployed has no unit to start). A job's
    oneshot service is never started by hand; its timer is."""
    wanted = [units.timer_name(e) if conf.exes[e].resolve_for(env_name).schedule
              else units.service_name(e) for e in sorted(conf.exes)]
    present = _present_units(conn)
    to_start = [u for u in wanted if u in present]
    if to_start:
        ssh.sudo(conn, "systemctl start " + " ".join(map(shlex.quote, to_start)))
