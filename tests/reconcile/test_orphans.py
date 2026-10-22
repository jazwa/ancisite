"""`orphans` — units/configs/exe-dirs the conf no longer declares — and the
`lifecycle` stop/start helpers `regen` uses."""

import tempfile
import unittest
from unittest import mock

from anci import layout, ssh
from anci.conf import Conf, Db, Env, Exe, ExeOverride, Replication, User
from anci.converge import lifecycle, orphans

from .support import load_conf, make_ctx


def make_conf(*, host: str | None = None, ip: str = "10.0.0.5") -> Conf:
    return Conf(
        users={"jack": User(name="jack", ssh_pub_keys=["k"])},
        envs={"dev": Env(name="dev", ip=ip, access_users=["jack"],
                         anci_version="0.0.1")},
        exes={
            "app": Exe(name="app", source="./s",
                       env_overrides={"dev": ExeOverride(env="dev", host=host)}),
            "job": Exe(name="job", source="./s", schedule="daily",
                       env_overrides={"dev": ExeOverride(env="dev")}),
        },
    )


def _contribute(conf, units, dirs=(), existing=()):
    ctx = make_ctx(conf, present_units=set(units), exe_dirs=list(dirs))
    ctx.conn = ssh.Conn(ip="x")
    with mock.patch.object(orphans, "_existing", return_value=list(existing)):
        return orphans.contribute(ctx)


class TestDesiredSet(unittest.TestCase):
    def test_app_units_plus_litestream(self):
        conf = make_conf()
        conf.dbs = {"db": Db(name="db", migrations="./m/",
                             replication=Replication(bucket="b", path="db"))}
        desired = orphans.desired_unit_names(conf, "dev")
        self.assertTrue({"app.service", "job.timer", "job.service",
                         "litestream.service"} <= desired)
        self.assertNotIn("caddy.service", desired)        # remote, no host

    def test_no_litestream_without_replication(self):
        self.assertNotIn("litestream.service", orphans.desired_unit_names(make_conf(), "dev"))

    def test_caddy_only_with_host(self):
        self.assertIn("caddy.service",
                      orphans.desired_unit_names(make_conf(host="dev.example.com"), "dev"))
        self.assertNotIn("caddy.service",
                         orphans.desired_unit_names(make_conf(ip="127.0.0.1"), "dev"))

    def test_every_exe_is_desired_here_even_if_declared_elsewhere(self):
        # subtables are overrides only: an exe named on another env is desired here too
        conf = make_conf()
        conf.exes["other"] = Exe(name="other", source="./s",
                                 env_overrides={"prod": ExeOverride(env="prod")})
        self.assertIn("other.service", orphans.desired_unit_names(conf, "dev"))


class TestOrphans(unittest.TestCase):
    def test_no_inventory_contributes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            c = orphans.contribute(make_ctx(load_conf(td)))   # inventories not gathered
        self.assertEqual((c.deletes, c.stops), ([], []))

    def test_clean_box(self):
        c = _contribute(make_conf(), {"app.service", "job.service", "job.timer"}, ["app", "job"])
        self.assertEqual((c.stops, c.deletes, c.report), ([], [], []))

    def test_removed_exe_is_stopped_disabled_and_deleted(self):
        c = _contribute(make_conf(),
                        {"app.service", "job.timer", "ghost.service", "ghost.timer"},
                        ["app", "job", "ghost"],
                        existing=["/var/lib/anci/releases/ghost", "/var/log/anci/ghost.log"])
        self.assertEqual(c.stops, ["ghost.service", "ghost.timer"])
        self.assertEqual(c.disables, ["ghost.timer"])      # the timer is enableable
        paths = [d.path for d in c.deletes]
        self.assertIn("/etc/systemd/system/ghost.service", paths)
        self.assertIn(f"{layout.UNITS_DIR}/ghost.timer", paths)
        self.assertEqual([d.tree for d in c.deletes if d.path == f"{layout.EXE_DIR}/ghost"], [True])
        self.assertNotIn(f"{layout.EXE_DIR}/app", paths)
        # data/history are reported, never rendered as deletions
        self.assertIn("/var/log/anci/ghost.log", c.report)
        self.assertFalse(any(p.startswith("/var/log/anci/") for p in paths))

    def test_mechanism_side_files_go_with_their_unit(self):
        c = _contribute(make_conf(), {"app.service", "job.timer", "litestream.service",
                                      "caddy.service"}, ["app", "job"])
        paths = [d.path for d in c.deletes]
        self.assertIn(layout.LITESTREAM_FILE, paths)
        self.assertIn(layout.CADDY_FILE, paths)
        self.assertIn(f"{layout.UNITS_DIR}/caddy.service", paths)

    def test_existing_probes_each_path(self):
        found = {"/a": ["yes"]}
        with mock.patch.object(ssh, "facts", return_value=found) as f:
            self.assertEqual(orphans._existing(ssh.Conn(ip="x"), ["/a", "/b"]), ["/a"])
        self.assertIn("[ -e /b ]", f.call_args.args[1]["/b"])


class TestLifecycle(unittest.TestCase):
    def _run(self, fn, units, *args):
        calls = []
        with mock.patch.object(ssh, "facts", return_value={"unit": units}), \
             mock.patch.object(ssh, "sudo", side_effect=lambda c, s, **k: calls.append(s)):
            fn(ssh.Conn(ip="1.2.3.4"), *args)
        return calls

    def test_odd_unit_names_never_reach_systemctl(self):
        calls = self._run(lifecycle.stop_app_units,
                          ["app.service", "evil;reboot.service", "$(id).timer", "a b.service"])
        self.assertEqual(calls, ["systemctl stop app.service"])

    def test_stop_quotes_what_it_passes(self):
        # belt and braces: even a name that slipped the filter would be one quoted word
        with mock.patch.object(lifecycle, "_present_units", return_value={"a.service"}), \
             mock.patch.object(ssh, "sudo") as sudo:
            lifecycle.stop_app_units(ssh.Conn(ip="x"))
        self.assertEqual(sudo.call_args.args[1], "systemctl stop a.service")

    def test_stop_is_box_driven_and_spares_mechanism_units(self):
        calls = self._run(lifecycle.stop_app_units,
                          ["app.service", "ghost.service", "litestream.service", "caddy.service"])
        self.assertEqual(calls, ["systemctl stop app.service ghost.service"])

    def test_start_is_conf_driven_and_only_what_exists(self):
        # job's timer isn't on the box yet, and a job's oneshot is never started
        calls = self._run(lifecycle.start_conf_units, ["app.service", "job.service"],
                          make_conf(), "dev")
        self.assertEqual(calls, ["systemctl start app.service"])


if __name__ == "__main__":
    unittest.main()
