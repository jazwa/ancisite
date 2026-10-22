"""`caddy` — only for an exe with a public `host` (remote envs)."""

import tempfile
import unittest

from anci.render import systemd
from anci.render import caddy as caddy_mod
from anci import layout
from anci.converge import caddy

from .support import file_at, link_at, load_conf, make_ctx

CONF_REMOTE = """
anci-version = '0.0.1'

[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.prod]
ip = '203.0.113.10'
mode = 'adopt'
access-users = ['jack']

[exe.app]
source = './app'

[exe.app.env.prod]
host = 'app.example.com'

[db.main-db]
migrations = './m'
"""

class TestCaddy(unittest.TestCase):
    def test_local_env_contributes_nothing(self):
        # a local env has no `host`: its services are reached through the localhost forward
        with tempfile.TemporaryDirectory() as td:
            c = caddy.contribute(make_ctx(load_conf(td)))
        self.assertEqual((c.files, c.downloads, c.after_apply), ([], [], []))

    def test_remote_without_host_contributes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, CONF_REMOTE.replace("[exe.app.env.prod]\nhost = 'app.example.com'\n", ""))
            c = caddy.contribute(make_ctx(conf, env_name="prod"))
        self.assertEqual((c.files, c.downloads, c.after_apply), ([], [], []))

    def test_remote_host_installs_unit_and_caddyfile(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, CONF_REMOTE)
            ctx = make_ctx(conf, env_name="prod")
            c = caddy.contribute(ctx)
            sites = caddy_mod.caddy_sites(conf, "prod", ctx.ports)
            self.assertEqual(sites, [("app.example.com", 8000)])
            self.assertEqual(file_at(c, layout.CADDY_FILE).content,
                             caddy_mod.render_caddyfile(sites))
            self.assertEqual(file_at(c, f"{layout.UNITS_DIR}/caddy.service").content,
                             systemd.render_caddy_unit())
            self.assertEqual(c.downloads, [])   # the binary ships with `vendored`
            self.assertEqual(link_at(c, "/etc/systemd/system/caddy.service").target,
                             f"{layout.UNITS_DIR}/caddy.service")
            self.assertEqual([a.phase for a in c.after_apply], [2])
            self.assertIn("useradd --system", "\n".join(c.prepare))

    def test_multiple_hosts_render_multiple_sites(self):
        # one exe = one domain (caddy serves many); no duplicate site blocks
        two = CONF_REMOTE + "\n[exe.blog]\nsource = './blog'\n[exe.blog.env.prod]\nhost = 'blog.example.com'\n"
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, two)
            sites = caddy_mod.caddy_sites(conf, "prod", make_ctx(conf, env_name="prod").ports)
        self.assertEqual({host for host, _port in sites}, {"app.example.com", "blog.example.com"})


class TestCaddyActivation(unittest.TestCase):
    """What a converge does to a running caddy, by what changed."""

    def _scripts(self, *, files=(), links=()):
        from unittest import mock
        from anci import layout, ssh
        from anci.apply import ApplyResult
        sudo = mock.MagicMock()
        run = caddy._activate(f"{layout.UNITS_DIR}/caddy.service", "/etc/systemd/system/caddy.service")
        with mock.patch.object(ssh, "sudo", sudo):
            status = run(None, ApplyResult(changed_files=list(files), changed_links=list(links)))
        return status, [c.args[1] for c in sudo.call_args_list]

    def test_nothing_changed_touches_nothing(self):
        status, scripts = self._scripts()
        self.assertEqual((status, scripts), ("unchanged", []))

    def test_caddyfile_only_reloads_in_place(self):
        from anci import layout
        status, scripts = self._scripts(files=[layout.CADDY_FILE])
        self.assertEqual(status, "reloaded")
        self.assertIn("validate", scripts[0])
        self.assertIn("systemctl reload caddy.service", scripts[0])
        # a stopped caddy is started, not reloaded
        self.assertIn("is-active --quiet caddy.service", scripts[0])
        self.assertIn("systemctl restart caddy.service", scripts[0])

    def test_binary_or_unit_change_restarts(self):
        from anci import layout
        for files in ([layout.CADDY_BIN], [f"{layout.UNITS_DIR}/caddy.service"]):
            status, scripts = self._scripts(files=files)
            self.assertEqual(status, "installed")
            self.assertIn("systemctl restart caddy.service", scripts[0])
            self.assertNotIn("reload caddy", scripts[0])
