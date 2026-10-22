"""systemd units: an app's service, a job's oneshot + timer, and the two
mechanism units (caddy, litestream)."""

from __future__ import annotations

import shlex

from .. import layout


def _hardening(rw_paths: list[str]) -> tuple[str, ...]:
    """systemd hardening. The app may write **only** its db dirs (state) — plus
    the log path systemd appends to as root."""
    lines = [
        "",
        "# hardening",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
    ]
    if rw_paths:
        lines.append(f"ReadWritePaths={' '.join(rw_paths)}")
    lines += [
        "ReadWritePaths=-/var/log/anci",
        "PrivateTmp=true",
    ]
    return tuple(lines)


def _literal(value: str) -> str:
    """Escape systemd's `%` specifiers (`%o` is the OS id, `%h` a home dir, …), so a
    user's value reaches the app as written. `${VAR}` expansion is untouched."""
    return value.replace("%", "%%")


def _exec_start(target: str, args: list[str]) -> str:
    """`ExecStart` line body: the target, plus shell-quoted args."""
    if not args:
        return target
    return target + " " + " ".join(_literal(shlex.quote(a)) for a in args)


def _env_lines(environment: dict[str, str], env_file: str | None) -> list[str]:
    """`Environment=` lines (sorted, space-quoted) + an optional EnvironmentFile."""
    lines = []
    for key in sorted(environment):
        value = _literal(environment[key])
        # systemd's Environment= splits on spaces; quote values so app env-vars
        # with spaces survive (systemd strips the double quotes itself).
        if " " in value or any(c in value for c in "\"'\\$"):
            value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines.append(f"Environment={key}={value}")
    # leading dash = optional file: apps get secrets when the courier finds one
    if env_file:
        lines.append(f"EnvironmentFile=-{env_file}")
    return lines


def _log_lines(log_file: str | None) -> list[str]:
    """`StandardOutput/StandardError=append:` lines.

    systemd (as root) opens the file and appends; the app just writes to its
    inherited fd, so it needs no write access to the path.
    """
    if not log_file:
        return []
    return [
        f"StandardOutput=append:{log_file}",
        f"StandardError=append:{log_file}",
    ]


def _app_unit(*, exe_name: str, env_name: str, sysuser: str, target: str,
              args: list[str], environment: dict[str, str], env_file: str | None,
              log_file: str | None, rw_paths: list[str] | None,
              unit_extra: list[str], service_extra: list[str],
              service_tail: list[str], install: list[str]) -> str:
    """The shape every app unit shares; the service and the job differ only in
    the `*_extra` / `service_tail` / `install` lines."""
    lines = [
        f"# rendered by anci — do not edit (exe={exe_name} env={env_name})",
        "[Unit]",
        f"Description=anci: {exe_name} ({env_name})",
        "After=network.target",
        *unit_extra,
        "",
        "[Service]",
        *service_extra,
        f"User={sysuser}",
        f"ExecStart={_exec_start(target, args)}",
        *_log_lines(log_file),
        *_env_lines(environment, env_file),
        *service_tail,
        *_hardening(rw_paths or []),
        "",
        *install,
    ]
    return "\n".join(lines)


def render_unit(
    *,
    exe_name: str,
    env_name: str,
    sysuser: str,
    target: str,
    args: list[str],
    port: int,
    env_vars: dict[str, str],
    rw_paths: list[str] | None = None,
    env_file: str | None = None,
    bind: str = "127.0.0.1",
    url: str | None = None,
    log_file: str | None = None,
) -> str:
    """Render a hardened systemd unit for one exe-env. `url` (`ANCI_URL`) is where users
    reach it; None leaves it unset (nothing to open)."""
    url_var = {"ANCI_URL": url} if url else {}
    return _app_unit(
        exe_name=exe_name, env_name=env_name, sysuser=sysuser, target=target, args=args,
        environment={"ANCI_ENV": env_name, "ANCI_BIND": bind, "ANCI_PORT": str(port), **url_var,
                     **env_vars},
        env_file=env_file, log_file=log_file, rw_paths=rw_paths,
        unit_extra=[], service_extra=[],
        service_tail=["Restart=on-failure", "RestartSec=2"],
        install=["[Install]", "WantedBy=multi-user.target", ""],
    )


def render_oneshot(
    *,
    exe_name: str,
    env_name: str,
    sysuser: str,
    target: str,
    args: list[str],
    env_vars: dict[str, str],
    rw_paths: list[str] | None = None,
    env_file: str | None = None,
    log_file: str | None = None,
) -> str:
    """Render a Type=oneshot unit for a scheduled job.

    No `ANCI_PORT` (a job doesn't serve HTTP), no `Restart=` (oneshot runs once —
    the timer retries on the next tick), and no `[Install]` (the timer triggers
    it, not boot).
    """
    return _app_unit(
        exe_name=exe_name, env_name=env_name, sysuser=sysuser, target=target, args=args,
        environment={"ANCI_ENV": env_name, **env_vars},
        env_file=env_file, log_file=log_file, rw_paths=rw_paths,
        # a timer can trigger this far more than 5×/10s (systemd's default rate
        # limit); without this, sub-2s schedules get silently throttled
        unit_extra=["StartLimitIntervalSec=0"],
        service_extra=["Type=oneshot"],
        service_tail=[], install=[],
    )


def render_timer(*, exe_name: str, env_name: str, schedule: str) -> str:
    """Render the timer that triggers a scheduled job."""
    return "\n".join([
        f"# rendered by anci — do not edit (exe={exe_name} env={env_name})",
        "[Unit]",
        f"Description=anci: {exe_name} ({env_name}) timer",
        "",
        "[Timer]",
        f"OnCalendar={schedule}",
        "AccuracySec=1s",
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])


def render_logtrim_service() -> str:
    return f"""\
# rendered by anci — do not edit
[Unit]
Description=anci: keep app logs bounded

[Service]
Type=oneshot
ExecStart=/bin/sh {layout.LOGTRIM_SCRIPT}
ProtectSystem=strict
ReadWritePaths={layout.LOG_DIR}
"""


def render_logtrim_timer() -> str:
    return """\
# rendered by anci — do not edit
[Unit]
Description=anci: keep app logs bounded (hourly)

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
"""


def render_caddy_unit() -> str:
    return """\
# rendered by anci — do not edit
[Unit]
Description=Caddy web server (anci-managed)
After=network.target

[Service]
User=caddy
ExecStart=/usr/local/bin/caddy run --config /etc/anci/caddy/Caddyfile --adapter caddyfile
# a Caddyfile-only change is applied in place: no dropped connections, and a
# bad config is rejected while the old one keeps serving
ExecReload=/usr/local/bin/caddy reload --config /etc/anci/caddy/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=2
# binds 80/443 as the unprivileged `caddy` user — no setcap, no root
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


def render_litestream_unit() -> str:
    return f"""\
# rendered by anci — do not edit
[Unit]
Description=litestream (anci)
After=network.target

[Service]
# the shared `anci` user owns every db, so replication needs no root
User={layout.ANCI_USER}
RuntimeDirectory=litestream
ExecStart=/usr/local/bin/litestream replicate -config {layout.LITESTREAM_FILE}
EnvironmentFile=-{layout.SECRETS_FILE}
# on stop litestream retries its final sync; a sync that can't succeed must not
# hold a converge (or regen) for systemd's default 90s
TimeoutStopSec=15s
Restart=on-failure
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""
