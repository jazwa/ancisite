"""Box inspection: what differs between the conf and the box, without changing
anything. See internal-docs/box-inspection.md."""

from __future__ import annotations

from .. import ssh
from ..conf import Conf
from .core import (Action, Change, ObservedState, Resource, State, TargetState, check, diff,
                   format_change)
from .db import Db
from .exe import Exe
from .singletons import Caddy, Litestream, Logtrim, Resolved, Secrets, Sshd
from .user import User
from .vendored import Vendored

RESOURCES: tuple[Resource, ...] = (User(), Exe(), Db(), Caddy(), Litestream(), Logtrim(),
                                   Sshd(), Resolved(), Secrets(), Vendored())


def _owner(change: Change) -> Resource | None:
    for r in RESOURCES:
        if change.identifier == r.name or change.identifier.startswith(f"{r.name}."):
            return r
    return None


def worth_showing(changes: list[Change]) -> list[Change]:
    """The changes to show someone: all but the removal of something anci
    leaves on the box on purpose (a db taken out of anci.toml keeps its data)."""
    return [c for c in changes
            if not (c.action is Action.DELETE and (r := _owner(c)) and r.kept_when_removed)]


def describe(change: Change) -> str:
    """A change as one plain line, in its resource's own words."""
    r = _owner(change)
    return format_change(change, r.field_words if r else None)


def target_state(conf: Conf, env_name: str) -> TargetState:
    """The box as the conf wants it. No ssh."""
    state = TargetState()
    for r in RESOURCES:
        part = r.target(conf, env_name)
        check(r, part, state)
        state |= part
    return state


def observed_state(conn: ssh.Conn, extra: dict[str, str] | None = None) -> ObservedState:
    """The box as it is now. One ssh call, which also runs the `extra` probes
    (key -> shell; their output lands in `state.extra`)."""
    probes = {r.name: r.probes() for r in RESOURCES}
    found = ssh.facts(conn, {**{f"{name}/{key}": cmd
                                for name, p in probes.items() for key, cmd in p.items()},
                             **{f"+{key}": cmd for key, cmd in (extra or {}).items()}}, root=True)
    state = ObservedState()
    state.extra = {key: found.get(f"+{key}", []) for key in extra or {}}
    for r in RESOURCES:
        state.facts[r.name] = {key: found.get(f"{r.name}/{key}", []) for key in probes[r.name]}
        part = r.observed(state.facts[r.name])
        check(r, part, state)
        state |= part
    return state


__all__ = ["Action", "Change", "ObservedState", "RESOURCES", "Resource", "State", "TargetState",
           "check", "describe", "diff", "format_change", "observed_state", "target_state", "worth_showing"]
