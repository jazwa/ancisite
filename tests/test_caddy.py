import unittest

from anci import versions as vmod
from anci.render.caddy import SECURITY_HEADERS, render_caddyfile
from anci.render.systemd import render_caddy_unit


def load_entry():
    return vmod.get(vmod.load(), "0.0.1")


class TestVersionsCaddy(unittest.TestCase):
    def test_pinned_caddy_has_url_and_checksum(self):
        e = load_entry()
        self.assertEqual(e.caddy, "2.11.7")
        self.assertTrue(e.caddy_url.startswith("https://github.com/caddyserver/caddy/releases/"))
        self.assertEqual(len(e.caddy_sha512), 128)


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
