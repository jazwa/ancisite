"""The single-instance resources: each is a fixed set of files and links, plus
the restart or reload that makes the box pick them up."""

from __future__ import annotations

import shlex

from .. import edge, errors, layout, secrets as secrets_mod, units, versions as versions_mod
from ..box import is_remote_host
from ..conf import allocate_ports
from ..render import caddy as caddy_mod, litestream as litestream_mod, systemd, template
from .core import FileSet, Step, daemon_reload
from .exe import exe_restart, service_names


SSHD_DROPIN = "/etc/ssh/sshd_config.d/00-anci.conf"

SSHD_CONTENT = """\
PasswordAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
"""

_SSHD_OFF = ("passwordauthentication", "kbdinteractiveauthentication",
             "challengeresponseauthentication")
_ROOT_OK = ("prohibit-password", "without-password", "no")

_SSHD_PATH = "PATH=$PATH:/usr/sbin:/sbin\ncommand -v sshd >/dev/null || { echo no-sshd; exit 0; }\n"


def _sshd_invalid(lines: list[str]) -> None:
    if lines and lines[0].startswith("invalid "):
        raise errors.EnvError(
            "sshd rejected its config with anci's key-only drop-in, so anci removed it",
            why="\n".join([lines[0][len("invalid "):], *lines[1:]]).strip()[-500:],
            try_="fix the sshd config on the box (`sudo sshd -t` shows the error), then re-run",
        )


def _sshd_enforced(lines: list[str]) -> None:
    if lines == ["no-sshd"]:
        return
    found = dict(line.split(" ", 1) for line in lines if " " in line)
    still_on = [f"{k} yes" for k in _SSHD_OFF if found.get(k) == "yes"]
    if found.get("permitrootlogin", "no") not in _ROOT_OK:
        still_on.append(f"permitrootlogin {found['permitrootlogin']}")
    if still_on:
        raise errors.EnvError(
            "sshd on the box still accepts password logins",
            why=f"`sshd -T` reports {', '.join(still_on)}\n"
                f"a setting read before {SSHD_DROPIN} wins, or sshd_config doesn't include "
                "/etc/ssh/sshd_config.d/",
            try_="remove that setting from sshd_config or an earlier-named drop-in in "
                 "/etc/ssh/sshd_config.d/ (or add the Include near the top of sshd_config), then re-run",
        )


def sshd_check() -> Step:
    return Step("sshd/check", _SSHD_PATH + "sshd -T", check=_sshd_enforced)


class Sshd(FileSet):
    name = "sshd"
    files = {"dropin": SSHD_DROPIN}
    marker = f"{layout.VAR_DIR}/.reload-sshd"
    removable = False
    rules = (("sshd/*", "sshd/check"),)

    def always(self, ctx):
        return [sshd_check()]

    def contents(self, conf, env_name):
        return {"dropin": SSHD_CONTENT}

    def activate(self, rows, ctx):
        q = shlex.quote
        return [Step("sshd/reload", after=("sshd/write",), check=_sshd_invalid, script=_SSHD_PATH + "\n".join([
            f'if ! out=$(sshd -t 2>&1); then rm -f {q(SSHD_DROPIN)}; echo "invalid $out"; exit 0; fi',
            'for u in ssh.service sshd.service; do',
            '  if systemctl is-active --quiet "$u"; then systemctl reload "$u"; break; fi',
            'done',
            f"rm -f {q(self.marker)}",
        ]))]


RESOLVED_DROPIN = "/etc/systemd/resolved.conf.d/anci.conf"

RESOLVED_CONTENT = """\
[Resolve]
LLMNR=no
"""


class Resolved(FileSet):
    name = "resolved"
    files = {"dropin": RESOLVED_DROPIN}
    marker = f"{layout.VAR_DIR}/.restart-resolved"
    removable = False

    def contents(self, conf, env_name):
        return {"dropin": RESOLVED_CONTENT}

    def activate(self, rows, ctx):
        unit = "systemd-resolved.service"
        return [Step("resolved/restart",
                     f"if systemctl is-active --quiet {unit}; then systemctl restart {unit}; fi\n"
                     f"rm -f {shlex.quote(self.marker)}", after=("resolved/write",))]


LOG_LIMIT = 1 << 30


def logtrim_script() -> str:
    return template("logtrim.sh", LOG_DIR=layout.LOG_DIR, LIMIT=str(LOG_LIMIT))


