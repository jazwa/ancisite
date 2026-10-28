"""exe.<name>: an exe's units, links, start-at-boot links, deployed version, and
the restart that puts a change into effect."""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .. import box, forward, layout, releases, secrets as secrets_mod, units
from ..hashes import sha256_file
from ..conf import allocate_ports
from ..render import systemd
from .core import Resource, Step, daemon_reload, disable, install_file, link, pairs, sha

_PORT_RE = re.compile(r"^([^:]+)\.service:Environment=ANCI_PORT=(\d+)$")


@dataclass
class Deploy:
    """What a deploy hands the exes: new binaries and static files, and what
    the release log records as each one's source."""

    binaries: dict[str, Path] = field(default_factory=dict)   # exe -> its new binary
    statics: dict = field(default_factory=dict)               # exe -> its scanned static files
    source: dict[str, str] = field(default_factory=dict)      # exe -> the release log's source


def deploy_inputs(binaries=None, statics=None, source=None) -> dict[str, Deploy]:
    """A converge's inputs for a deploy of `binaries` (and their static files)."""
    return {"exe": Deploy(binaries or {}, statics or {}, source or {})}


def _deploy(ctx) -> Deploy:
    return ctx.inputs.get("exe") or Deploy()


def deployed(observed, exe_name: str) -> str | None:
    """The blob id an exe runs on the box, or None when it isn't deployed."""
    return observed.get(f"exe.{exe_name}", {}).get("blob")


def render_units(conf, env_name: str, exe_name: str, ports: dict, local_ports: dict,
                 secrets_path: str | None) -> dict[str, str]:
    """An exe's unit files, path -> content: its service, plus its timer for a job."""
    resolved = conf.exes[exe_name].resolve_for(env_name)
    unit_path = layout.unit_path(units.service_name(exe_name))
    log_file = layout.log_path(exe_name)
    # from the conf alone, so reconcile/up render it too (the path exists
    # once a deploy has shipped the files)
    static_vars = {"ANCI_STATIC_DIR": layout.static_link(exe_name)} if resolved.static else {}
    # the user's env-vars win over every anci-injected var (secrets win over both)
    env_vars = {**box.db_env_vars(conf, env_name), **box.port_env_vars(env_name, ports),
                **static_vars, **resolved.env_vars}
    # the app may write only its db dirs (state) — nothing else
    rw_paths = [layout.db_dir(n) for n in sorted(conf.dbs)]
    if resolved.schedule is not None:
        return {
            unit_path: systemd.render_oneshot(
                exe_name=exe_name, env_name=env_name, sysuser=layout.ANCI_USER,
                target=resolved.target, args=resolved.args, env_vars=env_vars,
                rw_paths=rw_paths, env_file=secrets_path, log_file=log_file,
            ),
            layout.unit_path(units.timer_name(exe_name)): systemd.render_timer(
                exe_name=exe_name, env_name=env_name, schedule=resolved.schedule,
            ),
        }
    return {unit_path: systemd.render_unit(
        exe_name=exe_name, env_name=env_name, sysuser=layout.ANCI_USER,
        target=resolved.target, args=resolved.args, port=ports[(exe_name, env_name)],
        env_vars=env_vars, rw_paths=rw_paths, env_file=secrets_path,
        bind=box.bind_address(conf, exe_name, env_name),
        url=box.app_url(conf, exe_name, env_name, local_ports), log_file=log_file,
    )}


def paths(conf, env_name: str, exe_name: str, *, shared: bool = True) -> list[tuple[str, str]]:
    """Where an exe's files live on the box, and what each is (`anci info`).
    `shared`: also the env-wide files it uses (the secrets)."""
    resolved = conf.exes[exe_name].resolve_for(env_name)
    unit = units.service_name(exe_name)
    rows = [
        (layout.current_link(exe_name), "current version"),
        (resolved.target, "what systemd runs"),
        (layout.unit_path(unit), "unit"),
        (layout.unit_link(unit), "enabled"),
        (layout.log_path(exe_name), "log"),
        (releases.release_file(exe_name), "deploy history"),
    ]
    if resolved.static:
        rows.append((layout.static_link(exe_name), "current static files"))
    if resolved.schedule is not None:
        timer = units.timer_name(exe_name)
        rows += [(layout.unit_path(timer), "timer"), (layout.unit_link(timer), "enabled")]
    if shared:
        rows.append((layout.SECRETS_FILE, "secrets"))
    return rows


