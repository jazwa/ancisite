"""Caddy: unit + Caddyfile, only when an exe sets a public `host` (its binary
is always shipped: `vendored`)."""

from __future__ import annotations

from ..render import caddy as caddy_mod, systemd
from .. import errors, layout, ssh, units, versions as versions_mod
from ..apply import FileArtifact, LinkArtifact
from .context import AfterApply, DesiredState, ConvergeContext


def _activate(unit_path: str, unit_link: str):
    def run(conn, result, pending=False):
        unit_changed = unit_path in result.changed_files or unit_link in result.changed_links
        # `pending`: a run that changed caddy's files was interrupted before this
        if layout.CADDY_BIN in result.changed_files or unit_changed or pending:
            ssh.sudo(
                conn,
                f"systemctl daemon-reload && systemctl enable {units.CADDY_UNIT} "
                f">/dev/null && systemctl restart {units.CADDY_UNIT}",
            )
            return "installed"
        if layout.CADDY_FILE in result.changed_files:
            # in place when caddy is up (nothing dropped; a bad file is refused and
            # the old config keeps serving); a stopped caddy just gets started
            ssh.sudo(
                conn,
                f"caddy validate --config {layout.CADDY_FILE} --adapter caddyfile "
                f">/dev/null && if systemctl is-active --quiet {units.CADDY_UNIT}; "
                f"then systemctl reload {units.CADDY_UNIT}; "
                f"else systemctl restart {units.CADDY_UNIT}; fi",
            )
            return "reloaded"
        return "unchanged"
    return run


def contribute(ctx: ConvergeContext) -> DesiredState:
    sites = caddy_mod.caddy_sites(ctx.conf, ctx.env_name, ctx.ports)
    if not sites:
        return DesiredState(name="caddy")

    env = ctx.conf.envs[ctx.env_name]
    entry = versions_mod.get(versions_mod.load(), env.anci_version)
    if not entry.caddy_url:
        raise errors.EnvError(
            "the pinned anci-version doesn't ship a caddy binary URL",
            try_="update the versions map, or remove 'host' from your exes",
        )
    unit = units.CADDY_UNIT
    unit_path = f"{layout.UNITS_DIR}/{unit}"
    unit_link = f"/etc/systemd/system/{unit}"
    return DesiredState(
        name="caddy",
        prepare=[
            "id -u caddy >/dev/null 2>&1 || useradd --system --home /var/lib/caddy "
            "--create-home --shell /usr/sbin/nologin caddy; "
            "mkdir -p /etc/anci/caddy /var/lib/caddy && chown -R caddy:caddy /var/lib/caddy"
        ],
        files=[
            FileArtifact(path=unit_path, content=systemd.render_caddy_unit(), mode="0644"),
            FileArtifact(path=layout.CADDY_FILE,
                         content=caddy_mod.caddyfile_for(ctx.conf, ctx.env_name, ctx.ports),
                         mode="0644"),
        ],
        links=[LinkArtifact(path=unit_link, target=unit_path)],
        after_apply=[AfterApply(label="caddy", phase=2, run=_activate(unit_path, unit_link),
                                marker=f"{layout.VAR_DIR}/.restart-caddy",
                                watches=[unit_path, unit_link, layout.CADDY_BIN,
                                         layout.CADDY_FILE])],
    )