class Logtrim(FileSet):
    name = "logtrim"
    files = {"script": layout.LOGTRIM_SCRIPT,
             "service": layout.unit_path(units.LOGTRIM_SERVICE),
             "timer": layout.unit_path(units.LOGTRIM_TIMER)}
    modes = {"script": "0755"}
    links = {"service_link": (layout.unit_link(units.LOGTRIM_SERVICE),
                              layout.unit_path(units.LOGTRIM_SERVICE)),
             "timer_link": (layout.unit_link(units.LOGTRIM_TIMER),
                            layout.unit_path(units.LOGTRIM_TIMER))}
    unit = units.LOGTRIM_TIMER
    boot = (f"timers.target.wants/{units.LOGTRIM_TIMER}",)
    marker = f"{layout.VAR_DIR}/.enable-logtrim"
    removable = False

    def contents(self, conf, env_name):
        return {"script": logtrim_script(),
                "service": systemd.render_logtrim_service(),
                "timer": systemd.render_logtrim_timer()}

    def activate(self, rows, ctx):
        return [Step("logtrim/arm", f"systemctl enable --now {units.LOGTRIM_TIMER} >/dev/null\n"
                                    f"rm -f {shlex.quote(self.marker)}", after=("logtrim/write",))]


def _pin(conf, env_name: str):
    return versions_mod.get(versions_mod.load(), conf.envs[env_name].anci_version)


CADDY_MARKER = f"{layout.VAR_DIR}/.restart-caddy"


def caddy_restart() -> Step:
    u = units.CADDY_UNIT
    return Step("caddy/restart", f"systemctl enable {u} >/dev/null\nsystemctl restart {u}\n"
                                 f"rm -f {CADDY_MARKER}", after=("caddy/write",),
                says="caddy restarted")


def uses_caddy(conf, env_name: str) -> bool:
    return bool(caddy_mod.caddy_sites(conf, env_name, allocate_ports(conf)))


class Caddy(FileSet):
    name = "caddy"
    files = {"unit": layout.unit_path(units.CADDY_UNIT), "config": layout.CADDY_FILE}
    links = {"unit_link": (layout.unit_link(units.CADDY_UNIT), layout.unit_path(units.CADDY_UNIT))}
    unit = units.CADDY_UNIT
    boot = (f"multi-user.target.wants/{units.CADDY_UNIT}",)
    marker = CADDY_MARKER
    rules = (("caddy/remove", "box/daemon-reload"),)

    @property
    def rows(self):
        return {**super().rows, "config": "config"}

    def wanted(self, conf, env_name):
        return uses_caddy(conf, env_name)

    def contents(self, conf, env_name):
        if not self.wanted(conf, env_name):
            return None
        if not _pin(conf, env_name).caddy_url:
            raise errors.EnvError(
                "the pinned anci-version doesn't ship a caddy binary URL",
                try_="update the versions map, or remove 'host' from your exes",
            )
        return {"unit": systemd.render_caddy_unit(),
                "config": caddy_mod.caddyfile_for(conf, env_name, allocate_ports(conf))}

    def steps(self, change, ctx):
        if is_remote_host(ctx.conf.envs[ctx.env_name]):
            ctx.elsewhere.append(("edge", lambda: edge.reconcile(ctx.conf, ctx.env_name, ctx.user)))
        rows = self.rows_for(change)
        if "remove" in rows:
            return [self.remove_step(), daemon_reload()]
        out = []
        if change.action.name == "CREATE":
            out.append(Step("caddy/user",
                            "id -u caddy >/dev/null 2>&1 || useradd --system --home /var/lib/caddy "
                            "--create-home --shell /usr/sbin/nologin caddy\n"
                            f"install -d {layout.CADDY_DIR} /var/lib/caddy && "
                            "chown -R caddy:caddy /var/lib/caddy"))
        if rows & {"write", "config"}:
            out.append(Step("caddy/write", self.write_step(ctx).script, after=("caddy/user",)))
        if "write" in rows:
            out.append(daemon_reload())
        if rows & {"write", "pending"}:
            out.append(caddy_restart())
        elif "config" in rows:
            u = units.CADDY_UNIT
            out.append(Step("caddy/reload",
                            f"{layout.CADDY_BIN} validate --config {layout.CADDY_FILE} "
                            f"--adapter caddyfile >/dev/null\n"
                            f"if systemctl is-active --quiet {u}; then systemctl reload {u}; "
                            f"else systemctl restart {u}; fi\nrm -f {CADDY_MARKER}",
                            after=("caddy/write",), says="caddy reloaded"))
        if "boot" in rows:
            out.append(Step("caddy/enable", f"systemctl enable {units.CADDY_UNIT} >/dev/null"))
        return out


LITESTREAM_MARKER = f"{layout.VAR_DIR}/.restart-litestream"


def litestream_stop() -> Step:
    return Step("litestream/stop", f"systemctl stop {units.LITESTREAM_UNIT} 2>/dev/null || true")


def _any_db_file(conf, env_name: str) -> str | None:
    dbs = conf.replicated_dbs(env_name)
    if not dbs:
        return None
    return " || ".join(f"[ -f {layout.db_path(d.name)} ]" for d in dbs)


