"""Box inspection's and apply's building blocks: a resource reports what the
conf wants (`target`) and what the box has (`probes` → `observed`); `diff`
compares the two; `steps` turns each difference into shell steps that apply
runs in one script. See internal-docs/box-inspection.md and box-apply.md."""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from .. import errors, layout, ssh
from ..hashes import sha256_text as sha
from ..conf import Conf


class Action(Enum):
    CREATE = "+"
    UPDATE = "~"
    DELETE = "-"


@dataclass(frozen=True)
class Change:
    identifier: str     # "exe.web", "litestream"
    action: Action
    fields: dict        # name -> (observed, target), only the ones that differ


class State(dict[str, dict]):
    """identifier -> fields, for the whole box."""


class TargetState(State):
    """The box as the conf wants it."""


class ObservedState(State):
    """The box as it is now. `facts` keeps each resource's raw probe output
    (resource name -> key -> lines) for its steps; `extra` holds the output of
    probes that aren't a resource's (blob sizes, cloud-init's status)."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.facts: dict[str, dict[str, list[str]]] = {}
        self.extra: dict[str, list[str]] = {}


@dataclass(frozen=True)
class Step:
    """One thing apply does on the box: root shell, run in order inside the
    run's single script. Two changes needing the same step build the same id
    (and script), so it runs once. `check` reads the lines the step printed and
    raises to fail the run."""

    id: str                          # "exe.web/restart", "box/daemon-reload"
    script: str
    after: tuple[str, ...] = ()      # step ids that must run first, when they're in the run
    check: Callable[[list[str]], None] | None = None
    says: str = ""                   # what it did, for the run's summary ("web restarted")


class Uploads:
    """Everything this run ships, in its one tar: files by content, blobs (the
    content-addressed store) by sha256. A blob the box already has (`have`:
    blob id -> its size there) isn't shipped again."""

    def __init__(self, have: dict[str, str] | None = None):
        self.files: list[tuple[bytes, str]] = []                 # (content, sha256)
        self.blobs: dict[str, tuple[Path, str, str]] = {}       # id -> (local path, sha256, mode)
        self.have = have or {}

    def file(self, content: str | bytes) -> str:
        """The shell path the script finds `content` at."""
        data = content.encode() if isinstance(content, str) else content
        self.files.append((data, sha(data)))
        return f'"$U/f/{len(self.files) - 1}"'

    def blob(self, local: Path, sha256: str, mode: str = "0755") -> str:
        """Ship `local` to the blob store (unless the box has it); its path there."""
        blob = layout.blob_id(sha256)
        size = self.have.get(blob)
        if size is None:
            self.blobs.setdefault(blob, (local, sha256, mode))
        elif size != str(local.stat().st_size):
            raise errors.EnvError(
                f"blob name {blob} collides with different bytes",
                why="the 64-bit content address already exists on the box with another size",
                try_="just run it again",
            )
        return layout.blob_path(sha256)


