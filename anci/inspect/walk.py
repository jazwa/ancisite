"""Apply's walk: every change's steps, plus the ones every run has, in an
order that satisfies each step's `after` and the RULES below, joined into one
root script. See internal-docs/box-apply.md."""

from __future__ import annotations

import graphlib
import shlex
from fnmatch import fnmatch

from .. import errors, layout
from ..render import shell
from .core import ApplyContext, Change, Step

GC_MIN_AGE_MIN = 5

# (before, after): globs over step ids. A rule only orders steps that are in
# the run; one whose glob matches nothing adds nothing. These are the box's
# own; each resource adds its own (`Resource.rules`).
RULES = (
    ("box/layout", "*"),                       # the anci user, group and dirs come first
    ("*/write", "box/daemon-reload"),          # units are written, then systemd re-reads them…
    ("box/daemon-reload", "*/start"),          # …before anything (re)starts
    ("box/daemon-reload", "*/restart"),
    ("box/daemon-reload", "*/arm"),
    ("box/daemon-reload", "*/enable"),
    ("*", "box/gc"),                           # cleanup last, once nothing links to the old files
)


def _layout() -> Step:
    return Step("box/layout", "\n".join([*shell.ANCI_ACCOUNT_CMDS, *shell.layout_commands()]))


# grow the root filesystem into a disk the hypervisor grew: resolve the root
# source (e.g. /dev/vda1), split off the partition number, grow the partition,
# then the filesystem. Both no-op on a disk that already fills its device.
_GROW = (
    'root_src=$(findmnt -n -o SOURCE / 2>/dev/null || true)\n'
    'case "$root_src" in\n'
    '/dev/*[0-9])\n'
    '  disk=${root_src%[0-9]}; part=${root_src##*[!0-9]}\n'
    '  growpart "$disk" "$part" >/dev/null 2>&1 || true\n'
    '  resize2fs "$root_src" >/dev/null 2>&1 || true ;;\n'
    'esac'
)


def _gc() -> Step:
    """Delete blobs nothing links to, and static versions no exe serves, once
    they're older than GC_MIN_AGE_MIN (a concurrent deploy's fresh upload is
    younger). An exe dir that is itself a symlink is never followed."""
    b, x, e = layout.BLOB_DIR, layout.EXT_DIR, layout.EXE_DIR
    return Step("box/gc", f"""if cd {b} 2>/dev/null; then
real=$(pwd -P)    # the references are resolved paths: compare against the resolved dir
refs=$(mktemp); have=$(mktemp)
{{ for f in {e}/*/current {layout.CADDY_BIN} {layout.LITESTREAM_BIN} {layout.SQLITE_BIN} {x}/*/*; do
    readlink -f "$f" 2>/dev/null || true     # an unmatched glob, a missing link: no reference
  done
  for s in {e}/*/static; do
    v=$(readlink -f "$s" 2>/dev/null) || continue
    [ -d "$v" ] && find "$v" -type l -exec readlink -f {{}} + 2>/dev/null || true
  done; }} | sed -n "s|^$real/||p" | sort -u > "$refs"
find . -maxdepth 1 -type f -mmin +{GC_MIN_AGE_MIN} | sed 's|^\\./||' | sort > "$have"
comm -23 "$have" "$refs" | while IFS= read -r f; do rm -f -- "$f"; done
rm -f "$refs" "$have"
fi
for d in {e}/*; do
  [ -d "$d" ] && [ ! -L "$d" ] || continue
  keep=$(readlink -f "$d/static" 2>/dev/null)
  find "$d" -mindepth 1 -maxdepth 1 -type d \\( -name 'static-*' -o -name '.static-tmp*' \\) \\
       -mmin +{GC_MIN_AGE_MIN} | while IFS= read -r v; do
    [ "$v" = "$keep" ] || rm -rf -- "$v"
  done
done""")