def litestream_start(ctx) -> Step | None:
    any_db = _any_db_file(ctx.conf, ctx.env_name)
    if any_db is None:
        return None
    u = units.LITESTREAM_UNIT
    return Step("litestream/start", f"[ -f {layout.unit_path(u)} ] || exit 0\n"
                                    f"systemctl enable {u} >/dev/null\n"
                                    f"if {any_db}; then systemctl start {u}; fi\n"
                                    f"rm -f {LITESTREAM_MARKER}", says="litestream updated")


def litestream_restart(ctx) -> list[Step]:
    start = litestream_start(ctx)
    return [litestream_stop(), *([start] if start else [])]


def litestream_ensure(conf, env_name: str) -> Step | None:
    any_db = _any_db_file(conf, env_name)
    if any_db is None:
        return None
    u = units.LITESTREAM_UNIT
    return Step("litestream/ensure", f"[ -f {layout.unit_path(u)} ] || exit 0\n"
                                     f"if {any_db}; then systemctl is-active --quiet {u} || "
                                     f"systemctl start {u}; fi")


class Litestream(FileSet):
    name = "litestream"
    files = {"unit": layout.unit_path(units.LITESTREAM_UNIT), "config": layout.LITESTREAM_FILE}
    links = {"unit_link": (layout.unit_link(units.LITESTREAM_UNIT),
                           layout.unit_path(units.LITESTREAM_UNIT))}
    unit = units.LITESTREAM_UNIT
    boot = (f"multi-user.target.wants/{units.LITESTREAM_UNIT}",)
    marker = LITESTREAM_MARKER
    labels = {"config": "replication config", "unit": "replication unit"}
    rules = (("litestream/remove", "box/daemon-reload"),
             ("litestream/*", "litestream/ensure"))

    def always(self, ctx):
        ensure = litestream_ensure(ctx.conf, ctx.env_name)
        return [ensure] if ensure else []

    def wanted(self, conf, env_name):
        return bool(conf.replicated_dbs(env_name))

    def contents(self, conf, env_name):
        if not self.wanted(conf, env_name):
            return None
        if not _pin(conf, env_name).litestream_url:
            raise errors.EnvError(
                "the pinned anci-version doesn't ship a litestream binary URL",
                try_="update the versions map, or remove 'replication' from your dbs",
            )
        return {"unit": systemd.render_litestream_unit(),
                "config": litestream_mod.render_litestream_yaml(conf.replicated_dbs(env_name))}

    def steps(self, change, ctx):
        rows = self.rows_for(change)
        if "remove" in rows:
            return [self.remove_step(), daemon_reload()]
        out = litestream_restart(ctx)
        if "write" in rows:
            out += [Step("litestream/write", self.write_step(ctx).script, after=("litestream/stop",)),
                    daemon_reload()]
        return out


def _secrets_restarts(ctx) -> list[Step]:
    out = [exe_restart(name) for name in service_names(ctx)]
    if "litestream" in ctx.target and "litestream" in ctx.observed:
        out += litestream_restart(ctx)
    return out


def _markers(ctx) -> list[str]:
    out = [f"{layout.exe_home(n)}/.restart-pending" for n in service_names(ctx)]
    if "litestream" in ctx.target and "litestream" in ctx.observed:
        out.append(LITESTREAM_MARKER)
    return out


class Secrets(FileSet):
    name = "secrets"
    files = {"file": layout.SECRETS_FILE}
    modes = {"file": layout.SECRET_MODE}
    labels = {"file": "secrets"}
    rules = (("secrets/write", "*/restart"), ("secrets/write", "*/start"),
             ("*/restart", "secrets/remove"), ("*/start", "secrets/remove"))

    def wanted(self, conf, env_name):
        return secrets_mod.has_secrets(conf, env_name)

    def contents(self, conf, env_name):
        content, _ = secrets_mod.resolve_secrets(conf, env_name)
        return None if content is None else {"file": content}

    def steps(self, change, ctx):
        rows = self.rows_for(change)
        restarts = _secrets_restarts(ctx)
        if "remove" in rows:
            return [Step("secrets/remove", f"rm -f {layout.SECRETS_FILE}"), *restarts]
        q = shlex.quote
        touch = [f"touch {' '.join(q(m) for m in _markers(ctx))}"] if _markers(ctx) else []
        write = self.write_step(ctx)
        return [Step("secrets/write", "\n".join([*touch, write.script])), *restarts]


def optional_services() -> list[FileSet]:
    return [Caddy(), Litestream()]


def env_paths(conf, env_name: str) -> list[tuple[str, str]]:
    rows = [(p, f"{what} (every exe)") for p, what in Secrets().paths()]
    if Litestream().wanted(conf, env_name):
        rows += Litestream().paths()
    return rows
