"""The converge executor: plan → probe → prepare → fetch/upload → apply →
activate → record.

This is the single pass every mutating verb ends in. It owns no per-part logic —
it aggregates the reconcilers' `DesiredState`s, does the box work once, and
returns the address.
"""

from __future__ import annotations

import posixpath
import shlex

from .. import apply as apply_mod
from .. import auth, bin as bin_mod, boxlock, edge, errors, forward, layout, paths, releases, scratch, ssh, ui
from .. import secrets as secrets_mod
from ..apply import FileArtifact, LinkArtifact
from ..conf import Conf, allocate_ports
from ..box import is_remote_host
from .context import BlobUpload, ConvergeContext, StaticUpload
from .plan import plan_env


GC_MIN_AGE_MIN = 5


# ---------------------------------------------------------------------------
# box inventory (one batched round trip; waits out a fresh boot)


def _inventory(conn: ssh.Conn) -> tuple[str, set[str], list[str]]:
    """(cloud-init status, unit files, exe dirs) on the box."""
    found = ssh.facts(conn, {
        # boot-finished is written at the end of cloud-init's last stage; the
        # `cloud-init status` fallback (~200 ms) runs only while it's missing
        "ci": "[ -e /var/lib/cloud/instance/boot-finished ] && echo 'status: done' || cloud-init status",
        "unit": f"ls -1 {layout.UNITS_DIR}",
        "exe": f"ls -1 {layout.EXE_DIR}",
    }, timeout=120)
    return ssh.fact(found, "ci") or "", set(found.get("unit", [])), found.get("exe", [])


def _gather(conn: ssh.Conn) -> tuple[set[str], list[str], list[str]]:
    """(unit files, exe dirs, rejected) — the box's listings, minus any entry whose
    name anci couldn't have created. `/var/lib/anci/exe` is writable by the whole
    `anci` group, so a name like `x; reboot` must never become an orphan to delete
    (or reach a shell); it is reported instead."""
    ci, units, dirs = _inventory(conn)
    if "running" in ci:
        ssh.wait_ready(conn)  # a fresh box is still in its first boot
        _, units, dirs = _inventory(conn)
    safe_units, bad_units = layout.split_safe(sorted(units))
    safe_dirs, bad_dirs = layout.split_safe(dirs)
    rejected = ([f"{layout.UNITS_DIR}/{n}" for n in bad_units]
                + [f"{layout.EXE_DIR}/{n}" for n in bad_dirs])
    return set(safe_units), safe_dirs, rejected


def build_context(conf: Conf, env_name: str, conn: ssh.Conn, *,
                  binaries=None, statics=None, grow_disk=False, local_ports=None) -> ConvergeContext:
    """Everything the reconcilers need for this box — shared by converge and
    status' drift check, so both judge the box against the same plan."""
    units, exe_dirs, rejected = _gather(conn)
    return ConvergeContext(
        conf=conf, env_name=env_name, ports=allocate_ports(conf),
        binaries=binaries or {}, statics=statics or {}, grow_disk=grow_disk, secrets=secrets_mod.resolve_secrets(conf, env_name),
        local_ports=local_ports or {},
        conn=conn, present_units=units, exe_dirs=exe_dirs, rejected=rejected,
    )


# ---------------------------------------------------------------------------
# blobs


def pending_uploads(blobs: list[BlobUpload], exists: dict) -> tuple[int, int]:
    """(count, bytes) of the blobs the box doesn't have yet — each dest once."""
    todo = {b.dest: b.local_path for b in blobs if exists.get(b.dest) is None}
    return len(todo), sum(p.stat().st_size for p in todo.values())


def upload_note(count: int, size: int) -> str:
    """A step note for an upload: `12.3 MB in 4 files`."""
    return f"{ui.human_size(size)} in {count} file{'' if count == 1 else 's'}"


