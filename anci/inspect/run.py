"""Apply: bring a box in line with the conf — the pass every mutating verb
(`up`, `reconcile`, `deploy`, `migrate`) ends in.

Four ssh calls, however much changed: take the box's lock, read the box
(`observed_state`), upload one tar (the step script, every changed file, every
blob the box lacks), run it as root. See internal-docs/box-apply.md.
"""

from __future__ import annotations

import copy
import io
import os
import shlex
import tarfile
from typing import Callable

from .. import auth, boxlock, errors, forward, layout, scratch, ssh, ui
from ..conf import Conf
from . import RESOURCES, TargetState, describe, diff, observed_state, target_state
from .core import ApplyContext, Change, Step, Uploads, sha
from .walk import failed_step, script, step_output, walk

# a fresh box's cloud-init may still be finishing its first boot
_CLOUD_INIT = ("[ -e /var/lib/cloud/instance/boot-finished ] && echo 'status: done' "
               "|| cloud-init status 2>/dev/null || echo 'status: unavailable'")


def converge(conf: Conf, env_name: str, *, inputs: dict | None = None,
             grow_disk: bool = False,
             select: Callable[[list[Change]], list[Change]] | None = None,
             pipe: ui.Pipeline | None = None, label: str | None = None) -> str:
    """Bring `env_name`'s box in line with the conf. Returns its address.

    `inputs` hands resources what this run brings besides the conf, by resource
    name (a deploy's binaries: `exe.deploy_inputs`). `grow_disk`: also grow the
    guest filesystem into its disk (`up`/`reconcile`, which may have resized
    it). `select` narrows the changes applied (`db.migration_setup`).

    `pipe`: a verb's own pipeline to report in (it printed the header and ends
    the pipeline); `label`: report the whole run as that one step.
    """
    env = conf.envs.get(env_name)
    if env is None:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    user = auth.authorize(conf, env_name)
    conn = ssh.connect(conf, env_name, user)
    # before any progress output: an env's first forward asks for sudo
    forward.reconcile(conf, env_name)

    own = pipe is None
    if own:
        print(f"\n  {ui.bold(env_name)} {ui.dim('→')} {conn.ip}\n")
        pipe = ui.Pipeline()
    # the lock is the first thing touching the box; a fresh box's sshd answers
    # before the account exists, so wait that out
    with boxlock.hold(conn, user, "deploy" if inputs else "converge", wait_ready=True):
        _converge(conf, env_name, user, conn, pipe, label, inputs=inputs or {},
                  grow_disk=grow_disk, select=select)
    if own:
        pipe.done()
    return conn.ip


class _Quiet:
    """A pipeline step that shows nothing: a labeled run reports once, at the end."""

    def done(self, *a): pass
    def skip(self, *a): pass
    def note(self, *a): pass
    def fail(self): pass


def look(conf: Conf, env_name: str, conn, inputs: dict):
    """What the conf wants on the box, before and after this run's `inputs` (a
    deploy's builds), and what the box has: (conf_only, target, observed). The
    one place converge and `plan` work these out."""
    conf_only = target_state(conf, env_name)
    target = TargetState(copy.deepcopy(dict(conf_only)))
    for r in RESOURCES:
        r.with_inputs(target, inputs.get(r.name))
    blob_ids = sorted({b for r in RESOURCES for b in r.blobs(target, inputs.get(r.name))})
    extra = {"ci": _CLOUD_INIT,
             "blobs": f"cd {layout.BLOB_DIR} 2>/dev/null && for b in {' '.join(blob_ids)}; do "
                      f'[ -f "$b" ] && echo "$b $(stat -c %s "$b")"; done' if blob_ids else ":"}
    observed = observed_state(conn, extra)
    if any("running" in line for line in observed.extra["ci"]):
        ssh.wait_ready(conn)   # a fresh box: let cloud-init finish first
        observed = observed_state(conn, extra)
    return conf_only, target, observed


