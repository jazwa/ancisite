"""Shared types for the converge pass.

A *reconciler* is a module with `contribute(ctx) -> DesiredState`: it declares
the artifacts this part wants on the box. Desired states are pure (no ssh) — the
converge pass aggregates them, probes once, applies the delta, then runs the
after-apply steps.

Keeping the vocabulary here lets every reconciler stay small and lets tests
assert on a `DesiredState` without a box.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..apply import ApplyResult, DeleteArtifact, FileArtifact, LinkArtifact

if TYPE_CHECKING:
    from ..conf import Conf
    from ..staticfiles import Scanned
    from ..ssh import Conn


@dataclass
class BlobUpload:
    """A local binary to stream into the guest's content-addressed store."""

    exe_name: str
    local_path: Path
    dest: str                    # guest path (layout.blob_path(sha))
    sha: str = ""                # sha256 of local_path, hashed once at planning
    source: str = ""         # what the release log records (the conf's source)
    mode: str = "0755"       # 0755 for an exe binary; 0644 for a static file


@dataclass
class StaticUpload:
    """An exe's static files: each file ships to the blob store by its sha256,
    then the version dir is built as a tree of symlinks into the store."""

    exe_name: str
    local_dir: Path
    id: str                      # the version id (staticfiles.Scanned.id)
    dest: str                    # guest dir (layout.static_version(exe, id))
    entries: list[tuple[str, str]]   # (relative path, file sha256), sorted by path


@dataclass
class Download:
    """A pinned third-party binary (caddy/litestream) to fetch + push.

    Planning stays pure: the reconciler states what it wants, the executor
    fetches it once (hash-checked) and installs it.
    """

    url: str
    name: str
    dest: str                    # guest path
    sha256: str | None = None
    sha512: str | None = None
    extract: str | None = None
    mode: str = "0755"


@dataclass
class AfterApply:
    """A post-apply side effect (systemd restart, caddy reload, …).

    `phase` orders them: 0 = daemon-reload, 1 = app services, 2 = mechanism
    (caddy/litestream). `run(conn, result, pending)` gets the apply result so it
    can restart only what actually changed.

    `marker` + `watches` make a restart survive an interrupted run: when the
    apply is about to change any `watches` path, the executor first creates the
    `marker` file on the box, and removes it only once `run` succeeded. A marker
    found on a later run means a restart is still owed — `run` gets
    `pending=True` and must act as if everything it watches changed.
    """

    label: str
    phase: int
    run: Callable[..., str | None]
    marker: str | None = None
    watches: list[str] = field(default_factory=list)


@dataclass
class DesiredState:
    """One part's desired state for an env."""

    name: str
    prepare: list[str] = field(default_factory=list)     # root shell snippets
    files: list[FileArtifact] = field(default_factory=list)
    links: list[LinkArtifact] = field(default_factory=list)
    deletes: list[DeleteArtifact] = field(default_factory=list)
    blobs: list[BlobUpload] = field(default_factory=list)
    store_blobs: list[BlobUpload] = field(default_factory=list)   # content-addressed, not an exe (no release)
    statics: list[StaticUpload] = field(default_factory=list)
    downloads: list[Download] = field(default_factory=list)
    after_apply: list[AfterApply] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)       # units to stop (orphans)
    disables: list[str] = field(default_factory=list)    # timers to disable
    report: list[str] = field(default_factory=list)      # leftover lines (never deleted)


@dataclass
class ConvergeContext:
    """Everything the reconcilers need, resolved once.

    `binaries` maps exe name -> local binary for exes being (re)deployed; an exe
    with no source is reconciled from whatever is already on the box. `statics`
    maps exe name -> its scanned static files, likewise only for exes being
    deployed (no entry = the box's `static` link is left alone). `secrets`
    is the resolved (content, label) pair — resolved once so planning stays pure
    (no sops run per reconciler).
    """

    conf: "Conf"
    env_name: str
    ports: dict[tuple[str, str], int]
    binaries: dict[str, Path] = field(default_factory=dict)
    statics: dict[str, "Scanned"] = field(default_factory=dict)
    grow_disk: bool = False                    # `up`/`reconcile`: the disk may have grown
    secrets: tuple[str | None, str] = (None, "")
    local_ports: dict[str, int] = field(default_factory=dict)   # a local env: exe -> localhost port
    conn: "Conn | None" = None                 # set by the executor; None = pure planning
    # guest inventories for orphan planning (None = not gathered)
    present_units: set[str] | None = None
    exe_dirs: list[str] | None = None
    # box entries ignored for having a name anci can't have created (full paths)
    rejected: list[str] = field(default_factory=list)


# DesiredState's list fields an EnvPlan flattens across reconcilers
_FLAT = ("prepare", "files", "links", "deletes", "blobs", "store_blobs", "statics",
         "downloads", "stops", "disables", "report")


@dataclass
class EnvPlan:
    """Aggregated desired state for one env — what the executor applies.
    `plan.files`, `plan.stops`, … are every reconciler's lists, concatenated;
    `after_apply` is also sorted by phase."""

    desired_states: list[DesiredState]

    def __getattr__(self, attr: str) -> list:
        if attr not in _FLAT:
            raise AttributeError(attr)
        return [x for c in self.desired_states for x in getattr(c, attr)]

    @property
    def after_apply(self) -> list[AfterApply]:
        return sorted((a for c in self.desired_states for a in c.after_apply),
                      key=lambda a: a.phase)