def _blobs() -> Step:
    """The upload's blobs (unpacked in `$U`) into the store, unless already
    there (another run's, or a rollback)."""
    b = layout.BLOB_DIR
    return Step("box/blobs", "\n".join([
        'for f in "$U"/b/*; do',
        f'  id=${{f##*/}}; [ -e {b}/"$id" ] && continue',
        f'  install -o root -g {layout.ANCI_GROUP} -m "$(cat "$U/m/$id")" "$f" {b}/".tmp-$id"',
        f'  mv -f {b}/".tmp-$id" {b}/"$id"',
        "done",
    ]))


def always(ctx: ApplyContext, *, grow_disk: bool = False) -> list[Step]:
    """The steps every run has, whatever changed: the box's, and each resource's."""
    from . import RESOURCES
    out = [_layout(), _gc(), *(s for r in RESOURCES for s in r.always(ctx))]
    if grow_disk and not ctx.conf.envs[ctx.env_name].adopted:
        out.append(Step("box/grow-disk", _GROW))   # only a VM anci runs has a disk it grew
    return out


def _resource(change: Change):
    from . import RESOURCES
    name = change.identifier.split(".", 1)[0]
    for r in RESOURCES:
        if r.name == name:
            return r
    raise errors.InternalError(f"no resource owns {change.identifier}")


def walk(changes: list[Change], ctx: ApplyContext, *, grow_disk: bool = False) -> list[Step]:
    """Every change's steps plus the always ones, once each, in order."""
    from . import RESOURCES
    rules = RULES + tuple(rule for r in RESOURCES for rule in r.rules)
    steps: dict[str, Step] = {}

    def add(s: Step) -> None:
        have = steps.get(s.id)
        if have is None:
            steps[s.id] = s
            return
        if have.script != s.script:
            raise errors.InternalError(f"two changes built different steps named {s.id}")
        # the same step, wanted twice: it runs once, after everything either wanted first
        steps[s.id] = Step(s.id, s.script, tuple(dict.fromkeys(have.after + s.after)),
                           have.check or s.check, have.says or s.says)

    for c in changes:
        produced = _resource(c).steps(c, ctx)
        if produced:
            ctx.applied.append(c)     # what this run acts on (an undeployed exe it doesn't)
        for s in produced:
            add(s)
    for s in always(ctx, grow_disk=grow_disk):
        add(s)
    if ctx.uploads.blobs:
        add(_blobs())

    graph = graphlib.TopologicalSorter()
    for s in steps.values():
        graph.add(s.id, *(a for a in s.after if a in steps))
        # the upload's blobs are in the store before anything but the layout
        if "box/blobs" in steps and s.id not in ("box/layout", "box/blobs"):
            graph.add(s.id, "box/blobs")
        for before, after in rules:
            if fnmatch(s.id, after):
                graph.add(s.id, *(b for b in steps if b != s.id and fnmatch(b, before)))
    try:
        order = list(graph.static_order())
    except graphlib.CycleError as e:
        raise errors.InternalError("apply's steps depend on each other in a circle",
                                   why=" → ".join(e.args[1])) from None
    return [steps[i] for i in order]


STEP_MARK = "::step "


def script(steps: list[Step]) -> str:
    """The run's root script (it runs from the unpacked upload, `$U`): each
    step in order, each announced on both streams so its output can be read
    back and a failure named."""
    lines = ["set -e"]
    for s in steps:
        mark = shlex.quote(STEP_MARK + s.id)
        lines += [f"echo {mark}; echo {mark} >&2", "(", "set -e", s.script, ")"]
    return "\n".join(lines) + "\n"


def step_output(stdout: str) -> dict[str, list[str]]:
    """The script's stdout, split by step: id -> the lines it printed."""
    out: dict[str, list[str]] = {}
    current = None
    for line in stdout.splitlines():
        if line.startswith(STEP_MARK):
            current = line[len(STEP_MARK):]
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return out


def failed_step(stderr: str) -> str | None:
    """The step a failed script was in: the last one it announced."""
    marks = [l[len(STEP_MARK):] for l in stderr.splitlines() if l.startswith(STEP_MARK)]
    return marks[-1] if marks else None
