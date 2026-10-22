"""What an env's box looks like from outside: its kind, its address, the
URLs and bind address its apps get, how to ssh into it, and the db paths
injected into every app. Pure — derived from the conf alone.
"""

from __future__ import annotations

import re

from . import errors, layout, net, paths
from .conf import Conf, Env, allocate_ports, port_var


def is_remote(env: Env) -> bool:
    """True for someone else's box (`mode = 'adopt'`) — no hypervisor."""
    return env.adopted


def is_remote_host(env: Env) -> bool:
    """True for a box anci manages on a remote hypervisor (`ip` = that host)."""
    return not env.adopted and env.ip != "127.0.0.1"


def resolve_env_ip(conf: Conf, env_name: str) -> str:
    """Address to ssh to for an env: an adopted box's own `ip`, or the
    address anci assigned to the env's VM (deterministic — see net.py)."""
    env = conf.envs[env_name]
    if env.adopted:
        return env.ip
    return net.env_ip(paths.box_id(conf), env.name)


def localhost_name(conf: Conf) -> str:
    """`<project>.localhost` — a local env's services on this dev machine. One name
    per project keeps projects' cookies apart (cookies ignore ports)."""
    project = paths.box_id(conf).rsplit("-", 1)[0]
    return (re.sub(r"[^a-z0-9-]+", "-", project.lower()).strip("-") or "project") + ".localhost"


def app_url(conf: Conf, exe_name: str, env_name: str,
            local_ports: dict[str, int] | None = None) -> str:
    """The URL an app is (or will be) reachable at (`ANCI_URL`).

    A local env's service is forwarded to this machine's localhost (`local_ports`:
    exe -> its forwarded port; without one, the VM's address); a remote one is
    served at its `host`, or is internal to its box without one."""
    if env_name not in conf.envs:
        raise errors.ConfError(f"env '{env_name}' isn't in anci.toml")
    if exe_name not in conf.exes:
        raise errors.ConfError(f"exe '{exe_name}' isn't in anci.toml")
    resolved = conf.exes[exe_name].resolve_for(env_name)
    if resolved.schedule is not None:
        raise errors.ConfError(
            f"exe '{exe_name}' is a scheduled job — it has no URL",
            try_=f"use 'anci {env_name} status' to see when '{exe_name}' runs",
        )
    port = allocate_ports(conf)[(exe_name, env_name)]
    if conf.envs[env_name].local:
        lport = (local_ports or {}).get(exe_name)
        if lport:
            return f"http://{localhost_name(conf)}:{lport}"
        return f"http://{resolve_env_ip(conf, env_name)}:{port}"
    if resolved.host:
        return f"https://{resolved.host}"
    return f"http://127.0.0.1:{port}"


def bind_address(conf: Conf, exe_name: str, env_name: str) -> str:
    """What an app should bind to (`ANCI_BIND`): all addresses on a local env (the
    dev machine's localhost forward reaches it at the VM's address), loopback
    elsewhere (Caddy fronts a `host`; without one a service is internal)."""
    return "0.0.0.0" if conf.envs[env_name].local else "127.0.0.1"


def reachable_url(conf: Conf, exe_name: str, env_name: str,
                  local_ports: dict[str, int] | None = None) -> str | None:
    """The URL a user can actually open, or None when there's nothing to open
    (a remote service without a `host` is only reachable from its own box)."""
    if conf.envs[env_name].local or conf.exes[exe_name].resolve_for(env_name).host:
        return app_url(conf, exe_name, env_name, local_ports)
    return None


def ssh_hint(conf: Conf, env_name: str, user: str) -> str | None:
    """`ssh you@192.168.122.83` — how to log into this env's box."""
    if env_name not in conf.envs:
        return None
    if user == "?" or user not in conf.envs[env_name].access_users:
        return None
    env = conf.envs[env_name]
    # a managed box on a remote hypervisor is on that host's NAT: hop through it
    jump = f"-J {user}@{env.ip} " if is_remote_host(env) else ""
    return f"ssh {jump}{user}@{resolve_env_ip(conf, env_name)}"


def env_var_prefix(db_name: str) -> str:
    return "ANCI_DB_" + db_name.replace("-", "_").upper()


def port_env_vars(env_name: str, ports: dict[tuple[str, str], int]) -> dict[str, str]:
    """ANCI_PORT_<EXE> for every serving exe on this env, injected into every exe
    (so one exe can reach another at 127.0.0.1 without hardcoding its port)."""
    return {port_var(exe): str(port) for (exe, env), port in sorted(ports.items())
            if env == env_name}


def db_env_vars(conf: Conf, env_name: str) -> dict[str, str]:
    """All ANCI_DB_* vars injected into every exe on this env (implicit)."""
    out: dict[str, str] = {}
    for db in conf.dbs.values():
        prefix = env_var_prefix(db.name)
        out[f"{prefix}_PATH"] = layout.db_path(db.name)
        if db.extensions:
            out[f"{prefix}_EXT_DIR"] = layout.ext_dir(db.name)
    return out