def _units(conf, env_name: str, exe_name: str) -> dict[str, str]:
    secrets_path = layout.SECRETS_FILE if secrets_mod.has_secrets(conf, env_name) else None
    return render_units(conf, env_name, exe_name, allocate_ports(conf),
                        forward.installed_ports(conf, env_name), secrets_path)


def _boot(exe_name: str, scheduled: bool) -> list[str]:
    """The `*.wants` links `systemctl enable` makes: a service starts at boot,
    a job's timer is armed at boot (its oneshot service has no [Install])."""
    if scheduled:
        return [f"timers.target.wants/{units.timer_name(exe_name)}"]
    return [f"multi-user.target.wants/{units.service_name(exe_name)}"]


def marker(exe_name: str) -> str:
    """The box file saying this exe still owes a restart."""
    return f"{layout.exe_home(exe_name)}/.restart-pending"


def exe_restart(exe_name: str, after: tuple[str, ...] = ()) -> Step:
    """Restart a service (enabling it); clears its owed-restart marker."""
    svc = units.service_name(exe_name)
    return Step(f"exe.{exe_name}/restart",
                f"systemctl enable {svc} >/dev/null\nsystemctl restart {svc}\n"
                f"rm -f {shlex.quote(marker(exe_name))}", after=after, says=f"{exe_name} restarted")


def stop_steps(observed) -> list[Step]:
    """Stop EVERY exe on the box — the conf's and any it no longer declares —
    leaving each enabled (`regen` quiets the apps while it restores a db; a
    ghost writer racing the restore corrupts it). Litestream and caddy keep
    running."""
    names = sorted(i.split(".", 1)[1] for i in observed if i.startswith("exe."))
    return [Step(f"exe.{n}/stop-now",
                 f"systemctl stop {units.timer_name(n)} {units.service_name(n)} 2>/dev/null || true")
            for n in names]


def start_steps(conf, env_name: str, observed) -> list[Step]:
    """Start the conf's exes that are on the box again: a service runs, a job's
    timer is armed (its oneshot isn't run by hand). A removed exe stays down."""
    out = []
    for n in sorted(conf.exes):
        if f"exe.{n}" not in observed:
            continue
        scheduled = conf.exes[n].resolve_for(env_name).schedule is not None
        out.append(_arm(n, after=()) if scheduled else exe_restart(n))
    return out


def service_names(ctx) -> list[str]:
    """The services the conf wants that are already on the box."""
    return sorted(ident.split(".", 1)[1] for ident, t in ctx.target.items()
                  if ident.startswith("exe.") and t["kind"] == "service" and ident in ctx.observed)


