"""A local env's localhost forward: nft ruleset, port groups, and writing only on change."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import forward
from anci.conf import load
from anci.render.fwd import render_ruleset
from tests import REAL_RECONCILE

CONF = """
anci-version = '0.0.1'

[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdd5266e9506c16ebf7e12ffa7545d4c41feb0b2322d jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { vcpus = 2, mem = '1gb', disk = '20gb' }

[env.prod]
ip = '203.0.113.10'
mode = 'adopt'
access-users = ['jack']

[exe.app]
source = './app'

[exe.blog]
source = './blog'

[exe.job]
source = './job'
schedule = 'daily'
"""


def _conf(td: str):
    p = Path(td) / "anci.toml"
    p.write_text(CONF)
    return load(str(p))


class TestRuleset(unittest.TestCase):
    def setUp(self):
        self.text = render_ruleset("anci_fwd_x_dev", 3, "192.168.122.9", "virbr0",
                                   [(20030, 8000), (20031, 8010)])

    def test_rewrites_only_what_this_machine_sends(self):
        self.assertIn("ip daddr 127.0.0.1 tcp dport 20030 dnat to 192.168.122.9:8000", self.text)
        self.assertIn("ip daddr 127.0.0.1 tcp dport 20031 dnat to 192.168.122.9:8010", self.text)
        self.assertIn("type nat hook output", self.text)
        self.assertNotIn("type nat hook prerouting", self.text)   # nothing inbound is rewritten

    def test_guards_this_machines_loopback_from_the_vm(self):
        self.assertIn('iifname "virbr0" ip daddr 127.0.0.0/8 drop', self.text)

    def test_header_records_group_and_bridge(self):
        self.assertIn("# group 3\n", self.text)
        self.assertIn("# bridge virbr0\n", self.text)

    def test_replaces_its_table_atomically(self):
        lines = self.text.splitlines()
        self.assertLess(lines.index("delete table ip anci_fwd_x_dev"),
                        lines.index("table ip anci_fwd_x_dev {"))


class TestGroups(unittest.TestCase):
    def test_first_env_takes_group_zero_and_keeps_it(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fwd:
            conf = _conf(td)
            self.assertEqual(forward.group(conf, "dev", fwd), 0)
            forward.ruleset_path(conf, "dev", fwd).write_text("# group 4\n")
            self.assertEqual(forward.group(conf, "dev", fwd), 4)

    def test_lowest_free_group(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fwd:
            for name, g in (("a", 0), ("b", 2)):
                (Path(fwd) / f"{name}.nft").write_text(f"# group {g}\n")
            self.assertEqual(forward.group(_conf(td), "dev", fwd), 1)

    def test_full_block_is_an_error(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fwd:
            for g in range(forward.GROUPS):
                (Path(fwd) / f"e{g}.nft").write_text(f"# group {g}\n")
            with self.assertRaises(forward.errors.EnvError) as cm:
                forward.group(_conf(td), "dev", fwd)
        self.assertIn("no localhost ports left", str(cm.exception))

    def test_ports_follow_app_port_slots(self):
        with tempfile.TemporaryDirectory() as td:
            # app → 8000 (slot 0), blog → 8010 (slot 1); the job has no port
            self.assertEqual(forward.ports_for(_conf(td), "dev", 2), {"app": 20020, "blog": 20021})

    def test_installed_ports(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fwd:
            conf = _conf(td)
            self.assertEqual(forward.installed_ports(conf, "dev", fwd), {})
            forward.ruleset_path(conf, "dev", fwd).write_text("# group 1\n")
            self.assertEqual(forward.installed_ports(conf, "dev", fwd), {"app": 20010, "blog": 20011})
            self.assertEqual(forward.installed_ports(conf, "prod", fwd), {})


class TestReconcile(unittest.TestCase):
    def _run(self, td, installed: dict[str, str], loaded: bool = True):
        conf = _conf(td)
        hv = mock.MagicMock()
        with mock.patch.object(forward, "_read", side_effect=lambda p: installed.get(str(p))), \
             mock.patch.object(forward, "healthy", return_value=loaded), \
             mock.patch.object(forward, "group", return_value=0), \
             mock.patch.object(forward, "_bridge", return_value="virbr0"), \
             mock.patch.object(forward, "_have_nft", return_value=True), \
             mock.patch("anci.hypervisor.hypervisor", return_value=hv), \
             contextlib.redirect_stdout(io.StringIO()):
            ports = REAL_RECONCILE(conf, "dev")
        return conf, ports, [c.args[0] for c in hv.root.call_args_list]

    def test_first_run_writes_everything_and_applies(self):
        with tempfile.TemporaryDirectory() as td:
            _conf_, ports, scripts = self._run(td, {})
        self.assertEqual(ports, {"app": 20000, "blog": 20001})
        self.assertEqual(len([s for s in scripts if "cat >" in s]), 3)
        self.assertEqual(scripts[-1], forward.APPLY_SCRIPT)

    def test_unchanged_needs_no_sudo(self):
        with tempfile.TemporaryDirectory() as td:
            conf = _conf(td)
            with mock.patch.object(forward, "_bridge", return_value="virbr0"):
                files = forward.desired(conf, "dev", 0)
            _c, ports, scripts = self._run(td, {p: t for p, (t, _m) in files.items()})
        self.assertEqual(scripts, [])
        self.assertEqual(ports, {"app": 20000, "blog": 20001})

    def test_unchanged_but_unloaded_reapplies(self):
        # after a reboot whose libvirt hook didn't load the forward
        with tempfile.TemporaryDirectory() as td:
            conf = _conf(td)
            with mock.patch.object(forward, "_bridge", return_value="virbr0"):
                files = forward.desired(conf, "dev", 0)
            _c, _p, scripts = self._run(td, {p: t for p, (t, _m) in files.items()}, loaded=False)
        self.assertEqual(scripts, [forward.APPLY_SCRIPT])

    def test_healthy_needs_the_sysctl_and_a_live_canary(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as fwd:
            conf = _conf(td)
            forward.ruleset_path(conf, "dev", fwd).write_text(
                render_ruleset("t", 2, "192.168.122.9", "virbr0", [(20020, 8000)]))
            canary = ("127.0.0.1", forward.CANARY_BASE + 2)
            for sysctl, up, want in (("0\n", set(), False),                         # rebooted
                                     ("1\n", {canary}, True),                      # loaded
                                     ("1\n", {("192.168.122.9", 22)}, False),      # flushed
                                     ("1\n", set(), None)):                        # VM down
                with mock.patch.object(forward, "_read", side_effect=lambda q: sysctl
                                       if "route_localnet" in str(q) else Path(q).read_text()), \
                     mock.patch.object(forward, "_connects", side_effect=lambda h, port: (h, port) in up), \
                     mock.patch.object(forward.box, "resolve_env_ip", return_value="192.168.122.9"):
                    self.assertIs(forward.healthy(conf, "dev", fwd), want)

    def test_remote_env_has_no_forward(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(REAL_RECONCILE(_conf(td), "prod"), {})


if __name__ == "__main__":
    unittest.main()
