import unittest

from anci.conf import Conf, Env, Exe, ExeOverride, allocate_ports
from anci.render.caddy import SECURITY_HEADERS, caddyfile_for, render_caddyfile
from anci.render.systemd import render_caddy_unit


class TestRenderCaddyfile(unittest.TestCase):
    def test_sites_sorted_with_ports(self):
        out = render_caddyfile([("z.example.com", 8001), ("a.example.com", 8000)])
        lines = out.splitlines()
        self.assertLess(lines.index("a.example.com {"), lines.index("z.example.com {"))
        self.assertIn("reverse_proxy 127.0.0.1:8000", out)
        self.assertIn("reverse_proxy 127.0.0.1:8001", out)
        self.assertNotIn("tls ", out)     # Let's Encrypt
        self.assertEqual(out.count("encode gzip zstd"), 2)   # every site compresses
        self.assertEqual(out.count("lb_try_duration"), 2)    # every site rides out a restart
        self.assertTrue(out.endswith("}\n"))

    def test_every_site_sends_security_headers(self):
        out = render_caddyfile([("a.example.com", 8000), ("b.example.com", 8001)])
        self.assertEqual(out.count("    header {"), 2)   # one block per site
        # set as a default (?) so an app can still override on its own responses
        self.assertIn('        ?Strict-Transport-Security "max-age=31536000"', out)
        self.assertIn('        ?X-Content-Type-Options "nosniff"', out)
        self.assertIn('        ?X-Frame-Options "SAMEORIGIN"', out)
        self.assertIn('        ?Referrer-Policy "strict-origin-when-cross-origin"', out)
        self.assertNotIn("Content-Security-Policy", out)   # generic tool: no CSP guess
        self.assertNotIn("includeSubDomains", out)         # anci doesn't own subdomains

    def test_header_names_are_unique(self):
        self.assertEqual(len({name for name, _ in SECURITY_HEADERS}), len(SECURITY_HEADERS))

    def test_empty_sites_is_empty(self):
        self.assertEqual(render_caddyfile([]).strip(), "")


class TestCaddyfileFor(unittest.TestCase):
    def test_remote_site_uses_acme_without_proxy_protocol(self):
        conf = Conf(
            users={},
            envs={"prod": Env(name="prod", ip="10.0.0.5", access_users=[], anci_version="0.0.1")},
            exes={"web": Exe(name="web", source="./s", env_overrides={
                "prod": ExeOverride(env="prod", host="app.example.com")})},
        )
        text = caddyfile_for(conf, "prod", allocate_ports(conf))
        self.assertIn("app.example.com {", text)
        self.assertNotIn("proxy_protocol", text)
        self.assertNotIn("tls ", text)   # Let's Encrypt


class TestRenderCaddyUnit(unittest.TestCase):
    def test_binds_privileged_ports_as_unprivileged_user(self):
        unit = render_caddy_unit()
        self.assertIn("User=caddy", unit)
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ExecStart=/usr/local/bin/caddy run --config /etc/anci/caddy/Caddyfile", unit)
        self.assertIn("ExecReload=/usr/local/bin/caddy reload --config /etc/anci/caddy/Caddyfile", unit)


if __name__ == "__main__":
    unittest.main()
