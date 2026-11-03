"""`anci <env> status` — the home screen + drift report.

Default: one boxed table — exe, state, url (shown only when the box actually
answers — a report, not a conf promise), cpu + mem (a running service's live
use from systemd accounting; a job shows — between runs), last deploy (who).
The deployed blob id is identity, not a readable version, so it lives in
`anci info`, not here —
a second boxed table for the conf's dbs — name, last migration (the guest's
`_anci_migrations` bookkeeping), size (data file + wal), replication (the
replica's last successful sync, read locally from the litestream daemon; the db rows replace a litestream checklist line)
— where the replica goes is in `anci info` — and a
checklist under it: caddy, the TLS front (litestream's health is per-db in the
db table) — plus the ssh hint to log into the box.
`--debug` adds the drift report: what `anci <env> plan` shows (everything on
the box that differs from the conf) and the release log against what's deployed.

Box state is read with one round trip — box inspection's (`observed_state`),
carrying this report's health probes too; a down box degrades
gracefully instead of failing the whole report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
import shlex
import time
from datetime import datetime

from . import dbs
from .. import auth, box, errors, forward, layout, releases as rel_mod, ssh, ui, units
from ..inspect import ObservedState, describe, diff, observed_state, target_state, worth_showing
from ..inspect.exe import deployed
from ..inspect.singletons import optional_services
from ..layout import ANCI_USER
from ..conf import Conf, allocate_ports


_SERVICES = {r.unit: r for r in optional_services()}


def _mech_units(conf: Conf, env_name: str) -> list[str]:
    return sorted(u for u, r in _SERVICES.items() if r.wanted(conf, env_name))


def _probes(conf: Conf, exe_names: list[str], env_name: str, ports: dict) -> dict[str, str]:
    p: dict[str, str] = {}
    for exe_name in exe_names:
        resolved = conf.exes[exe_name].resolve_for(env_name)
        if resolved.schedule is not None:
            timer = units.timer_name(exe_name)
            p[f"{exe_name} timer"] = f"systemctl is-active {timer}"
            p[f"{exe_name} next"] = (
                f"n=$(systemctl show {timer} -p NextElapseUSecRealtime --value); "
                f'[ -z "$n" ] && n=$(systemd-analyze calendar {shlex.quote(resolved.schedule)} '
                f'| sed -n "s/^ *Next elapse: //p"); [ -n "$n" ] && echo "$n"'
            )
            svc = units.service_name(exe_name)
            p[f"{exe_name} run"] = (
                f"a=$(systemctl show {svc} -p ActiveState --value); "
                f"r=$(systemctl show {svc} -p Result --value); "
                f"t=$(systemctl show {svc} -p ExecMainExitTimestampMonotonic --value); "
                f'if [ "$a" = activating ]; then echo running; '
                f'elif [ "${{t:-0}}" != 0 ]; then [ "$r" = success ] && echo succeeded || echo failed; fi'
            )
        else:
            svc = units.service_name(exe_name)
            p[f"{exe_name} active"] = f"systemctl is-active {svc}"
            p[f"{exe_name} port"] = (
                f"{ssh.port_probe_line(ports[(exe_name, env_name)])} && echo open"
            )
            p[f"{exe_name} cpu"] = (
                f"a=$(systemctl show {svc} -p CPUUsageNSec --value); "
                f"sleep {_CPU_SAMPLE_SECS}; "
                f"b=$(systemctl show {svc} -p CPUUsageNSec --value); echo \"$a $b\""
            )
            p[f"{exe_name} mem"] = f"systemctl show {svc} -p MemoryCurrent --value"
            p[f"{exe_name} restarts"] = f"systemctl show {svc} -p NRestarts --value"
    p["box disk"] = f"df -P -B1 {layout.VAR_DIR} | awk 'NR==2{{print $3, $3+$4}}'"
    p["box mem"] = ("awk '/^MemTotal:/{t=$2}/^MemAvailable:/{a=$2}"
                    "END{print (t-a)*1024, t*1024}' /proc/meminfo")
    rel_files = " ".join(f"{rel_mod.RELEASES_DIR}/{e}/releases.jsonl" for e in exe_names)
    p["releases"] = (f'for f in {rel_files}; do [ -f "$f" ] && '
                     f'sed "s|^|$(basename "$(dirname "$f")")\t|" "$f"; done')
    for unit in _mech_units(conf, env_name):
        p[f"{unit} active"] = f"systemctl is-active {unit}"
    p["litestream active"] = f"systemctl is-active {units.LITESTREAM_UNIT}"
    p["litestream dbs"] = (
        f"sudo -n -u {ANCI_USER} {layout.LITESTREAM_BIN} list -socket {layout.LITESTREAM_SOCKET} -json "
        "| awk -F'\"' '/\"path\"/{if (p) print p \"\\t\" t; p=$4; t=\"\"} "
        "/\"last_sync_at\"/{t=$4; sub(/\\.[0-9]+/, \"\", t)} END{if (p) print p \"\\t\" t}'"
    )
    for db_name in conf.dbs:
        _db_probes(p, db_name)
    return p


def _db_probes(p: dict[str, str], db_name: str) -> None:
    f = layout.db_path(db_name)
    p[f"{db_name} last"] = (
        f"[ -f {f} ] && {dbs.sqlite3_cmd(f)} "
        f"'SELECT name FROM {dbs.BOOKKEEPING_TABLE} ORDER BY rowid DESC LIMIT 1;'"
    )
    p[f"{db_name} size"] = (
        f"[ -f {f} ] && echo $(( $(stat -c%s {f}) + $(stat -c%s {f}-wal 2>/dev/null || echo 0) ))"
    )


def _iso_utc(ts: str) -> str | None:
    if ts in ("", "-", "n/a"):
        return None
    if "UTC" not in ts:
        return ts
    try:
        _, rest = ts.split(" ", 1)
        dt = datetime.strptime(rest, "%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return ts
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(v: str | None) -> int | None:
    return int(v) if v and v.isdigit() else None


_CPU_SAMPLE_SECS = 0.4

_MEM_UNSET = (1 << 64) - 1


def _mem(v: str | None) -> int | None:
    n = _int(v)
    return n if n is not None and n != _MEM_UNSET else None


def _pair(sample: str | None) -> tuple[int, int] | None:
    if not sample:
        return None
    parts = sample.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def _cpu_percent(sample: str | None) -> float | None:
    if not sample:
        return None
    parts = sample.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    a, b = int(parts[0]), int(parts[1])
    return max(b - a, 0) / (_CPU_SAMPLE_SECS * 1e9) * 100


@dataclass
class ExeState:
    current: str | None = None
    active: bool = False
    state: str | None = None
    restarts: int | None = None
    port: bool = False
    timer: bool = False
    next: str | None = None
    run: str | None = None
    cpu: float | None = None
    mem: int | None = None


@dataclass
class MechState:
    active: str | None
    enabled: bool
    installed: bool


@dataclass
class DbState:
    last: str | None = None
    size: int | None = None
    replicating: bool = False
    replica_ts: str | None = None

    @property
    def exists(self) -> bool:
        return self.size is not None


@dataclass
class Report:
    state: dict[str, ExeState]
    releases: list[tuple[str, str]]
    mech: dict[str, MechState]
    dbs: dict[str, DbState]
    litestream_up: bool = False
    disk: tuple[int, int] | None = None
    mem: tuple[int, int] | None = None


def _read(observed: ObservedState, conf: Conf, exe_names: list[str],
          env_name: str) -> Report:
    found = observed.extra

    def fact(key):
        return ssh.fact(found, key)

    state = {}
    for e in exe_names:
        state[e] = ExeState(
            current=deployed(observed, e),
            active=fact(f"{e} active") == "active",
            state=fact(f"{e} active"),
            restarts=_int(fact(f"{e} restarts")),
            port=fact(f"{e} port") == "open",
            timer=fact(f"{e} timer") == "active",
            next=_iso_utc(fact(f"{e} next") or ""),
            run=fact(f"{e} run"),
            cpu=_cpu_percent(fact(f"{e} cpu")),
            mem=_mem(fact(f"{e} mem")),
        )
    mech = {}
    for unit in _mech_units(conf, env_name):
        if f"{unit} active" not in found:
            continue
        r = _SERVICES[unit]
        mech[unit] = MechState(active=fact(f"{unit} active"), enabled=r.starts_at_boot(observed),
                               installed=r.installed(observed))
    listed = dict((line.split("\t") + [""])[:2] for line in found.get("litestream dbs", []))
    dbs = {
        d: DbState(last=fact(f"{d} last"), size=_int(fact(f"{d} size")),
                   replicating=layout.db_path(d) in listed,
                   replica_ts=listed.get(layout.db_path(d)) or None)
        for d in conf.dbs
    }
    releases = [tuple(line.split("\t", 1)) for line in found.get("releases", [])
                if "\t" in line]
    return Report(state, releases, mech, dbs,
                  litestream_up=fact("litestream active") == "active",
                  disk=_pair(fact("box disk")), mem=_pair(fact("box mem")))


def _latest_release(
    release_lines: list[tuple[str, str]], exe_name: str, env_name: str
) -> dict | None:
    latest = None
    for exe_part, payload in release_lines:
        if exe_part != exe_name:
            continue
        try:
            d = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if d.get("env") == env_name and (latest is None or d.get("ts", "") > latest.get("ts", "")):
            latest = d
    return latest


def _box_changes(conf: Conf, env_name: str, observed: ObservedState) -> list[str]:
    try:
        target = target_state(conf, env_name)
    except errors.Error as err:
        return [f"couldn't work out what the box should have ({err.what})"]
    return [describe(c) for c in worth_showing(diff(observed, target))]


def _box_stat_lines(report: Report) -> list[str]:
    out = []
    for label, pair in (("disk", report.disk), ("mem", report.mem)):
        if pair is None:
            continue
        used, total = pair
        pct = round(used / total * 100) if total else 0
        out.append(f"  {ui.dim(label.ljust(4))} "
                   f"{ui.human_size(used)} / {ui.human_size(total)} ({pct}%)")
    return out


def _mech_check(unit: str, m: MechState | None) -> str:
    if m is None:
        return f"  {ui.dim('—')} {ui.dim(unit)}"
    raw = (m.active or "").strip()
    if not m.installed:
        state, up = "not installed", False
    elif raw == "active":
        state, up = "running", True
    elif raw == "inactive":
        state, up = "stopped", False
    elif raw == "failed":
        state, up = "failed", False
    else:
        state, up = (raw or "unknown"), False
    bullet = ui.green("✓") if up else ui.red("✗")
    line = f"  {bullet} {unit} — {state}"
    if m.installed and not m.enabled:
        line += f" {ui.yellow('· not enabled at boot')}"
    return line


def _parse_rfc3339(s: str) -> int | None:
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def _duration_behind(seconds: int) -> str:
    seconds = max(0, int(seconds))
    for div, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= div:
            return f"{seconds // div}{unit} since last sync"
    return f"{seconds}s since last sync"


def _behind(replica_ts: str | None) -> str | None:
    if not replica_ts:
        return None
    ts = _parse_rfc3339(replica_ts)
    if ts is None:
        return None
    return _duration_behind(int(time.time()) - ts)


def _replication_cell(conf: Conf, env_name: str, db_name: str, st: DbState,
                      litestream_up: bool = True) -> str:
    if not st.exists:
        return "—"
    if st.replicating:
        return _behind(st.replica_ts) or "litestream error"
    wanted = conf.dbs[db_name].resolve_for(env_name).replication is not None
    return "litestream error" if wanted and not litestream_up else "off"



def _replication_notes(conf: Conf, env_name: str, report: "Report") -> list[str]:
    notes = []
    for db_name, st in report.dbs.items():
        wanted = conf.dbs[db_name].resolve_for(env_name).replication is not None
        if st.replicating and not wanted:
            notes.append(f"{db_name} is still replicating, but it's off in anci.toml "
                         f"(anci {env_name} reconcile stops it)")
        elif wanted and st.exists and not st.replicating and report.litestream_up:
            notes.append(f"{db_name} isn't replicating yet, but it's on in anci.toml "
                         f"(anci {env_name} reconcile starts it)")
    return notes


def _unset_caddy_line() -> str:
    return f"  {ui.red('✗')} {ui.dim('caddy.service — not configured (no host)')}"


def status(conf: Conf, env_name: str, *, debug: bool = False) -> None:
    if env_name not in conf.envs:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    env = conf.envs[env_name]
    ports = allocate_ports(conf)
    local_ports = forward.installed_ports(conf, env_name)
    rows: list[list[str]] = []
    db_rows: list[list[str]] = []
    db_notes: list[str] = []
    drift: list[str] = []
    changes: list[str] = []
    mech_units = _mech_units(conf, env_name)
    serves_http = not env.local and any(
        conf.exes[e].resolve_for(env_name).schedule is None for e in conf.exes
    )
    unset_caddy = serves_http and not any(
        u == units.CADDY_UNIT for u in mech_units
    )
    mech_checks: list[str] = []
    parsed: Report | None = None
    down = False
    caller = auth.identify_user(conf)
    hint = box.ssh_hint(conf, env_name, caller)

    exe_names = list(conf.exes)
    db_names = list(conf.dbs)
    if not exe_names and not db_names:
        print(ui.dim("this conf defines no exes or dbs"))
        return
    if caller == "?" or caller not in env.access_users:
        for exe_name in exe_names:
            rows.append([exe_name, ui.dim("no access"), "—", "—", "—", "—"])
        for db_name in db_names:
            db_rows.append([db_name, "—", "—", "—"])
    else:
        try:
            conn = ssh.connect(conf, env.name, caller)
            observed = observed_state(conn, extra=_probes(conf, exe_names, env.name, ports))
            parsed = _read(observed, conf, exe_names, env.name)
            down = False
        except ssh.SSHError as e:
            if e.auth or e.host_key:
                raise
            down = bool(e.unreachable)
            if down:
                hint = None
            parsed = _read(ObservedState(), conf, exe_names, env.name)

        for exe_name in exe_names:
            if down:
                rows.append([exe_name, ui.red("box down"), "—", "—", "—", "—"])
                continue

            st = parsed.state[exe_name]
            blob = st.current
            scheduled = conf.exes[exe_name].resolve_for(env.name).schedule is not None

            if scheduled:
                if st.timer:
                    state_s = {"failed": ui.red("failed"), "running": ui.yellow("running"),
                               "succeeded": ui.green("succeeded")}.get(st.run, ui.green("scheduled"))
                    if st.next:
                        state_s += ui.dim(f" · next {st.next}")
                elif blob:
                    state_s = ui.red("timer stopped")
                else:
                    state_s = ui.dim("not deployed")
                url = "—"
            else:
                if st.active and st.port:
                    state_s = ui.green("serving")
                elif st.active:
                    state_s = ui.yellow("starting")
                elif blob and st.state == "activating":
                    state_s = ui.red(f"failing · restarted {st.restarts or 0}×")
                elif blob and st.state == "failed":
                    state_s = ui.red("failed")
                elif blob:
                    state_s = ui.red("stopped")
                else:
                    state_s = ui.dim("not deployed")
                url = (box.reachable_url(conf, exe_name, env.name, local_ports) or "—") \
                    if st.port else "—"

            if scheduled or not st.active:
                cpu_s = mem_s = "—"
            else:
                cpu_s = f"{st.cpu:.1f}%" if st.cpu is not None else "—"
                mem_s = ui.human_size(st.mem) if st.mem is not None else "—"

            latest = _latest_release(parsed.releases, exe_name, env.name)
            ts = latest["ts"] if latest else "—"
            author = latest.get("user", "—") if latest else "—"
            rows.append([exe_name, state_s, url, cpu_s, mem_s, f"{ts} by {author}"])

            if debug and blob:
                if latest and latest.get("blob") and \
                        layout.blob_id(latest["blob"]) != layout.blob_id(blob):
                    drift.append(f"{exe_name}: release log disagrees with the deployed version")

        if down:
            mech_checks = [_mech_check(u, None) for u in mech_units
                           if u != units.LITESTREAM_UNIT]
            for db_name in db_names:
                db_rows.append([db_name, "—", "—", "—"])
        else:
            for unit in mech_units:
                if unit == units.LITESTREAM_UNIT:
                    continue
                m = parsed.mech.get(unit)
                if m is not None:
                    mech_checks.append(_mech_check(unit, m))
            if debug:
                changes = _box_changes(conf, env.name, observed)
            for db_name in db_names:
                st = parsed.dbs[db_name]
                db_rows.append([
                    db_name,
                    f"{st.last}.sql" if st.last else "—",
                    ui.human_size(st.size) if st.size is not None else "—",
                    _replication_cell(conf, env_name, db_name, st, parsed.litestream_up),
                ])
            db_notes = _replication_notes(conf, env_name, parsed)
        if unset_caddy:
            mech_checks.append(_unset_caddy_line())
    if env.local and forward.healthy(conf, env_name) is False:
        mech_checks.append(f"  {ui.red('✗')} localhost forward — down "
                           f"{ui.dim(f'(anci {env_name} up reloads it; asks for sudo)')}")

    if rows:
        print(ui.table(["exe", "state", "url", "cpu", "mem", "last deploy"], rows))
    if db_rows:
        print()
        print(ui.table(["db", "last migration", "size", "replication"], db_rows))
        for note in db_notes:
            print(f"  {ui.yellow('⚠')} {note}")
    box_stats = _box_stat_lines(parsed) if parsed is not None and not down else []
    if box_stats or mech_checks or hint:
        print()
        for line in box_stats:
            print(line)
        for line in mech_checks:
            print(line)
        if hint:
            if box_stats or mech_checks:
                print()
            print(f"  {ui.dim('ssh with ')}{hint}")
    if debug:
        if drift or changes:
            print(f"\n  {ui.red('drift:')}")
            for c in changes:
                print(f"  {c}")
            for d in drift:
                print(f"  - {d}")
        else:
            print(f"\n  {ui.green('✓')} {ui.dim('no drift — the guest matches your conf')}")
