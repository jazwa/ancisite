"""Status tests describe the box as status's probe keys (`app active`, …) plus a
few box-inspection facts in the same flat style; these turn that into what the
one `ssh.facts` call behind `observed_state` returns."""

from __future__ import annotations

from unittest import mock

from anci import ssh, units
from anci.inspect import ObservedState, observed_state

# a mechanism unit -> the resource that reports it
_MECH = {units.CADDY_UNIT: "caddy", units.LITESTREAM_UNIT: "litestream"}


def box_facts(found: dict[str, list[str]]) -> dict[str, list[str]]:
    """`<exe> current`, `<unit> installed` and `<unit> enabled` become box
    inspection's facts; every other key is one of status's own probes."""
    out: dict[str, list[str]] = {}
    for key, lines in found.items():
        name, _, what = key.rpartition(" ")
        if what == "current":
            out.setdefault("exe/dirs", []).append(name)
            out.setdefault("exe/current", []).extend(f"{name} {l}" for l in lines)
        elif what == "installed" and name in _MECH:
            if lines == ["yes"]:
                out[f"{_MECH[name]}/unit"] = ["0" * 64]
        elif what == "enabled" and name in _MECH:
            if lines and lines[0].startswith("enabled"):
                out[f"{_MECH[name]}/boot"] = [f"multi-user.target.wants/{name}"]
        else:
            out[f"+{key}"] = lines
    return out


def observed(found: dict[str, list[str]]) -> ObservedState:
    """The ObservedState `status._read` gets for a box described by `found`."""
    extra = {k[1:]: "" for k in box_facts(found) if k.startswith("+")}
    with mock.patch.object(ssh, "facts", return_value=box_facts(found)):
        return observed_state(None, extra=extra)