@dataclass
class ApplyContext:
    """What steps are built from: the conf, both states, and this run's inputs."""

    conf: Conf
    env_name: str
    user: str
    observed: ObservedState
    target: TargetState
    uploads: Uploads = field(default_factory=Uploads)
    # resource name -> what this run hands it (a deploy's binaries, for `exe`)
    inputs: dict[str, object] = field(default_factory=dict)
    # work on another machine that must happen before the script, (label, run)
    # pairs; `run` returns a step detail, or None when there was nothing to do
    elsewhere: list[tuple[str, Callable[[], str | None]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)                # lines to show after the run
    # The changes this run will actually make on the box. Some differences aren't
    # acted on: an exe that was never deployed is only created by `anci deploy`.
    applied: list[Change] = field(default_factory=list)


class Resource:
    """One kind of thing anci puts on a box. `target` reads only the conf;
    `observed` reads only its probes' output, never the conf, so what the conf
    no longer mentions is still seen (and diffs as a delete).

    `fields` is every field it reports: `observed` gives all of them, `target`
    a subset (one it leaves out isn't compared). Anything else fails fast
    (`check`), so a field can't slip past whatever handles the changes."""

    name: str
    fields: frozenset[str]
    # field (and "CREATE" / "DELETE") -> the matrix row its change maps to. A
    # change with no row stops the run before anything touches the box.
    rows: dict[str, str]

    def target(self, conf: Conf, env_name: str) -> dict[str, dict]:
        """identifier -> fields, as the conf wants them. No ssh."""
        raise NotImplementedError

    def probes(self) -> dict[str, str]:
        """key -> shell command; each runs as root, in one ssh call for the whole box."""
        raise NotImplementedError

    def observed(self, found: dict[str, list[str]]) -> dict[str, dict]:
        """identifier -> fields, from the probes' output lines (key -> lines)."""
        raise NotImplementedError

    def steps(self, change: Change, ctx: ApplyContext) -> list[Step]:
        """The steps that carry out `change` (see `rows_for`)."""
        raise NotImplementedError

    # -- hooks the engine calls on every resource; each does nothing by default

    def with_inputs(self, target: TargetState, inputs: object) -> None:
        """Set on the target what this run's `inputs` for it decide (a deploy's
        binary is the exe's `blob`)."""

    def blobs(self, target: TargetState, inputs: object) -> list[str]:
        """The blob ids this run may ship for it: whether the box has each
        decides the upload."""
        return []

    def always(self, ctx: ApplyContext) -> list[Step]:
        """Steps every run has, whatever changed."""
        return []

    # (before, after) globs over step ids, ordering its steps against others'
    rules: tuple[tuple[str, str], ...] = ()
    # field -> plain words for it in a change line (`blob` -> "new build")
    field_words: dict[str, str] = {}
    # True when removing it from the conf leaves it on the box on purpose (a
    # db's data): its removal isn't a change worth showing
    kept_when_removed = False

    def rows_for(self, change: Change) -> set[str]:
        """The matrix rows `change` maps to: its action's row for a create or a
        delete, each changed field's row for an update. Fails on one with none."""
        keys = list(change.fields) if change.action is Action.UPDATE else [change.action.name]
        missing = [k for k in keys if k not in self.rows]
        if missing:
            raise errors.InternalError(
                f"anci doesn't know how to apply {change.identifier}: {', '.join(missing)}",
                why=format_change(change))
        return {self.rows[k] for k in keys}


def check(resource: Resource, part: dict[str, dict], state: State) -> None:
    """Fail fast on what a resource reports but doesn't declare: an identifier
    outside its name, one another resource already reported, or a field not
    in its `fields` (and, observed, one of its `fields` missing)."""
    for ident, fields in part.items():
        if ident != resource.name and not ident.startswith(f"{resource.name}."):
            raise errors.InternalError(f"{resource.name} reported {ident!r}, which isn't one of its own")
        if ident in state:
            raise errors.InternalError(f"two resources reported {ident!r}")
        unknown = fields.keys() - resource.fields
        missing = resource.fields - fields.keys() if isinstance(state, ObservedState) else set()
        if unknown or missing:
            raise errors.InternalError(
                f"{ident} doesn't match the fields {resource.name} declares",
                why=", ".join([*(f"unknown: {f}" for f in sorted(unknown)),
                               *(f"missing: {f}" for f in sorted(missing))]))


def diff(observed: ObservedState, target: TargetState) -> list[Change]:
    """Every identifier whose fields differ. Pure. A field missing from the
    target isn't compared (e.g. an exe's `blob` when nothing is deployed)."""
    changes = []
    for ident in sorted(observed.keys() | target.keys()):
        have, want = observed.get(ident), target.get(ident)
        if want is None:
            changes.append(Change(ident, Action.DELETE,
                                  {k: (v, None) for k, v in have.items() if v is not None}))
        elif have is None:
            changes.append(Change(ident, Action.CREATE,
                                  {k: (None, v) for k, v in want.items() if v is not None}))
        else:
            fields = {k: (have.get(k), v) for k, v in want.items() if have.get(k) != v}
            if fields:
                changes.append(Change(ident, Action.UPDATE, fields))
    return changes


_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")      # a blob or static version id


def _readable(value) -> bool:
    """Whether a value says something to a reader on its own: a kind, a port,
    a replica — not a hash, an id, a path, or a list of them."""
    if value is None or isinstance(value, (bool, int)):
        return True
    return (isinstance(value, str) and not value.startswith("/") and len(value) <= 40
            and not _SHA_RE.match(value) and not _ID_RE.match(value))


def format_change(change: Change, words: dict[str, str] | None = None) -> str:
    """One short line: `+ exe.web`, `~ exe.report  kind: service → job`, or just
    the field's name when its values aren't readable (`~ vendored  litestream`)."""
    head = f"{change.action.value} {change.identifier}"
    if change.action is not Action.UPDATE:
        return head

    def say(k, old, new):
        if words and k in words:
            return words[k]
        if k == "pending":
            # an earlier run changed its files but was cut off before the restart
            return "not restarted since its last change"
        if _readable(old) and _readable(new):
            return f"{k}: {'none' if old is None else old} → {'none' if new is None else new}"
        return k
    return head + "  " + ", ".join(say(k, old, new) for k, (old, new) in change.fields.items())


# ---------------------------------------------------------------------------
# helpers for resources


def sha_probe(path: str) -> str:
    """Prints the file's sha256, or nothing when it's missing."""
    return f"sha256sum {shlex.quote(path)} | cut -c1-64"


def link_probe(path: str) -> str:
    """Prints the link's target, or nothing when it isn't a link."""
    return f"readlink {shlex.quote(path)}"


def pairs(lines: list[str]) -> dict[str, str]:
    """`<name> <value>` lines → name -> value ("" when the value is empty)."""
    return dict(line.partition(" ")[::2] for line in lines)


def install_file(ctx: ApplyContext, path: str, content: str | bytes, mode: str = "0644") -> str:
    """Shell installing `content` (shipped in the upload) at `path`, as root."""
    q = shlex.quote
    return (f"install -d {q(posixpath.dirname(path))} && "
            f"install -m {mode} {ctx.uploads.file(content)} {q(path)}")


def link(path: str, target: str) -> str:
    """Shell pointing the symlink `path` at `target`. Atomic: a new link beside
    the old one, renamed over it, so the path never goes missing."""
    q = shlex.quote
    return f"ln -sfn {q(target)} {q(path)}.anci-new && mv -T {q(path)}.anci-new {q(path)}"


def daemon_reload() -> Step:
    """systemd re-reads its units; ordered after every unit write (walk.RULES)."""
    return Step("box/daemon-reload", "systemctl daemon-reload")


def disable(unit: str) -> str:
    """Shell stopping `unit` and removing its start-at-boot links, even when its
    unit file is already gone (then `systemctl disable` can't find them)."""
    q = shlex.quote
    return (f"systemctl disable --now {q(unit)} >/dev/null 2>&1 || true; "
            f"rm -f {layout.SYSTEMD_DIR}/*.wants/{q(unit)}")


def boot_probe(units: list[str]) -> str:
    """Prints each `<target>.wants/<unit>` link to one of `units`."""
    globs = " ".join(f"*.wants/{shlex.quote(u)}" for u in units)
    return f'cd {layout.SYSTEMD_DIR} && for l in {globs}; do [ -L "$l" ] && echo "$l"; done'


class FileSet(Resource):
    """A single-instance resource that's a set of files and links (sshd's
    drop-in, caddy's unit + Caddyfile, …). Its identifier is its name; each file
    field holds the file's sha256, each link field the link's target. With a
    `unit`, it also reports its start-at-boot links (`boot`); with a `marker`, a
    restart an interrupted run still owes (`pending`)."""

    files: dict[str, str] = {}               # field -> path
    modes: dict[str, str] = {}               # field -> file mode (default 0644)
    links: dict[str, tuple[str, str]] = {}   # field -> (path, target)
    unit: str | None = None                  # the systemd unit it runs
    boot: tuple[str, ...] = ()               # its start-at-boot links, under SYSTEMD_DIR
    marker: str | None = None                # box file: a restart is still owed
    removable = True                         # False: the box always has it
    labels: dict[str, str] = {}              # field -> what the file is, for `anci info`

    @property
    def fields(self) -> frozenset[str]:
        return frozenset([*self.files, *self.links, *(["boot"] if self.boot else []),
                          *(["pending"] if self.marker else [])])

    @property
    def rows(self) -> dict[str, str]:
        rows = {f: "write" for f in [*self.files, *self.links]}
        rows["CREATE"] = "write"
        if self.boot:
            rows["boot"] = "boot"
        if self.marker:
            rows["pending"] = "pending"
        if self.removable:
            rows["DELETE"] = "remove"
        return rows

    def paths(self) -> list[tuple[str, str]]:
        """Its labeled files on the box, and what each is (`anci info`)."""
        return [(self.files[f], label) for f, label in self.labels.items()]

    def installed(self, observed: ObservedState) -> bool:
        """Whether the box has its unit file."""
        return observed.get(self.name, {}).get("unit") is not None

    def starts_at_boot(self, observed: ObservedState) -> bool:
        return bool(observed.get(self.name, {}).get("boot"))

    def wanted(self, conf: Conf, env_name: str) -> bool:
        """Whether the box should have it at all (cheap: no rendering)."""
        return True

    def contents(self, conf: Conf, env_name: str) -> dict[str, str] | None:
        """field -> each file's content, or None when the box shouldn't have it."""
        raise NotImplementedError

    def target(self, conf, env_name):
        contents = self.contents(conf, env_name)
        if contents is None:
            return {}
        fields = {**{f: sha(c) for f, c in contents.items()},
                  **{f: t for f, (_, t) in self.links.items()}}
        if self.boot:
            fields["boot"] = sorted(self.boot)
        if self.marker:
            fields["pending"] = False
        return {self.name: fields}

    def probes(self):
        probes = {**{f: sha_probe(p) for f, p in self.files.items()},
                  **{f: link_probe(p) for f, (p, _) in self.links.items()}}
        if self.boot:
            probes["boot"] = boot_probe(sorted({posixpath.basename(b) for b in self.boot}))
        if self.marker:
            probes["pending"] = f"[ -e {shlex.quote(self.marker)} ] && echo yes"
        return probes

    def observed(self, found):
        fields = {f: ssh.fact(found, f) for f in [*self.files, *self.links]}
        if not any(v is not None for v in fields.values()):
            return {}
        if self.boot:
            fields["boot"] = sorted(found["boot"])
        if self.marker:
            fields["pending"] = bool(found["pending"])
        return {self.name: fields}

    # -- steps

    def steps(self, change, ctx):
        rows = self.rows_for(change)
        if "remove" in rows:
            return [self.remove_step(), *([daemon_reload()] if self.unit else [])]
        out = []
        if "write" in rows:
            out.append(self.write_step(ctx))
            if self.unit:
                out.append(daemon_reload())
        if "boot" in rows and self.unit:
            out.append(Step(f"{self.name}/enable", f"systemctl enable {self.unit} >/dev/null"))
        return out + self.activate(rows, ctx)

    def activate(self, rows: set[str], ctx: ApplyContext) -> list[Step]:
        """The restart/reload steps for `rows` (none by default)."""
        return []

    def write_step(self, ctx: ApplyContext) -> Step:
        """Mark the owed restart, then install every file and link."""
        contents = self.contents(ctx.conf, ctx.env_name) or {}
        lines = [f"touch {shlex.quote(self.marker)}"] if self.marker else []
        lines += [install_file(ctx, self.files[f], c, self.modes.get(f, "0644"))
                  for f, c in contents.items()]
        lines += [link(p, t) for p, t in self.links.values()]
        return Step(f"{self.name}/write", "\n".join(lines))

    def remove_step(self) -> Step:
        """Stop and disable its unit, then delete its files and links."""
        q = shlex.quote
        paths = [*self.files.values(), *(p for p, _ in self.links.values()),
                 *([self.marker] if self.marker else [])]
        lines = [disable(self.unit)] if self.unit else []
        lines.append("rm -f " + " ".join(q(p) for p in paths))
        return Step(f"{self.name}/remove", "\n".join(lines))
