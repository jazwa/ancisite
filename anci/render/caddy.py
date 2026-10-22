"""The Caddyfile: one site per exe `host` — TLS termination; cert management
is Caddy's problem (Let's Encrypt). Only remote envs have a `host`: a local
env's services are reached through the dev machine's localhost forward."""

from __future__ import annotations

from ..conf import Conf


# Security headers every site sends. Generic, so nothing here can break an app:
# HSTS without includeSubDomains/preload (anci doesn't own the subdomains), no
# CSP (anci can't know an app's inline scripts), X-Frame-Options SAMEORIGIN
# rather than DENY. The app stays free to override any of these on its own
# responses — Caddy's `header` defers to a header the upstream already set.
SECURITY_HEADERS = (
    ("Strict-Transport-Security", "max-age=31536000"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
)


# how long a request waits for an app that's restarting: about 10× a typical
# restart, and short enough that a crashed app still answers (502) quickly
RESTART_GRACE = "3s"


def render_caddyfile(sites: list[tuple[str, int]]) -> str:
    """(host, upstream_port) pairs → one Caddyfile per box. Every site compresses its
    responses (`encode` skips ones that already carry a Content-Encoding) and sends
    the security headers above.

    A box on a remote hypervisor host is reached through the host's DNAT edge
    (see edge.py) — the client address survives, so nothing differs here.
    """
    lines = ["# rendered by anci — do not edit"]
    for host, port in sorted(sites):
        lines.append(f"{host} {{")
        lines.append("    encode gzip zstd")
        lines.append("    header {")
        for name, value in SECURITY_HEADERS:
            # `?` sets the header only when the upstream didn't, so an app can override
            lines.append(f'        ?{name} "{value}"')
        lines.append("    }")
        # while the app restarts (nothing listening), hold the request and retry
        # the connection instead of answering 502 at once — retried only when it
        # couldn't connect, so the app never sees a request twice
        lines.append(f"    reverse_proxy 127.0.0.1:{port} {{")
        lines.append(f"        lb_try_duration {RESTART_GRACE}")
        lines.append("    }")
        lines.append("}")
    return "\n".join(lines) + "\n"


def caddy_sites(conf: Conf, env_name: str, ports: dict[tuple[str, str], int]) -> list[tuple[str, int]]:
    """The (host, upstream_port) sites this env routes through caddy.

    One site per exe that sets a `host` (caddy serves many domains). Shared by
    the caddy reconciler and the edge (which publishes only when a site exists).
    """
    sites: list[tuple[str, int]] = []
    for exe_name in sorted(conf.exes):
        resolved = conf.exes[exe_name].resolve_for(env_name)
        if resolved.schedule is not None or not resolved.host:
            continue   # a job serves no HTTP; no host = no caddy
        sites.append((resolved.host, ports[(exe_name, env_name)]))
    return sites


def caddyfile_for(conf: Conf, env_name: str, ports: dict[tuple[str, str], int]) -> str:
    """The Caddyfile this env's box should have — shared by converge and status."""
    return render_caddyfile(caddy_sites(conf, env_name, ports))