class Exe(Resource):
    name = "exe"
    fields = frozenset({"kind", "unit", "timer", "unit_link", "timer_link", "bin", "boot",
                        "port", "blob", "static", "pending"})
    rows = {"CREATE": "create", "DELETE": "delete", "kind": "kind",
            "unit": "units", "timer": "units", "unit_link": "units", "timer_link": "units",
            "bin": "units", "boot": "boot", "port": "port", "blob": "blob", "static": "static",
            "pending": "pending"}
    rules = (("exe.*/delete", "box/daemon-reload"),)     # a removed exe's units go first
    field_words = {"blob": "new build", "static": "new static files"}

    def with_inputs(self, target, inputs):
        # what's deployed isn't the conf's to say: a deploy says it
        for name, binary in (inputs.binaries if inputs else {}).items():
            target[f"exe.{name}"]["blob"] = layout.blob_id(sha256_file(binary))
        for name, scanned in (inputs.statics if inputs else {}).items():
            target[f"exe.{name}"]["static"] = scanned.id

    def blobs(self, target, inputs):
        if not inputs:
            return []
        ids = [layout.blob_id(sha256_file(b)) for b in inputs.binaries.values()]
        return ids + [layout.blob_id(s) for sc in inputs.statics.values() for _, s in sc.entries]

    def target(self, conf, env_name):
        state = {}
        for e in sorted(conf.exes):
            resolved = conf.exes[e].resolve_for(env_name)
            scheduled = resolved.schedule is not None
            files = _units(conf, env_name, e)
            service, timer = units.service_name(e), units.timer_name(e)
            fields = {
                "kind": "job" if scheduled else "service",
                "unit": sha(files[layout.unit_path(service)]),
                "timer": sha(files[layout.unit_path(timer)]) if scheduled else None,
                "unit_link": layout.unit_path(service),
                "timer_link": layout.unit_path(timer) if scheduled else None,
                "bin": layout.current_link(e),
                "boot": _boot(e, scheduled),
                "port": None if scheduled else allocate_ports(conf)[(e, env_name)],
                "pending": False,
            }
            # no `blob`: what's deployed isn't the conf's to say (a deploy sets it).
            # `static` likewise, unless the conf dropped it: then there must be none
            if resolved.static is None:
                fields["static"] = None
            state[f"exe.{e}"] = fields
        return state

    def probes(self):
        u, d, s = layout.UNITS_DIR, layout.EXE_DIR, layout.SYSTEMD_DIR
        return {
            "units": f"ls -1 {u}",
            "dirs": f"ls -1 {d}",
            "shas": f'cd {u} && for f in *; do [ -f "$f" ] && echo "$f $(sha256sum "$f" | cut -c1-64)"; done',
            "links": f'cd {u} && for f in *; do echo "$f $(readlink {s}/"$f")"; done',
            "ports": f"cd {u} && grep -H '^Environment=ANCI_PORT=' -- *.service",
            "boot": f'cd {s} && for l in *.wants/*; do [ -L "$l" ] && echo "$l"; done',
            "bin": f'cd {d} && for e in *; do echo "$e $(readlink "$e/bin")"; done',
            # resolved (-e): a blob gone from the store reads as nothing deployed
            "current": f'cd {d} && for e in *; do echo "$e $(readlink -e "$e/current")"; done',
            "static": f'cd {d} && for e in *; do echo "$e $(readlink "$e/static")"; done',
            "pending": f'cd {d} && for e in *; do [ -e "$e/.restart-pending" ] && echo "$e"; done',
            # the static versions already built (a redeploy or rollback reuses one)
            "versions": f'cd {d} && for v in */static-*; do [ -d "$v" ] && echo "$v"; done',
        }

    def observed(self, found):
        unit_files = [u for u in found["units"] if u not in units.MECHANISM_UNITS]
        names = set(layout.split_safe(found["dirs"])[0])
        names |= {os.path.splitext(u)[0] for u in layout.split_safe(unit_files)[0]
                  if u.endswith((".service", ".timer"))}
        shas, links = pairs(found["shas"]), pairs(found["links"])
        bins, current, static = pairs(found["bin"]), pairs(found["current"]), pairs(found["static"])
        ports = {m[1]: int(m[2]) for line in found["ports"] if (m := _PORT_RE.match(line))}
        pending = set(found["pending"])
        state = {}
        for e in sorted(names):
            service, timer = units.service_name(e), units.timer_name(e)
            state[f"exe.{e}"] = {
                "kind": "job" if timer in unit_files else "service",
                "unit": shas.get(service) or None,
                "timer": shas.get(timer) or None,
                "unit_link": links.get(service) or None,
                "timer_link": links.get(timer) or None,
                "bin": bins.get(e) or None,
                "boot": sorted(l for l in found["boot"] if os.path.basename(l) in (service, timer)),
                # a job's oneshot carries no ANCI_PORT
                "port": ports.get(e) if timer not in unit_files else None,
                "blob": os.path.basename(current.get(e, "")) or None,
                "static": os.path.basename(static.get(e, "")).removeprefix("static-") or None,
                "pending": e in pending,
            }
        return state

    # -- steps

    def steps(self, change, ctx):
        name = change.identifier.split(".", 1)[1]
        rows = self.rows_for(change)
        if "delete" in rows:
            # its log and release history stay
            ctx.notes.append(f"removed {name}; kept its log and releases")
            return [_stop(name), _delete(name), daemon_reload()]
        want = ctx.target[change.identifier]
        deploy = _deploy(ctx)
        if "create" in rows and name not in deploy.binaries:
            return []    # only a deploy creates an exe
        out = []
        if "create" in rows:
            out.append(_dirs(name))
        if "kind" in rows:
            out.append(_stop(name))
        if rows & {"create", "kind", "units"}:
            out += [_write(ctx, name, want), daemon_reload()]
        moved = []
        if rows & {"create", "blob"}:
            out.append(_link_blob(ctx, name))
            moved.append(f"exe.{name}/link")
        if rows & {"create", "static"} and "static" in want:
            out.append(_link_static(ctx, name, want["static"]))
            moved.append(f"exe.{name}/static")
        if moved and name in deploy.binaries:
            out.append(_release(ctx, name, after=tuple(moved)))
        if "boot" in rows and not rows & {"create", "kind"}:
            out.append(_enable(name, want["kind"]))
        mine = tuple(f"exe.{name}/{s}" for s in ("write", "link", "static", "release"))
        if want["kind"] == "service":
            if rows & {"create", "kind", "units", "port", "blob", "static", "pending"}:
                out.append(exe_restart(name, after=mine + _holder(ctx, name, want["port"])))
        elif rows & {"create", "kind", "units", "pending"}:
            out.append(_arm(name, after=mine))
        return out


