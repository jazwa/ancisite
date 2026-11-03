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
    identifier: str
    action: Action
    fields: dict


class State(dict[str, dict]):
    pass


class TargetState(State):
    pass


class ObservedState(State):

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.facts: dict[str, dict[str, list[str]]] = {}
        self.extra: dict[str, list[str]] = {}


@dataclass(frozen=True)
class Step:

    id: str
    script: str
    after: tuple[str, ...] = ()
    check: Callable[[list[str]], None] | None = None
    says: str = ""
    verify: str = ""


class Uploads:

    def __init__(self, have: dict[str, str] | None = None):
        self.files: list[tuple[bytes, str]] = []
        self.blobs: dict[str, tuple[Path, str, str]] = {}
        self.have = have or {}

    def file(self, content: str | bytes) -> str:
        data = content.encode() if isinstance(content, str) else content
        self.files.append((data, sha(data)))
        return f'"$U/f/{len(self.files) - 1}"'

    def blob(self, local: Path, sha256: str, mode: str = "0755") -> str:
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

    conf: Conf
    env_name: str
    user: str
    observed: ObservedState
    target: TargetState
    uploads: Uploads = field(default_factory=Uploads)
    inputs: dict[str, object] = field(default_factory=dict)
    elsewhere: list[tuple[str, Callable[[], str | None]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    applied: list[Change] = field(default_factory=list)


class Resource:

    name: str
    fields: frozenset[str]
    rows: dict[str, str]

    def target(self, conf: Conf, env_name: str) -> dict[str, dict]:
        raise NotImplementedError

    def probes(self) -> dict[str, str]:
        raise NotImplementedError

    def observed(self, found: dict[str, list[str]]) -> dict[str, dict]:
        raise NotImplementedError

    def steps(self, change: Change, ctx: ApplyContext) -> list[Step]:
        raise NotImplementedError


    def with_inputs(self, target: TargetState, inputs: object) -> None:
        pass

    def blobs(self, target: TargetState, inputs: object) -> list[str]:
        return []

    def always(self, ctx: ApplyContext) -> list[Step]:
        return []

    rules: tuple[tuple[str, str], ...] = ()
    field_words: dict[str, str] = {}
    kept_when_removed = False

    def rows_for(self, change: Change) -> set[str]:
        keys = list(change.fields) if change.action is Action.UPDATE else [change.action.name]
        missing = [k for k in keys if k not in self.rows]
        if missing:
            raise errors.InternalError(
                f"anci doesn't know how to apply {change.identifier}: {', '.join(missing)}",
                why=format_change(change))
        return {self.rows[k] for k in keys}


def check(resource: Resource, part: dict[str, dict], state: State) -> None:
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
_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _readable(value) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    return (isinstance(value, str) and not value.startswith("/") and len(value) <= 40
            and not _SHA_RE.match(value) and not _ID_RE.match(value))


def format_change(change: Change, words: dict[str, str] | None = None) -> str:
    head = f"{change.action.value} {change.identifier}"
    if change.action is not Action.UPDATE:
        return head

    def say(k, old, new):
        if words and k in words:
            return words[k]
        if k == "pending":
            return "not restarted since its last change"
        if _readable(old) and _readable(new):
            return f"{k}: {'none' if old is None else old} → {'none' if new is None else new}"
        return k
    return head + "  " + ", ".join(say(k, old, new) for k, (old, new) in change.fields.items())


def sha_probe(path: str) -> str:
    return f"sha256sum {shlex.quote(path)} | cut -c1-64"


def link_probe(path: str) -> str:
    return f"readlink {shlex.quote(path)}"


def pairs(lines: list[str]) -> dict[str, str]:
    return dict(line.partition(" ")[::2] for line in lines)


def install_file(ctx: ApplyContext, path: str, content: str | bytes, mode: str = "0644") -> str:
    q = shlex.quote
    return (f"install -d {q(posixpath.dirname(path))} && "
            f"install -m {mode} {ctx.uploads.file(content)} {q(path)}")


def link(path: str, target: str) -> str:
    q = shlex.quote
    return f"ln -sfn {q(target)} {q(path)}.anci-new && mv -T {q(path)}.anci-new {q(path)}"


def daemon_reload() -> Step:
    return Step("box/daemon-reload", "systemctl daemon-reload")


def disable(unit: str) -> str:
    q = shlex.quote
    return (f"systemctl disable --now {q(unit)} >/dev/null 2>&1 || true; "
            f"rm -f {layout.SYSTEMD_DIR}/*.wants/{q(unit)}")


def boot_probe(units: list[str]) -> str:
    globs = " ".join(f"*.wants/{shlex.quote(u)}" for u in units)
    return f'cd {layout.SYSTEMD_DIR} && for l in {globs}; do [ -L "$l" ] && echo "$l"; done'


class FileSet(Resource):

    files: dict[str, str] = {}
    modes: dict[str, str] = {}
    links: dict[str, tuple[str, str]] = {}
    unit: str | None = None
    boot: tuple[str, ...] = ()
    marker: str | None = None
    removable = True
    labels: dict[str, str] = {}

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
        return [(self.files[f], label) for f, label in self.labels.items()]

    def installed(self, observed: ObservedState) -> bool:
        return observed.get(self.name, {}).get("unit") is not None

    def starts_at_boot(self, observed: ObservedState) -> bool:
        return bool(observed.get(self.name, {}).get("boot"))

    def wanted(self, conf: Conf, env_name: str) -> bool:
        return True

    def contents(self, conf: Conf, env_name: str) -> dict[str, str] | None:
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
        return []

    def write_step(self, ctx: ApplyContext) -> Step:
        contents = self.contents(ctx.conf, ctx.env_name) or {}
        lines = [f"touch {shlex.quote(self.marker)}"] if self.marker else []
        lines += [install_file(ctx, self.files[f], c, self.modes.get(f, "0644"))
                  for f, c in contents.items()]
        lines += [link(p, t) for p, t in self.links.values()]
        return Step(f"{self.name}/write", "\n".join(lines))

    def remove_step(self) -> Step:
        q = shlex.quote
        paths = [*self.files.values(), *(p for p, _ in self.links.values()),
                 *([self.marker] if self.marker else [])]
        lines = [disable(self.unit)] if self.unit else []
        lines.append("rm -f " + " ".join(q(p) for p in paths))
        return Step(f"{self.name}/remove", "\n".join(lines))
