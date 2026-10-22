"""Litestream: unit + yaml, only when a db replicates (its binary is always
shipped: `vendored`)."""

from __future__ import annotations

import shlex

from ..render import litestream, systemd
from .. import errors, layout, ssh, units, versions as versions_mod
from ..apply import FileArtifact, LinkArtifact
from .context import AfterApply, DesiredState, ConvergeContext


def _target(db) -> str:
    """Where a db's replica lives — the identity litestream's local state belongs to."""
    r = db.replication
    return f"{r.endpoint or ''} {r.bucket} {r.path}"


def _reset_moved(replicated: list) -> str:
    """Root shell, run while litestream is stopped: a db whose replica target
    changed since its local state was written gets that state removed, so
    litestream starts the new replica from a fresh snapshot (it can't upload
    history it already deleted locally). Then the record is updated."""
    lines = []
    for d in replicated:
        rec, t = shlex.quote(layout.replica_record(d.name)), shlex.quote(_target(d))
        lines.append(f'if [ -f {rec} ] && [ "$(cat {rec})" != {t} ]; then '
                     f"rm -rf {shlex.quote(layout.litestream_state_dir(d.name))}; fi; "
                     f"printf '%s\\n' {t} > {rec}")
    return "\n".join(lines)


def _record_missing(replicated: list) -> str:
    """Root shell: write the replica record where there's none yet (an existing
    box, or a new db), without touching litestream's state."""
    return "\n".join(
        f"[ -f {shlex.quote(layout.replica_record(d.name))} ] || "
        f"printf '%s\\n' {shlex.quote(_target(d))} > {shlex.quote(layout.replica_record(d.name))}"
        for d in replicated)


def _activate(unit: str, unit_path: str, unit_link: str, replicated: list):
    any_db = " || ".join(f"[ -f {layout.db_path(d.name)} ]" for d in replicated)

    def run(conn, result, pending=False):
        unit_changed = unit_path in result.changed_files or unit_link in result.changed_links
        # the daemon reads its binary, config and credentials (EnvironmentFile) only
        # at start, so any of them changing needs a restart (`pending`: a run that
        # changed one of them was interrupted before restarting)
        changed = (
            pending
            or unit_changed
            or layout.LITESTREAM_BIN in result.changed_files
            or layout.LITESTREAM_FILE in result.changed_files
            or layout.SECRETS_FILE in result.changed_files
        )
        # enable always. Once a db file exists (migrate created it): restart if
        # something changed, otherwise only make sure it is running — a no-change
        # converge must not interrupt replication.
        act = (f"systemctl stop {unit}\n{_reset_moved(replicated)}\nsystemctl start {unit}"
               if changed else
               f"{_record_missing(replicated)}\n"
               f"systemctl is-active --quiet {unit} || systemctl start {unit}")
        ssh.sudo(
            conn,
            ("systemctl daemon-reload\n" if unit_changed else "")
            + f"systemctl enable {unit} >/dev/null\n"
            f"if {any_db}; then\n{act}\nfi\n",
        )
        return "restarted" if changed else "running"
    return run


def contribute(ctx: ConvergeContext) -> DesiredState:
    replicated = ctx.conf.replicated_dbs(ctx.env_name)
    if not replicated:
        return DesiredState(name="litestream")

    env = ctx.conf.envs[ctx.env_name]
    entry = versions_mod.get(versions_mod.load(), env.anci_version)
    if not entry.litestream_url:
        raise errors.EnvError(
            "the pinned anci-version doesn't ship a litestream binary URL",
            try_="update the versions map, or remove 'replication' from your dbs",
        )
    unit = units.LITESTREAM_UNIT
    unit_path = f"{layout.UNITS_DIR}/{unit}"
    unit_link = f"/etc/systemd/system/{unit}"
    return DesiredState(
        name="litestream",
        files=[
            FileArtifact(path=unit_path, content=systemd.render_litestream_unit(), mode="0644"),
            FileArtifact(path=layout.LITESTREAM_FILE,
                         content=litestream.render_litestream_yaml(replicated), mode="0644"),
        ],
        links=[LinkArtifact(path=unit_link, target=unit_path)],
        after_apply=[AfterApply(label="litestream", phase=2,
                             run=_activate(unit, unit_path, unit_link, replicated),
                             marker=f"{layout.VAR_DIR}/.restart-litestream",
                             watches=[unit_path, unit_link, layout.LITESTREAM_BIN,
                                      layout.LITESTREAM_FILE, layout.SECRETS_FILE])],
    )
