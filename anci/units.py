"""Unit names: one systemd unit per exe, plus the two mechanism units.

A cross-module contract — deploy renders by it, status probes it, converge
prunes by it, and `regen` starts/stops the timer. **No env segment**: one env
= one box, so the unit names on a box are already unique (and an env can't be
part of them). Keeping the naming here is the whole reason this module exists.

`anci` reserves the exe names `caddy`, `litestream` and `anci-logtrim`
(validated in conf.py), so no app unit can collide with the mechanism units.
"""

from __future__ import annotations

CADDY_UNIT = "caddy.service"
LITESTREAM_UNIT = "litestream.service"
LOGTRIM_SERVICE = "anci-logtrim.service"   # keeps /var/log/anci/*.log bounded
LOGTRIM_TIMER = "anci-logtrim.timer"
MECHANISM_UNITS = {CADDY_UNIT, LITESTREAM_UNIT, LOGTRIM_SERVICE, LOGTRIM_TIMER}


def service_name(exe_name: str) -> str:
    return f"{exe_name}.service"   # exe names are validated (conf.py)


def timer_name(exe_name: str) -> str:
    return f"{exe_name}.timer"