def _holder(ctx, name: str, port: int) -> tuple[str, ...]:
    """When ports shift, the exe that holds `port` now stops or moves first."""
    for ident, have in ctx.observed.items():
        if ident.startswith("exe.") and ident != f"exe.{name}" and have["port"] == port:
            return (f"{ident}/restart", f"{ident}/stop")
    return ()


def _dirs(name: str) -> Step:
    """Its managed dir, its release-log dir, and its log file."""
    log = layout.log_path(name)
    return Step(f"exe.{name}/dirs", "\n".join([
        f"install -d -o root -g {layout.ANCI_GROUP} -m 2775 "
        f"{layout.exe_home(name)} {layout.RELEASES_DIR}/{name}",
        f"touch {log} && chown {layout.ANCI_USER}:{layout.ANCI_GROUP} {log} && chmod 0640 {log}",
    ]))


def _stop(name: str) -> Step:
    """Stop and disable whatever it runs as now: a service, or a job's timer."""
    return Step(f"exe.{name}/stop", "\n".join([
        disable(units.timer_name(name)), disable(units.service_name(name))]))


def _delete(name: str) -> Step:
    q = shlex.quote
    paths = [layout.unit_path(u) for u in (units.service_name(name), units.timer_name(name))]
    paths += [layout.unit_link(u) for u in (units.service_name(name), units.timer_name(name))]
    return Step(f"exe.{name}/delete", f"rm -f {' '.join(q(p) for p in paths)}\n"
                                      f"rm -rf {q(layout.exe_home(name))}",
                after=(f"exe.{name}/stop",))


def _write(ctx, name: str, want: dict) -> Step:
    """Mark the owed restart, then its units and links (a service drops a job's timer)."""
    q = shlex.quote
    lines = [f"touch {q(marker(name))}"]
    lines += [install_file(ctx, p, c) for p, c in _units(ctx.conf, ctx.env_name, name).items()]
    service, timer = units.service_name(name), units.timer_name(name)
    lines.append(link(layout.unit_link(service), layout.unit_path(service)))
    if want["timer"] is None:
        lines.append(f"rm -f {q(layout.unit_path(timer))} {q(layout.unit_link(timer))}")
    else:
        lines.append(link(layout.unit_link(timer), layout.unit_path(timer)))
    # `target` (what systemd runs) → `current` → the blob
    lines.append(link(ctx.conf.exes[name].resolve_for(ctx.env_name).target, layout.current_link(name)))
    return Step(f"exe.{name}/write", "\n".join(lines),
                after=(f"exe.{name}/dirs", f"exe.{name}/stop"))


