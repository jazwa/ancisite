"""Per-exe desired state: unit(s), links, blob, static files, log file.

Mirrors what `deploy` renders today, for every exe at once. The blob comes from
`ctx.binaries` (local bytes) and the static files from `ctx.statics`; an exe
without them keeps whatever version the box already runs.
"""

from __future__ import annotations

from .. import apply as apply_mod
from .. import box, layout, ssh, units
from ..render import systemd
from ..apply import DeleteArtifact, FileArtifact, LinkArtifact
from .context import AfterApply, BlobUpload, DesiredState, ConvergeContext, StaticUpload


def _prepare(exe_name: str) -> str:
    """Root bits for one exe: its managed dir and its log file."""
    log = layout.log_path(exe_name)
    return "\n".join([
        f"install -d -o root -g {layout.ANCI_GROUP} -m 2775 "
        f"{layout.EXE_DIR}/{exe_name} {layout.RELEASES_DIR}/{exe_name}",
        f"touch {log} && chown {layout.ANCI_USER}:{layout.ANCI_GROUP} {log} "
        f"&& chmod 0640 {log}",
    ])


def _restart_marker(exe_name: str) -> str:
    """The box file saying this exe still owes a restart (see AfterApply)."""
    return f"{layout.exe_home(exe_name)}/.restart-pending"


def _service_restart(exe_name, unit, unit_path, unit_link, target, cur, secrets_path,
                     static_link):
    # the executor runs daemon-reload once, before any of these
    def run(conn, result, pending=False):
        new_version = target in result.changed_links or cur in result.changed_links
        unit_changed = unit_path in result.changed_files or unit_link in result.changed_links
        secrets_changed = secrets_path is not None and secrets_path in result.changed_files
        # new static files restart it too: apps may read them once, at startup
        static_changed = static_link is not None and static_link in result.changed_links
        changes = [label for label, changed in (
            ("new version", new_version), ("unit changed", unit_changed),
            ("secrets changed", secrets_changed), ("new static files", static_changed),
        ) if changed]
        if not (changes or pending):
            return "unchanged"
        ssh.sudo(conn, f"systemctl enable {unit} >/dev/null && systemctl restart {unit}")
        return ", ".join(changes) or "pending restart"
    return run


def _job_restart(timer, unit_path, unit_link, timer_path, timer_link):
    def run(conn, result, pending=False):
        unit_changed = unit_path in result.changed_files or unit_link in result.changed_links
        timer_changed = timer_path in result.changed_files or timer_link in result.changed_links
        if unit_changed or timer_changed or pending:
            ssh.sudo(conn, f"systemctl enable --now {timer} >/dev/null")
        # a job isn't restarted on a new binary — verify the timer is armed
        ssh.script(conn, f"systemctl is-active --quiet {timer}")
        if unit_changed or timer_changed:
            return "unit changed" if unit_changed else "timer changed"
        return "pending restart" if pending else "unchanged"
    return run


