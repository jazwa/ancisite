"""App logs: bounded on every box. Each exe's stdout/stderr is appended to
/var/log/anci/<exe>.log (the units' `append:`); an hourly timer copies a log
past LOG_LIMIT to <exe>.log.1 and truncates it, so each exe keeps at most about
twice the limit on disk."""

from __future__ import annotations

from .. import layout, ssh, units
from ..apply import FileArtifact, LinkArtifact
from ..render import systemd, template
from .context import AfterApply, DesiredState, ConvergeContext

LOG_LIMIT = 1 << 30   # 1 GiB per exe log


def render_script() -> str:
    return template("logtrim.sh", LOG_DIR=layout.LOG_DIR, LIMIT=str(LOG_LIMIT))


def contribute(ctx: ConvergeContext) -> DesiredState:
    service = f"{layout.UNITS_DIR}/{units.LOGTRIM_SERVICE}"
    timer = f"{layout.UNITS_DIR}/{units.LOGTRIM_TIMER}"
    timer_link = f"/etc/systemd/system/{units.LOGTRIM_TIMER}"
    paths = [layout.LOGTRIM_SCRIPT, service, timer, timer_link]

    def run(conn, result, pending=False):
        if pending or set(paths) & set(result.changed_files + result.changed_links):
            ssh.sudo(conn, f"systemctl enable --now {units.LOGTRIM_TIMER} >/dev/null")
        return None   # box upkeep, not a restart worth reporting

    return DesiredState(
        name="logtrim",
        files=[
            FileArtifact(path=layout.LOGTRIM_SCRIPT, content=render_script(), mode="0755"),
            FileArtifact(path=service, content=systemd.render_logtrim_service(), mode="0644"),
            FileArtifact(path=timer, content=systemd.render_logtrim_timer(), mode="0644"),
        ],
        links=[LinkArtifact(path=f"/etc/systemd/system/{units.LOGTRIM_SERVICE}", target=service),
               LinkArtifact(path=timer_link, target=timer)],
        after_apply=[AfterApply(label="logtrim", phase=2, run=run,
                                marker=f"{layout.VAR_DIR}/.enable-logtrim", watches=paths)],
    )