def _link_blob(ctx, name: str) -> Step:
    """Point `current` at the new binary (shipped to the blob store)."""
    binary = _deploy(ctx).binaries[name]
    path = ctx.uploads.blob(binary, sha256_file(binary))
    return Step(f"exe.{name}/link", f"touch {shlex.quote(marker(name))}\n"
                                    + link(layout.current_link(name), path),
                after=(f"exe.{name}/dirs",))


def _link_static(ctx, name: str, static_id: str | None) -> Step:
    """Point `static` at the version being deployed, building it from the blob
    store unless the box has it; or unlink it when the conf dropped `static`."""
    q = shlex.quote
    link_path = layout.static_link(name)
    if static_id is None:
        return Step(f"exe.{name}/static", f"rm -f {q(link_path)}")
    scanned = _deploy(ctx).statics[name]
    dest = layout.static_version(name, static_id)
    lines = [f"touch {q(marker(name))}"]
    built = {f"{layout.EXE_DIR}/{v}" for v in ctx.observed.facts.get("exe", {}).get("versions", [])}
    if dest in built:
        # reused (a rollback, an unchanged redeploy): freshen it so a concurrent
        # gc can't take it before the link points at it
        lines.append(f"touch {q(dest)}")
    else:
        paths = {sha: ctx.uploads.blob(scanned.root / rel, sha, "0644") for rel, sha in scanned.entries}
        # a symlink tree into the store, built beside the version and renamed
        # into place, so a half-built version is never visible under its id
        tmp = f"{layout.exe_home(name)}/.static-tmp"
        dirs = sorted({d for rel, _ in scanned.entries if (d := posixpath.dirname(rel))})
        lines += [f"rm -rf {q(tmp)}", f"mkdir -p {q(tmp)}"]
        lines += [f"mkdir -p {q(f'{tmp}/{d}')}" for d in dirs]
        lines += [f"ln -s {q(paths[sha])} {q(f'{tmp}/{rel}')}" for rel, sha in scanned.entries]
        lines += [f"find {q(tmp)} -type d -exec chmod 0755 {{}} +",
                  f"if [ -e {q(dest)} ]; then rm -rf {q(tmp)}; else mv -T {q(tmp)} {q(dest)}; fi"]
    lines.append(link(link_path, dest))
    return Step(f"exe.{name}/static", "\n".join(lines), after=(f"exe.{name}/dirs",))


def _release(ctx, name: str, after: tuple[str, ...]) -> Step:
    """Record the release: what runs now changed (written as soon as the links
    moved, so a failed restart can't lose it)."""
    deploy = _deploy(ctx)
    scanned = deploy.statics.get(name)
    return Step(f"exe.{name}/release", releases.append_script(
        name, env=ctx.env_name, blob=layout.blob_id(sha256_file(deploy.binaries[name])),
        source=deploy.source.get(name, ctx.conf.exes[name].resolve_for(ctx.env_name).source),
        user=ctx.user, static=scanned.id if scanned else None), after=after)


def _enable(name: str, kind: str) -> Step:
    """Fix its start-at-boot links alone: only what it runs as now starts at boot."""
    unit = units.timer_name(name) if kind == "job" else units.service_name(name)
    return Step(f"exe.{name}/enable", "\n".join([
        f"rm -f {layout.SYSTEMD_DIR}/*.wants/{units.service_name(name)} "
        f"{layout.SYSTEMD_DIR}/*.wants/{units.timer_name(name)}",
        f"systemctl enable {unit} >/dev/null"]))


def _arm(name: str, after: tuple[str, ...]) -> Step:
    """Arm a job's timer (a job isn't restarted: its next run uses what changed)."""
    timer = units.timer_name(name)
    return Step(f"exe.{name}/arm", f"systemctl enable --now {timer} >/dev/null\n"
                                   f"systemctl is-active --quiet {timer}\n"
                                   f"rm -f {shlex.quote(marker(name))}", after=after)
