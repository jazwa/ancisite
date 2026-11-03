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

from .. import auth, box, errors, layout, ssh, ui
from ..conf import Conf
from ..inspect.db import paths as db_paths
from ..inspect.exe import paths as exe_paths
from ..inspect.singletons import env_paths


@dataclass
class _Entry:
    path: str
    meaning: str
    target: str | None = None
    size: int | None = None


class _Subject(NamedTuple):
    kind: str
    name: str
    candidates: list[tuple[str, str]]
    note: str | None


class _Stat(NamedTuple):
    kind: str
    size: int | None = None
    files: int = 0
    target: str | None = None
    resolved: str | None = None


def _int(s: str) -> int | None:
    return int(s) if s.isdigit() else None


def _subjects(conf: Conf, env_name: str, kind: str | None,
              name: str | None) -> list[_Subject]:
    def exe_sub(n: str, shared: bool = True) -> _Subject:
        schedule = conf.exes[n].resolve_for(env_name).schedule or "always"
        return _Subject("exe", n, exe_paths(conf, env_name, n, shared=shared), schedule)

    def db_sub(n: str, shared: bool = True) -> _Subject:
        return _Subject("db", n, db_paths(conf, env_name, n, shared=shared), None)

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
    subs = [exe_sub(n, shared=False) for n in conf.exes]
    subs += [db_sub(n, shared=False) for n in conf.dbs]
    if not subs:
        raise errors.ConfError("this conf defines no exes or dbs")
    return [_Subject("env", env_name, env_paths(conf, env_name), None), *subs]


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
    found = ssh.facts(conn, {p: f"describe {shlex.quote(p)}" for p in paths},
                      preamble=_DESCRIBE)
    res: dict[str, _Stat] = {}
    for path in paths:
        lines = found.get(path) or ["missing"]
        st = res.setdefault(path, _stat(lines[0]))
        if st.kind == "link" and len(lines) > 1:
            res.setdefault(st.resolved, _stat(lines[1]))
    return res


def _split_root(path: str) -> tuple[str, str]:
    for root in (layout.VAR_DIR, layout.LOG_DIR, layout.ETC_DIR, "/etc/systemd/system"):
        if path == root or path.startswith(root + "/"):
            return root, path[len(root) + 1:]
    return posixpath.dirname(path), posixpath.basename(path)


def _target(root: str, target: str) -> str:
    if target == root or target.startswith(root + "/"):
        return target[len(root) + 1:]
    return target


def _entries(candidates: list[tuple[str, str]], res: dict[str, _Stat], *,
             owner: str | None = None, blob_owners: dict[str, str] | None = None) -> list[_Entry]:
    out: list[_Entry] = []
    emitted: set[str] = set()
    for path, meaning in candidates:
        st = res.get(path, _Stat("missing"))
        if res and st.kind == "missing" and path.endswith(("-wal", "-shm")):
            continue
        if st.kind == "link":
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
    sched = f"  {ui.dim(note)}" if note not in (None, "always") else ""
    print(f"\n  {ui.bold('shared') if kind == 'env' else f'{ui.dim(kind)} {ui.bold(name)}'}{sched}")

    by_root: dict[str, list[tuple[str, _Entry]]] = {}
    for e in entries:
        root, rel = _split_root(e.path)
        by_root.setdefault(root, []).append((rel, e))

    lines: list[tuple[str, str, str, str]] = []
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
        reachable = False

    blob_owners: dict[str, str] = {}
    for sub in subjects:
        _render(sub.kind, sub.name, env_name, sub.candidates, res, sub.note, blob_owners)
    if not reachable:
        print(ui.dim("\n  box unreachable — showing paths only"))
    print()