def contribute(ctx: ConvergeContext) -> DesiredState:
    c = DesiredState(name="exes")
    db_vars = box.db_env_vars(ctx.conf, ctx.env_name)
    port_vars = box.port_env_vars(ctx.env_name, ctx.ports)
    secrets_path = layout.SECRETS_FILE if ctx.secrets[0] is not None else None
    # the app may write only its db dirs (state) — nothing else
    rw_paths = [layout.db_dir(n) for n in sorted(ctx.conf.dbs)]

    for exe_name in sorted(ctx.conf.exes):
        resolved = ctx.conf.exes[exe_name].resolve_for(ctx.env_name)
        source = ctx.binaries.get(exe_name)
        deployed = exe_name in (ctx.exe_dirs or [])
        if source is None and not deployed:
            # never deployed and no bytes to deploy it with — leave it alone
            continue
        sha = apply_mod.sha256_file(source) if source else None   # the only hash of it
        blob_path = layout.blob_path(sha) if sha else None
        unit = units.service_name(exe_name)
        unit_path = f"{layout.UNITS_DIR}/{unit}"
        unit_link = f"/etc/systemd/system/{unit}"
        cur = layout.current_link(exe_name)
        log_file = layout.log_path(exe_name)
        static_link = layout.static_link(exe_name)
        # from the conf alone, so reconcile/up render it too (the path exists
        # once a deploy has shipped the files)
        static_vars = {"ANCI_STATIC_DIR": static_link} if resolved.static else {}
        # the user's env-vars win over every anci-injected var (secrets win over both)
        env_vars = {**db_vars, **port_vars, **static_vars, **resolved.env_vars}

        c.prepare.append(_prepare(exe_name))
        # both kinds: the boot-enable link, `target` → `current` → the blob
        c.links += [LinkArtifact(path=unit_link, target=unit_path),
                    LinkArtifact(path=resolved.target, target=cur)]
        if blob_path is not None:
            c.blobs.append(BlobUpload(exe_name=exe_name, local_path=source,
                                      dest=blob_path, source=resolved.source, sha=sha))
            c.links.append(LinkArtifact(path=cur, target=blob_path))
        scanned = ctx.statics.get(exe_name)
        if scanned is not None:
            dest = layout.static_version(exe_name, scanned.id)
            c.statics.append(StaticUpload(exe_name=exe_name, local_dir=scanned.root,
                                          id=scanned.id, dest=dest, entries=scanned.entries))
            c.links.append(LinkArtifact(path=static_link, target=dest))
        elif resolved.static is None:
            # `static` dropped from the conf: unlink it (its versions then fall to gc)
            c.deletes.append(DeleteArtifact(static_link))

        if resolved.schedule is not None:
            timer = units.timer_name(exe_name)
            timer_path = f"{layout.UNITS_DIR}/{timer}"
            timer_link = f"/etc/systemd/system/{timer}"
            c.files += [
                FileArtifact(path=unit_path, mode="0644", content=systemd.render_oneshot(
                    exe_name=exe_name, env_name=ctx.env_name, sysuser=layout.ANCI_USER,
                    target=resolved.target, args=resolved.args, env_vars=env_vars,
                    rw_paths=rw_paths, env_file=secrets_path, log_file=log_file,
                )),
                FileArtifact(path=timer_path, mode="0644", content=systemd.render_timer(
                    exe_name=exe_name, env_name=ctx.env_name, schedule=resolved.schedule,
                )),
            ]
            c.links.append(LinkArtifact(path=timer_link, target=timer_path))
            c.after_apply.append(AfterApply(
                label=f"{exe_name} (job)", phase=1,
                run=_job_restart(timer, unit_path, unit_link, timer_path, timer_link),
                marker=_restart_marker(exe_name),
                watches=[unit_path, unit_link, timer_path, timer_link],
            ))
        else:
            served_static = static_link if resolved.static else None
            port = ctx.ports[(exe_name, ctx.env_name)]
            bind = box.bind_address(ctx.conf, exe_name, ctx.env_name)
            url = box.app_url(ctx.conf, exe_name, ctx.env_name, ctx.local_ports)
            c.files.append(FileArtifact(path=unit_path, mode="0644", content=systemd.render_unit(
                exe_name=exe_name, env_name=ctx.env_name, sysuser=layout.ANCI_USER,
                target=resolved.target, args=resolved.args, port=port, env_vars=env_vars,
                rw_paths=rw_paths, env_file=secrets_path, bind=bind, url=url, log_file=log_file,
            )))
            c.after_apply.append(AfterApply(
                label=f"{exe_name} (service)", phase=1,
                run=_service_restart(exe_name, unit, unit_path, unit_link,
                                     resolved.target, cur, secrets_path, served_static),
                marker=_restart_marker(exe_name),
                watches=[p for p in (resolved.target, cur, unit_path, unit_link, secrets_path,
                                     served_static) if p is not None],
            ))
    return c