def _converge(conf, env_name, user, conn, pipe, label, *, inputs, grow_disk, select):
    whole = pipe.step(label) if label else None   # the run as one step
    step = _Quiet() if whole else pipe.step("inspecting")
    _, target, observed = look(conf, env_name, conn, inputs)
    changes = diff(observed, target)
    if select is not None:
        changes = select(changes)
    ctx = ApplyContext(
        conf=conf, env_name=env_name, user=user, observed=observed, target=target,
        uploads=Uploads(dict(line.split(" ", 1) for line in observed.extra["blobs"])),
        inputs=inputs)
    steps = walk(changes, ctx, grow_disk=grow_disk)
    applied = ctx.applied
    step.done(f"{len(applied)} change{'' if len(applied) == 1 else 's'}" if applied else "in line")
    if not whole:
        for c in applied:
            print(f"    {ui.dim(describe(c))}")

    # work on another machine the script depends on (the hypervisor's edge)
    for where, work in ctx.elsewhere:
        other = pipe.step(where)
        detail = work()
        other.done(detail) if detail else other.skip("nothing to do")

    step = _Quiet() if whole else pipe.step("uploading")
    count = len(ctx.uploads.blobs)
    size = sum(p.stat().st_size for p, _, _ in ctx.uploads.blobs.values())
    if count:
        step.note(f"{ui.human_size(size)} in {count} file{'' if count == 1 else 's'}")
    remote = _upload(conn, ctx.uploads, script(steps))
    step.done(f"{count} uploaded, {ui.human_size(size)}" if count else "nothing new")

    step = whole or pipe.step("applying")
    try:
        out = ssh.sudo(conn, _launcher(remote), timeout=3600).stdout
    except ssh.SSHError as e:
        if e.unreachable or e.auth or e.host_key:
            raise
        step.fail()
        raise _failed(e) from None
    printed = step_output(out)
    for s in steps:
        if s.check:
            s.check(printed.get(s.id, []))
    did = ", ".join(dict.fromkeys(s.says for s in steps if s.says))
    if whole:
        what = ", ".join(c.identifier for c in applied)
        whole.done("; ".join(p for p in (what, did) if p)) if what or did else whole.skip("in line")
    else:
        step.done(did) if did else step.skip("nothing to restart")
    for note in ctx.notes:
        print(f"    {ui.dim(note)}")


def run_steps(conn, steps: list[Step]) -> None:
    """Run steps, in the order given, as one root script — for a verb that
    acts on the box outside a converge (regen). Ships nothing."""
    if not steps:
        return
    try:
        out = ssh.sudo(conn, script(steps), timeout=600).stdout
    except ssh.SSHError as e:
        if e.unreachable or e.auth or e.host_key:
            raise
        raise _failed(e) from None
    printed = step_output(out)
    for s in steps:
        if s.check:
            s.check(printed.get(s.id, []))


def _upload(conn, uploads: Uploads, run: str) -> str:
    """One tar — the script, the files (`f/<n>`), the blobs with their modes
    (`b/<id>`, `m/<id>`), and a sha256 manifest — to the box. Its path there."""
    local = scratch.local("apply.tar")
    local.unlink(missing_ok=True)
    manifest = []
    # 0600 from the start: it may carry the decrypted secrets file
    with os.fdopen(os.open(local, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f, \
         tarfile.open(fileobj=f, mode="w") as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            tar.addfile(info, io.BytesIO(data))
            manifest.append(f"{sha(data)}  {name}")

        add("run.sh", run.encode())
        for i, (data, _) in enumerate(uploads.files):
            add(f"f/{i}", data)
        for blob, (path, sha256, mode) in uploads.blobs.items():
            with path.open("rb") as src:
                info = tarfile.TarInfo(f"b/{blob}")
                info.size, info.mode = path.stat().st_size, 0o600
                tar.addfile(info, src)
            manifest.append(f"{sha256}  b/{blob}")
            add(f"m/{blob}", mode.encode())
        data = ("\n".join(manifest) + "\n").encode()
        info = tarfile.TarInfo("manifest")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    remote = scratch.remote("apply.tar")
    try:
        ssh.put(conn, str(local), remote, private=True)
    finally:
        local.unlink(missing_ok=True)
    return remote


def _launcher(remote: str) -> str:
    """Root: unpack the upload, check every byte arrived, run its script."""
    t = shlex.quote(remote)
    return (f"set -e\nU=$(mktemp -d)\ntrap 'rm -rf \"$U\" {t}' EXIT\n"
            f'tar -xf {t} -C "$U"\n'
            f'(cd "$U" && sha256sum --quiet -c manifest)\n'
            f'export U\nbash "$U/run.sh"\n')


def _failed(e: ssh.SSHError) -> errors.EnvError:
    at = failed_step(e.stderr)
    why = "\n".join(l for l in e.stderr.splitlines() if not l.startswith("::step "))[-800:]
    return errors.EnvError(
        f"applying the changes failed{f' at {at}' if at else ''}",
        why=why.strip() or None,
        try_="fix what it says, then re-run: what already ran is kept, the rest is picked up",
        debug=e.stderr)
