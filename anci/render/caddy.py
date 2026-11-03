"""The Caddyfile: one site per exe `host` — TLS termination; cert management
is Caddy's problem (Let's Encrypt). Only remote envs have a `host`: a local
env's services are reached through the dev machine's localhost forward."""

from __future__ import annotations

from ..conf import Conf


SECURITY_HEADERS = (
    ("Strict-Transport-Security", "max-age=31536000"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
)


RESTART_GRACE = "3s"


def render_caddyfile(sites: list[tuple[str, int]]) -> str:
    lines = []
    for host, port in sorted(sites):
        lines.append(f"{host} {{")
        lines.append("    encode gzip zstd")
        lines.append("    header {")
        for name, value in SECURITY_HEADERS:
            lines.append(f'        ?{name} "{value}"')
        lines.append("    }")
        lines.append(f"    reverse_proxy 127.0.0.1:{port} {{")
        lines.append(f"        lb_try_duration {RESTART_GRACE}")
        lines.append("    }")
        lines.append("}")
    return "\n".join(lines) + "\n"


def caddy_sites(conf: Conf, env_name: str, ports: dict[tuple[str, str], int]) -> list[tuple[str, int]]:
    sites: list[tuple[str, int]] = []
    for exe_name in sorted(conf.exes):
        resolved = conf.exes[exe_name].resolve_for(env_name)
        if resolved.schedule is not None or not resolved.host:
            continue
        sites.append((resolved.host, ports[(exe_name, env_name)]))
    return sites


def caddyfile_for(conf: Conf, env_name: str, ports: dict[tuple[str, str], int]) -> str:
    return render_caddyfile(caddy_sites(conf, env_name, ports))