def upload_blob(conn: ssh.Conn, blob: BlobUpload, have_size: str | None,
                exists: dict | None = None) -> bool:
    """Upload a blob unless the box has it. A blob's name is its hash and every
    upload is verified in full, so an existing blob is judged by its size — no
    re-hashing tens of MB on every deploy. `exists` (the shared probe result)
    is updated, so a blob two exes share ships once."""
    sha = blob.sha
    if exists is not None and have_size is None:
        have_size = exists.get(blob.dest)
    if have_size is not None:
        if have_size == str(blob.local_path.stat().st_size):
            return False
        raise errors.EnvError(
            f"blob name {layout.blob_id(sha)} collides with different bytes",
            why="the 64-bit content address already exists on the box with another size",
            try_="just run it again",
        )
    tmp = scratch.in_dir(layout.BLOB_DIR, "blob")
    ssh.put(conn, str(blob.local_path), tmp)
    apply_mod.verify_upload(conn, tmp, sha)
    ssh.sudo(
        conn,
        f"chown root:{layout.ANCI_GROUP} {tmp} && chmod {blob.mode} {tmp} && mv -f {tmp} {blob.dest}",
    )
    if exists is not None:
        exists[blob.dest] = str(blob.local_path.stat().st_size)
    return True


def _upload_static(conn: ssh.Conn, st: StaticUpload, present: bool,
                   exists: dict[str, str | None]) -> int:
    """Build an exe's static version dir on the box, unless it's already there.

    Each file ships to the content-addressed blob store by its sha256 — skipped
    when the box already has it, so an unchanged file (or one shared with
    another version or exe) is never re-uploaded. The version dir is then a tree
    of symlinks into the store, assembled in a hidden sibling and renamed into
    place, so a half-written version is never visible under its id. `exists` is
    the shared probe (blob dest -> size or None). Returns the blobs uploaded."""
    q = shlex.quote
    if present:
        # reused (a rollback, an unchanged redeploy): freshen it so a concurrent
        # gc can't take it before the link points at it
        ssh.sudo(conn, f"touch {q(st.dest)}")
        return 0

    uploaded, seen = 0, set()
    for rel, sha in st.entries:
        dest = layout.blob_path(sha)
        if sha in seen:            # the same bytes under two names ship once
            continue
        seen.add(sha)
        if upload_blob(conn, BlobUpload(exe_name=st.exe_name, local_path=st.local_dir / rel,
                                         dest=dest, sha=sha, mode="0644"), None, exists):
            uploaded += 1

    # assemble the version as a symlink tree into the store (every blob is now
    # present), in a hidden sibling, then rename it into place atomically
    tmp = f"{layout.exe_home(st.exe_name)}/.static-tmp-{scratch.RUN_ID}"
    dirs = sorted({d for rel, _ in st.entries if (d := posixpath.dirname(rel))})
    lines = [f"mkdir -p {q(f'{tmp}/{d}')}" for d in dirs]
    lines += [f"ln -s {q(layout.blob_path(sha))} {q(f'{tmp}/{rel}')}" for rel, sha in st.entries]
    ssh.sudo(conn, "set -e\n"
             f"rm -rf {q(tmp)}\n"
             f"mkdir -p {q(tmp)}\n"
             + "".join(l + "\n" for l in lines)
             + f"find {q(tmp)} -type d -exec chmod 0755 {{}} +\n"
             f"if [ -e {q(st.dest)} ]; then rm -rf {q(tmp)}; else mv -T {q(tmp)} {q(st.dest)}; fi\n",
             timeout=600)
    return uploaded


def _gc(conn: ssh.Conn) -> None:
    """Delete unreferenced blobs and static versions older than GC_MIN_AGE_MIN
    (concurrent-deploy guard)."""
    script = f"""
cd {layout.BLOB_DIR} 2>/dev/null || exit 0
ref={scratch.remote("gc-refs")}
{{ for f in {layout.EXE_DIR}/*/current {layout.CADDY_BIN} {layout.LITESTREAM_BIN} \
            {layout.SQLITE_BIN} {layout.EXT_DIR}/*/*; do
    readlink -f "$f" 2>/dev/null
  done
  for s in {layout.EXE_DIR}/*/static; do
    v=$(readlink -f "$s" 2>/dev/null) || continue
    [ -d "$v" ] && find "$v" -type l -exec readlink -f {{}} + 2>/dev/null
  done; }} | sort -u > "$ref"
find . -maxdepth 1 -type f -mmin +{GC_MIN_AGE_MIN} | sed 's|^\\./||' | while read -r b; do
  p=$(readlink -f "$b" 2>/dev/null) || continue
  [ -n "$p" ] && grep -qxF "$p" "$ref" || rm -f -- "$b"
done
rm -f "$ref"
"""
    ssh.script(conn, script)
    # static versions are root-owned trees, so this half runs as root; an exe
    # dir that is itself a symlink is never followed
    ssh.sudo(conn, f"""
for d in {layout.EXE_DIR}/*; do
  [ -d "$d" ] && [ ! -L "$d" ] || continue
  keep=$(readlink -f "$d/static" 2>/dev/null)
  find "$d" -mindepth 1 -maxdepth 1 -type d \\( -name 'static-*' -o -name '.static-tmp-*' \\) \\
       -mmin +{GC_MIN_AGE_MIN} | while read -r v; do
    [ "$v" = "$keep" ] || rm -rf -- "$v"
  done
done
""")


