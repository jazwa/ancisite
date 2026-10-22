"""`anci <env> info` — where an env's exe/db files live on the box, and how big.

Read-only. Renders a tree grouped by root directory, with two aligned columns:
the path on the left, its size and semantic meaning on the right. Everything is
a guest path (anci owns all of them under `/var/lib/anci`, `/etc/anci`,
`/var/log/anci`, plus the exe's configured `target`).

The whole guest side is one batched ssh probe; if the box is unreachable the
paths are still shown (they come from the conf) with `—` sizes.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from typing import NamedTuple

from .. import auth, box, errors, layout, ssh, ui, units
from ..conf import Conf


@dataclass
class _Entry:
    path: str
    meaning: str
    target: str | None = None   # symlink target (drawn as `path → target`)
    size: int | None = None


class _Subject(NamedTuple):
    kind: str                            # "env" (files every exe/db shares) | "exe" | "db"
    name: str
    candidates: list[tuple[str, str]]    # (guest path, what it is)
    note: str | None                     # an exe's schedule ("always" for a server)


class _Stat(NamedTuple):
    """What the probe found at one path."""
    kind: str                            # "file" | "dir" | "link" | "missing"
    size: int | None = None              # bytes (a dir: all its files)
    files: int = 0                       # a dir's file count
    target: str | None = None            # a link: where it points
    resolved: str | None = None          # a link: the file it ends at


def _int(s: str) -> int | None:
    return int(s) if s.isdigit() else None


# ---------------------------------------------------------------------------
# what each subject should show (paths are anci-owned; no ad-hoc strings)


def _env_candidates(conf: Conf, env_name: str) -> list[tuple[str, str]]:
    """Files the whole env shares: the secrets every exe gets, and litestream's
    config + unit when any db replicates."""
    rows = [(layout.SECRETS_FILE, "secrets (every exe)")]
    if conf.replicated_dbs(env_name):
        rows += [
            (layout.LITESTREAM_FILE, "replication config"),
            (f"{layout.UNITS_DIR}/{units.LITESTREAM_UNIT}", "replication unit"),
        ]
    return rows


def _exe_candidates(conf: Conf, env_name: str, exe_name: str, *,
                    shared: bool = True) -> list[tuple[str, str]]:
    """`shared`: include the env-wide files this exe uses (a one-exe view)."""
    exe = conf.exes[exe_name]
    resolved = exe.resolve_for(env_name)
    unit = units.service_name(exe_name)
    rows = [
        (layout.current_link(exe_name), "current version"),
        (resolved.target, "what systemd runs"),
        (f"{layout.UNITS_DIR}/{unit}", "unit"),
        (f"/etc/systemd/system/{unit}", "enabled"),
        (layout.log_path(exe_name), "log"),
        (f"{layout.RELEASES_DIR}/{exe_name}/releases.jsonl", "deploy history"),
    ]
    if resolved.static:
        rows.append((layout.static_link(exe_name), "current static files"))
    if resolved.schedule is not None:
        timer = units.timer_name(exe_name)
        rows += [
            (f"{layout.UNITS_DIR}/{timer}", "timer"),
            (f"/etc/systemd/system/{timer}", "enabled"),
        ]
    if shared:
        rows.append((layout.SECRETS_FILE, "secrets"))
    return rows


def _db_candidates(conf: Conf, env_name: str, db_name: str, *,
                   shared: bool = True) -> list[tuple[str, str]]:
    """`shared`: include the env-wide files this db uses (a one-db view)."""
    db = conf.dbs[db_name]
    data = layout.db_path(db_name)
    rows = [
        (data, "database"),
        (f"{data}-wal", "write-ahead log"),
        (f"{data}-shm", "WAL index"),
    ]
    if db.extensions:
        rows.append((layout.ext_dir(db_name), "extensions"))
        rows += [(f"{layout.ext_dir(db_name)}/{posixpath.basename(e)}", "extension")
                 for e in db.extensions]
    if not shared:
        return rows
    if db.resolve_for(env_name).replication:
        rows += [
            (layout.LITESTREAM_FILE, "replication config"),
            (f"{layout.UNITS_DIR}/{units.LITESTREAM_UNIT}", "replication unit"),
        ]
    rows.append((layout.SECRETS_FILE, "secrets"))
    return rows


def _subjects(conf: Conf, env_name: str, kind: str | None,
              name: str | None) -> list[_Subject]:
    """What to show: one exe/db, or everything."""
    def exe_sub(n: str, shared: bool = True) -> _Subject:
        schedule = conf.exes[n].resolve_for(env_name).schedule or "always"
        return _Subject("exe", n, _exe_candidates(conf, env_name, n, shared=shared), schedule)

    def db_sub(n: str, shared: bool = True) -> _Subject:
        return _Subject("db", n, _db_candidates(conf, env_name, n, shared=shared), None)

    if kind is not None and name is None:
        raise errors.ConfError(
            f"which {kind}?",
            try_=f"anci {env_name} info {kind} <name>",
        )
    if kind == "exe":
        if name not in conf.exes:
            raise errors.ConfError(
                f"exe '{name}' isn't in anci.toml",
                why=f"defined exes: {', '.join(sorted(conf.exes)) or '(none)'}",
            )
        return [exe_sub(name)]
    if kind == "db":
        if name not in conf.dbs:
            raise errors.ConfError(
                f"db '{name}' isn't in anci.toml",
                why=f"defined dbs: {', '.join(sorted(conf.dbs)) or '(none)'}",
            )
        return [db_sub(name)]
    if kind is not None:
        raise errors.ConfError(f"'{kind}' isn't a subject — use 'exe' or 'db'")
    if name is not None:
        raise errors.ConfError(
            "expected `info exe <name>` or `info db <name>`",
            try_=f"anci {env_name} info exe {name}",
        )
    # everything: the env-wide files once, then each exe's and db's own
    subs = [exe_sub(n, shared=False) for n in conf.exes]
    subs += [db_sub(n, shared=False) for n in conf.dbs]
    if not subs:
        raise errors.ConfError("this conf defines no exes or dbs")
    return [_Subject("env", env_name, _env_candidates(conf, env_name), None), *subs]


# ---------------------------------------------------------------------------
# guest probe: one round trip for every path


# `describe P` prints one `kind<TAB>a<TAB>b` line; a link adds a second line
# describing what it resolves to (the blob for an exe's `current`, the version
# dir for its `static`). A dir is sized through its links (`find -L`): static
# versions and ext dirs are trees of symlinks into the blob store
_DESCRIBE = r"""
describe() {
  p="$1"
  if [ -L "$p" ]; then
    f=$(readlink -f "$p")
    printf 'link\t%s\t%s\n' "$(readlink "$p")" "$f"
    if [ -n "$f" ] && [ -f "$f" ]; then
      printf 'file\t%s\t\n' "$(stat -c%s "$f")"
    elif [ -n "$f" ] && [ -d "$f" ]; then
      b=$(find -L "$f" -type f -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')
      printf 'dir\t%s %s\t\n' "$b" "$(find -L "$f" -type f 2>/dev/null | wc -l)"
    fi
  elif [ -d "$p" ]; then
    b=$(find -L "$p" -type f -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')
    printf 'dir\t%s %s\t\n' "$b" "$(find -L "$p" -type f 2>/dev/null | wc -l)"
  elif [ -f "$p" ]; then
    printf 'file\t%s\t\n' "$(stat -c%s "$p")"
  fi
}
"""


def _stat(line: str) -> _Stat:
    kind, a, b = (line.split("\t") + ["", ""])[:3]
    if kind == "link":
        return _Stat("link", target=a, resolved=b)
    if kind == "file":
        return _Stat("file", size=_int(a))
    if kind == "dir":
        size, count = (a.split() + ["0", "0"])[:2]
        return _Stat("dir", size=_int(size), files=_int(count) or 0)
    return _Stat("missing")


def _probe(conn: ssh.Conn, paths: list[str]) -> dict[str, _Stat]:
    """path -> what's there; a link's resolved file is keyed by its own path."""
    found = ssh.facts(conn, {p: f"describe {shlex.quote(p)}" for p in paths},
                      preamble=_DESCRIBE)
    res: dict[str, _Stat] = {}
    for path in paths:
        lines = found.get(path) or ["missing"]
        st = res.setdefault(path, _stat(lines[0]))
        if st.kind == "link" and len(lines) > 1:
            res.setdefault(st.resolved, _stat(lines[1]))  # first wins (the blob's stat)
    return res


# ---------------------------------------------------------------------------
# rendering


def _split_root(path: str) -> tuple[str, str]:
    for root in (layout.VAR_DIR, layout.LOG_DIR, layout.ETC_DIR, "/etc/systemd/system"):
        if path == root or path.startswith(root + "/"):
            return root, path[len(root) + 1:]
    return posixpath.dirname(path), posixpath.basename(path)


def _target(root: str, target: str) -> str:
    """A symlink target, shown relative when it's inside the row's root."""
    if target == root or target.startswith(root + "/"):
        return target[len(root) + 1:]
    return target


def _entries(candidates: list[tuple[str, str]], res: dict[str, _Stat], *,
             owner: str | None = None, blob_owners: dict[str, str] | None = None) -> list[_Entry]:
    """`blob_owners` (blob -> the first subject shown with it) is shared across
    subjects, so a blob two exes run says so instead of looking like two."""
    out: list[_Entry] = []
    emitted: set[str] = set()
    for path, meaning in candidates:
        st = res.get(path, _Stat("missing"))
        # sqlite keeps a db's -wal/-shm only while it's in use: absent isn't news
        # (an unreachable box probed nothing, so every path still shows)
        if res and st.kind == "missing" and path.endswith(("-wal", "-shm")):
            continue
        if st.kind == "link":
            # a link into the blob store or a static version gets its own
            # line, before the pointer
            dest = st.resolved or ""
            if dest not in emitted:
                if dest.startswith(layout.BLOB_DIR + "/"):
                    emitted.add(dest)
                    blob_meaning = "binary" if path.endswith("/current") else "file"
                    if blob_owners is not None:
                        first = blob_owners.setdefault(dest, owner)
                        if first != owner:
                            blob_meaning += f" (shared with {first})"
                    out.append(_Entry(dest, blob_meaning, size=res.get(dest, _Stat("file")).size))
                elif res.get(dest, _Stat("missing")).kind == "dir":
                    emitted.add(dest)
                    d = res[dest]
                    out.append(_Entry(dest, f"static files ({d.files} files)",
                                      size=d.size))
            out.append(_Entry(path, meaning, target=st.target))
        elif st.kind == "dir":
            out.append(_Entry(path, meaning, size=st.size if st.files else None))
        else:
            out.append(_Entry(path, meaning, size=st.size))
    return out


def _render(
    kind: str, name: str, env_name: str,
    candidates: list[tuple[str, str]], res: dict, note: str | None = None,
    blob_owners: dict[str, str] | None = None,
) -> None:
    entries = _entries(candidates, res, owner=name, blob_owners=blob_owners)
    # a job's schedule rides on its header; a service (note "always") needs none
    sched = f"  {ui.dim(note)}" if note not in (None, "always") else ""
    print(f"\n  {ui.bold('shared') if kind == 'env' else f'{ui.dim(kind)} {ui.bold(name)}'}{sched}")

    by_root: dict[str, list[tuple[str, _Entry]]] = {}   # insertion-ordered
    for e in entries:
        root, rel = _split_root(e.path)
        by_root.setdefault(root, []).append((rel, e))

    lines: list[tuple[str, str, str, str]] = []  # root, left, size, meaning
    for root, items in by_root.items():
        for i, (rel, e) in enumerate(items):
            branch = "└─" if i == len(items) - 1 else "├─"
            left = f"{branch} {rel}"
            if e.target:
                left += f" → {_target(root, e.target)}"
            size = ui.human_size(e.size) if e.size is not None else "—"
            lines.append((root, left, size, e.meaning))

    lw = max(len(x[1]) for x in lines)
    sw = max(len(x[2]) for x in lines)
    current = None
    for root, left, size, meaning in lines:
        if root != current:
            print(f"  {ui.dim(root)}")
            current = root
        print(f"    {left:<{lw}}  {size:>{sw}}  {ui.dim(meaning)}")


def info(conf: Conf, env_name: str, kind: str | None = None, name: str | None = None) -> None:
    if env_name not in conf.envs:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    subjects = _subjects(conf, env_name, kind, name)

    user = auth.authorize(conf, env_name)
    hint = box.ssh_hint(conf, env_name, user)
    if hint:
        print(ui.dim(f"  {hint}"))
    paths = list(dict.fromkeys(p for sub in subjects for p, _ in sub.candidates))

    res: dict[str, _Stat] = {}
    reachable = True
    try:
        conn = ssh.connect(conf, env_name, user)
        res = _probe(conn, paths)
    except ssh.SSHError:
        reachable = False  # show the paths from the conf with `—` sizes

    blob_owners: dict[str, str] = {}
    for sub in subjects:
        _render(sub.kind, sub.name, env_name, sub.candidates, res, sub.note, blob_owners)
    if not reachable:
        print(ui.dim("\n  box unreachable — showing paths only"))
    print()
