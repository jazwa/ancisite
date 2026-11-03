"""What an env's box looks like from outside: its kind, its address, the
URLs and bind address its apps get, how to ssh into it, and the db paths
injected into every app. Pure — derived from the conf alone.
"""

from __future__ import annotations

import re

from . import errors, layout, net, paths
from .conf import Conf, Env, allocate_ports, port_var


def is_remote(env: Env) -> bool:
    return env.adopted


def is_remote_host(env: Env) -> bool:
    return not env.adopted and env.ip != "127.0.0.1"


def resolve_env_ip(conf: Conf, env_name: str) -> str:
    env = conf.envs[env_name]
    if env.adopted:
        return env.ip
    return net.env_ip(paths.box_id(conf), env.name)


def localhost_name(conf: Conf) -> str:
    project = paths.box_id(conf).rsplit("-", 1)[0]
    return (re.sub(r"[^a-z0-9-]+", "-", project.lower()).strip("-") or "project") + ".localhost"


def app_url(conf: Conf, exe_name: str, env_name: str,
            local_ports: dict[str, int] | None = None) -> str:
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
    return "0.0.0.0" if conf.envs[env_name].local else "127.0.0.1"


def reachable_url(conf: Conf, exe_name: str, env_name: str,
                  local_ports: dict[str, int] | None = None) -> str | None:
    if conf.envs[env_name].local or conf.exes[exe_name].resolve_for(env_name).host:
        return app_url(conf, exe_name, env_name, local_ports)
    return None


def ssh_hint(conf: Conf, env_name: str, user: str) -> str | None:
    if env_name not in conf.envs:
        return None
    if user == "?" or user not in conf.envs[env_name].access_users:
        return None
    env = conf.envs[env_name]
    jump = f"-J {user}@{env.ip} " if is_remote_host(env) else ""
    return f"ssh {jump}{user}@{resolve_env_ip(conf, env_name)}"


def env_var_prefix(db_name: str) -> str:
    return "ANCI_DB_" + db_name.replace("-", "_").upper()


def port_env_vars(env_name: str, ports: dict[tuple[str, str], int]) -> dict[str, str]:
    return {port_var(exe): str(port) for (exe, env), port in sorted(ports.items())
            if env == env_name}


def db_env_vars(conf: Conf, env_name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for db in conf.dbs.values():
        prefix = env_var_prefix(db.name)
        out[f"{prefix}_PATH"] = layout.db_path(db.name)
        if db.extensions:
            out[f"{prefix}_EXT_DIR"] = layout.ext_dir(db.name)
    return out