# ---------------------------------------------------------------------------
# the pass


def converge(conf: Conf, env_name: str, *, binaries=None, statics=None, source=None,
             grow_disk=False) -> str:
    """Bring `env_name`'s box to match the conf. Returns the address.

    `binaries` maps exe -> local binary to deploy; `statics` maps exe -> its
    packed static files; `source` maps exe -> the string the release log
    records for it. `grow_disk`: also grow the guest filesystem to its disk
    (only `up`/`reconcile`, which may have resized it).
    """
    env = conf.envs.get(env_name)
    if env is None:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    user = auth.authorize(conf, env_name)
    conn = ssh.connect(conf, env_name, user)
    # before any progress output: an env's first forward asks for sudo
    local_ports = forward.reconcile(conf, env_name)

    print(f"\n  {ui.bold(env_name)} {ui.dim('→')} {conn.ip}\n")
    # the lock is the first thing touching the box; a fresh box's sshd answers
    # before the account exists, so wait that out
    with boxlock.hold(conn, user, "deploy" if binaries else "converge", wait_ready=True):
        return _converge(conf, env, env_name, user, conn, binaries=binaries,
                         statics=statics, source=source or {}, grow_disk=grow_disk,
                         local_ports=local_ports)


def _converge(conf: Conf, env, env_name: str, user: str, conn: ssh.Conn, *,
              binaries, statics, source, grow_disk, local_ports) -> str:
    """`converge`'s pass, run while holding the box's lock."""
    pipe = ui.Pipeline()

    # reconcile: inventories, plan, stop orphans, root prep
    step = pipe.step("reconciling")
    try:
        ctx = build_context(conf, env_name, conn, binaries=binaries, statics=statics,
                            grow_disk=grow_disk, local_ports=local_ports)
    except ssh.SSHError as e:
        if not ssh.boot_race(conn, e):
            raise
        ssh.wait_ready(conn)
        ctx = build_context(conf, env_name, conn, binaries=binaries, statics=statics,
                            grow_disk=grow_disk, local_ports=local_ports)
    plan = plan_env(ctx)

    if plan.stops:
        ssh.sudo(conn, "systemctl stop " + " ".join(map(shlex.quote, plan.stops)))
    for u in plan.disables:
        ssh.sudo(conn, f"systemctl disable {shlex.quote(u)} >/dev/null 2>&1 || true")
    if plan.prepare:
        ssh.sudo(conn, "\n".join(plan.prepare))
    detail = f"{len(plan.files)} files" if plan.files else "clean"
    if plan.stops:
        detail += f"; stopped {', '.join(plan.stops)} (no longer in anci.toml)"
    step.done(detail)
    # a removed exe's history is kept, never deleted — say where it is
    for path in plan.report:
        print(f"    {ui.dim(f'kept {path} (from a removed exe)')}")
    for path in ctx.rejected:
        print(f"  {ui.yellow('⚠')} ignored {path!r}: not a name anci creates, so it is left alone "
              f"(look at who put it there)")

    # vendored binaries are content-addressed blobs too: fetch each locally,
    # name it by the sha256 of the *binary*, and symlink its bin path at it (so
    # it streams instead of sitting in memory, dedups, and an unchanged one is
    # skipped). Done before the probe so the blob dests can be probed with it.
    download_blobs, download_links = [], []
    for v in plan.downloads:
        binary = bin_mod.vendor(v.url, v.sha512, v.sha256, v.name,
                                paths.bin_cache_dir(), extract=v.extract)
        sha = apply_mod.sha256_file(binary)
        download_blobs.append(BlobUpload(exe_name=v.name, local_path=binary,
                                         dest=layout.blob_path(sha), sha=sha, mode=v.mode))
        download_links.append(LinkArtifact(path=v.dest, target=layout.blob_path(sha)))

    # probe once, then upload the delta
    step = pipe.step("uploading")
    links = list(plan.links) + download_links
    file_paths = [f.path for f in plan.files]
    link_paths = [l.path for l in links]
    # existence checks: paths to delete, blobs and static versions already on
    # the box, and restart markers left by an interrupted run
    markers = [a.marker for a in plan.after_apply if a.marker]
    blob_dests = sorted({layout.blob_path(sha) for s in plan.statics for _, sha in s.entries}
                        | {b.dest for b in plan.store_blobs} | {b.dest for b in download_blobs})
    exist_paths = ([d.path for d in plan.deletes] + [b.dest for b in plan.blobs]
                   + [s.dest for s in plan.statics] + blob_dests + markers)
    probe_result = apply_mod.probe(conn, file_paths, link_paths, deletes=exist_paths)
    hashes, targets, exists = probe_result

    files = list(plan.files)
    blobs = plan.blobs + plan.store_blobs + download_blobs
    static_blobs = [BlobUpload(exe_name=s.exe_name, local_path=s.local_dir / rel,
                               dest=layout.blob_path(sha), sha=sha, mode="0644")
                    for s in plan.statics if not exists.get(s.dest, False) for rel, sha in s.entries]
    count, size = pending_uploads(blobs + static_blobs, exists)
    if count:
        step.note(upload_note(count, size))
    uploaded = sum(1 for b in blobs if upload_blob(conn, b, None, exists))
    uploaded += sum(_upload_static(conn, s, exists.get(s.dest, False), exists)
                    for s in plan.statics)
    step.done(f"{uploaded} uploaded, {ui.human_size(size)}" if uploaded else "unchanged")

    # a restart owed by an earlier, interrupted run; then mark the ones this
    # apply makes owed *before* changing anything, so they survive an interruption
    pending = {m for m in markers if exists.get(m)}
    changing = ({f.path for f in apply_mod.diff_files(files, hashes)}
                | {l.path for l in apply_mod.diff_links(links, targets)}
                | {d.path for d in apply_mod.diff_deletes(plan.deletes, exists)})
    owed = sorted({a.marker for a in plan.after_apply
                   if a.marker and a.marker not in pending and changing & set(a.watches)})
    if owed:
        ssh.sudo(conn, "touch " + " ".join(map(shlex.quote, owed)))

    # apply the file/link delta + reload
    step = pipe.step("applying")
    result = apply_mod.apply(
        conn, files, links, probe_result=probe_result, deletes=plan.deletes)
    unit_touched = any(
        p.startswith(layout.UNITS_DIR) or p.startswith("/etc/systemd/system")
        for p in result.changed_files + result.changed_links
    )
    # a pending restart may also owe the reload of a unit an interrupted run wrote
    if unit_touched or pending:
        ssh.sudo(conn, "systemctl daemon-reload")
    if result.changed_files or result.changed_links:
        step.done(f"{len(result.changed_files)} files, {len(result.changed_links)} links")
    else:
        step.done("unchanged")

    # record: a release is a *change* of what runs — an exe whose `current` and
    # `static` already pointed at these versions wasn't deployed again. Recorded
    # as soon as the links moved, so an interrupted restart can't lose it (the
    # restart itself is retried through its marker).
    static_ids = {s.exe_name: s.id for s in plan.statics}
    for b in plan.blobs:
        if not ({layout.current_link(b.exe_name), layout.static_link(b.exe_name)}
                & set(result.changed_links)):
            continue
        releases.append(
            conn, b.exe_name, env=env_name,
            blob=layout.blob_id(b.sha),
            source=source.get(b.exe_name, b.source), user=user,
            static=static_ids.get(b.exe_name),
        )

    # a box on a remote hypervisor host is public only through the host's edge;
    # route it before caddy (re)starts, so its first ACME challenge gets through
    if is_remote_host(env):
        step = pipe.step("edge")
        detail = edge.reconcile(conf, env_name, user)
        if detail:
            step.done(detail)
        else:
            step.skip("no public hosts")

    # restart: run the post-apply side effects (systemd restart/enable, caddy
    # reload); a marker is cleared only once its step succeeded
    step = pipe.step("restarting")
    did, done = [], []
    try:
        for a in plan.after_apply:
            out = a.run(conn, result, pending=a.marker in pending)
            if a.marker in pending or a.marker in owed:
                done.append(a.marker)
            if out and out != "unchanged":
                did.append(out)
    finally:
        if done:
            ssh.sudo(conn, "rm -f " + " ".join(map(shlex.quote, sorted(set(done)))))
    if did:
        step.done(", ".join(dict.fromkeys(did)))
    else:
        step.skip("nothing to restart")

    _gc(conn)

    pipe.done()
    return conn.ip
