"""The exe resource, with its real probes run against a box laid out in a temp dir."""

import tempfile
import unittest
from unittest import mock

from anci import forward, layout, units
from anci.conf import allocate_ports
from anci.inspect.exe import render_units
from anci.inspect import Action, ObservedState, TargetState, diff
from anci.inspect import exe as exe_mod

from .support import CONF, load_conf
from .support import link, run_probes, write

BLOB = "9f2ce1b07d3a5c44"
JOB = CONF.replace("[exe.app]\nsource = './app'\n", "[exe.app]\nsource = './app'\nschedule = 'daily'\n")


class TestExe(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = td.name
        self.system = f"{td.name}/system"
        for patch in (mock.patch.object(layout, "UNITS_DIR", f"{td.name}/units"),
                      mock.patch.object(layout, "EXE_DIR", f"{td.name}/exe"),
                      mock.patch.object(layout, "SYSTEMD_DIR", self.system),
                      mock.patch.object(forward, "installed_ports", return_value={})):
            patch.start()
            self.addCleanup(patch.stop)

    def _deploy(self, conf, exe_name="app"):
        """Lay out what converge + `systemctl enable` leave for a deployed exe."""
        files = render_units(conf, "dev", exe_name, allocate_ports(conf), {}, None)
        for path, content in files.items():
            write(path, content)
            name = path.rsplit("/", 1)[1]
            link(f"{self.system}/{name}", path)
        if units.timer_name(exe_name) in [p.rsplit("/", 1)[1] for p in files]:
            link(f"{self.system}/timers.target.wants/{units.timer_name(exe_name)}",
                 f"{self.system}/{units.timer_name(exe_name)}")
        else:
            link(f"{self.system}/multi-user.target.wants/{units.service_name(exe_name)}",
                 f"{self.system}/{units.service_name(exe_name)}")
        link(f"{layout.EXE_DIR}/{exe_name}/bin", layout.current_link(exe_name))
        write(f"{self.root}/blobs/{BLOB}", "binary")     # in the store: `current` resolves
        link(layout.current_link(exe_name), f"{self.root}/blobs/{BLOB}")

    def _diff(self, conf):
        r = exe_mod.Exe()
        return diff(ObservedState(r.observed(run_probes(r))), TargetState(r.target(conf, "dev")))

    def test_a_deployed_service_matches(self):
        conf = load_conf(self.root)
        self._deploy(conf)
        r = exe_mod.Exe()
        self.assertEqual(r.observed(run_probes(r))["exe.app"]["blob"], BLOB)
        self.assertEqual(self._diff(conf), [])

    def test_a_deployed_job_matches(self):
        conf = load_conf(self.root, JOB)
        self._deploy(conf)
        self.assertEqual(self._diff(conf), [])

    def test_service_turned_job_shows_the_service_still_starting_at_boot(self):
        self._deploy(load_conf(self.root))
        [change] = self._diff(load_conf(self.root, JOB))
        self.assertEqual(change.action, Action.UPDATE)
        self.assertEqual(change.fields["kind"], ("service", "job"))
        self.assertEqual(change.fields["boot"], (["multi-user.target.wants/app.service"],
                                                 ["timers.target.wants/app.timer"]))
        self.assertIn("timer", change.fields)

    def test_an_exe_gone_from_the_conf_is_a_delete(self):
        conf = load_conf(self.root, CONF + "\n[exe.old]\nsource = './old'\n")
        self._deploy(conf)
        self._deploy(conf, "old")
        # app's unit changes too: it no longer gets ANCI_PORT_OLD
        changes = {c.identifier: c for c in self._diff(load_conf(self.root))}
        self.assertEqual(changes["exe.old"].action, Action.DELETE)
        self.assertEqual(list(changes["exe.app"].fields), ["unit"])

    def test_never_deployed_is_a_create(self):
        [change] = self._diff(load_conf(self.root))
        self.assertEqual((change.identifier, change.action), ("exe.app", Action.CREATE))

    def test_mechanism_units_are_not_exes(self):
        conf = load_conf(self.root)
        self._deploy(conf)
        write(f"{layout.UNITS_DIR}/{units.LITESTREAM_UNIT}", "x")
        write(f"{layout.UNITS_DIR}/{units.LOGTRIM_TIMER}", "x")
        self.assertEqual(self._diff(conf), [])

    def test_static_dropped_from_the_conf_must_be_unlinked(self):
        conf = load_conf(self.root)
        self._deploy(conf)
        link(layout.static_link("app"), layout.static_version("app", "4be07a1c9d3f2e18"))
        [change] = self._diff(conf)
        self.assertEqual(change.fields, {"static": ("4be07a1c9d3f2e18", None)})


if __name__ == "__main__":
    unittest.main()
