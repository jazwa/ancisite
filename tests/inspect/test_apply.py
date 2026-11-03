"""Apply, end to end on a fake box: the real scripts run (tests/inspect/fakebox.py),
and after every run, inspecting the box again finds nothing left to change."""

import contextlib
import io
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci import errors, layout
from anci.inspect import Action, diff, observed_state, target_state
from anci.inspect import run as run_mod
from anci.inspect import vendored as vendored_mod
from anci.inspect.exe import deploy_inputs

from .fakebox import FakeBox
from .support import CONF, load_conf, write_source

JOB = CONF.replace("[exe.app]\nsource = './app'\n", "[exe.app]\nsource = './app'\nschedule = 'daily'\n")
REPLICATED = CONF + "\n[db.main-db.replication]\nbucket = 'backups'\n"
TWO = CONF + "\n[exe.web]\nsource = './web'\n"
# an x86_64 ELF header: what `deploy` (and so `plan`) checks a build can run on the box
ELF = b"\x7fELF" + bytes([2, 1]) + b"\x00" * 12 + (62).to_bytes(2, "little")


class ApplyTest(unittest.TestCase):
    """A fresh fake box and project per test; `converge` runs the real pass."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name) / "project"
        self.dir.mkdir()
        self.box = FakeBox(Path(td.name) / "box")
        for patch in (self.box.connected(),
                      mock.patch.object(vendored_mod, "binaries", return_value={})):
            patch.__enter__()
            self.addCleanup(patch.__exit__, None, None, None)

    def conf(self, text=CONF):
        return load_conf(self.dir, text)

    def converge(self, conf, *, deploy=(), statics=None, **kw) -> str:
        binaries = {n: write_source(self.dir, n, ELF + n.encode()) for n in deploy}
        inputs = deploy_inputs(binaries, statics) if binaries else None
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run_mod.converge(conf, "dev", inputs=inputs, **kw)
        return out.getvalue()

    def remaining(self, conf) -> list:
        """What inspecting the box now still finds to change — but an exe that
        was never deployed, which only `deploy` can create."""
        return [c for c in diff(observed_state(None), target_state(conf, "dev"))
                if not (c.action is Action.CREATE and c.identifier.startswith("exe."))]

    def assertConverged(self, conf):
        self.assertEqual(self.remaining(conf), [])


class TestFreshBox(ApplyTest):
    def test_deploy_converges(self):
        conf = self.conf()
        self.converge(conf, deploy=["app"])
        self.assertConverged(conf)
        self.assertIn("app.service", self.box.active())
        self.assertTrue(self.box.path(layout.current_link("app")).is_symlink())
        self.assertIn("jack", self.box.members())

    def test_a_second_run_changes_nothing(self):
        conf = self.conf()
        self.converge(conf, deploy=["app"])
        self.box.clear_calls()
        out = self.converge(conf)
        self.assertIn("in line", out)
        self.assertFalse([c for c in self.box.calls() if "restart" in c])


class TestExe(ApplyTest):
    def test_new_binary_restarts_only_it_and_records_a_release(self):
        conf = self.conf(TWO)
        self.converge(conf, deploy=["app", "web"])
        self.box.clear_calls()
        write_source(self.dir, "app", ELF + b" v2")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run_mod.converge(conf, "dev", inputs=deploy_inputs({"app": self.dir / "app"}))
        self.assertConverged(conf)
        self.assertEqual([c for c in self.box.calls() if c.startswith("restart")],
                         ["restart app.service"])
        log = self.box.path("/var/lib/anci/releases/app/releases.jsonl").read_text()
        self.assertEqual(len(log.splitlines()), 2)

    def test_the_same_binary_again_changes_nothing(self):
        conf = self.conf()
        self.converge(conf, deploy=["app"])
        self.box.clear_calls()
        self.converge(conf, deploy=["app"])
        self.assertFalse([c for c in self.box.calls() if c.startswith("restart")])

    def test_service_turned_job_stops_starting_at_boot(self):
        self.converge(self.conf(), deploy=["app"])
        conf = self.conf(JOB)
        self.converge(conf)
        self.assertConverged(conf)
        wants = self.box.path("/etc/systemd/system")
        self.assertFalse((wants / "multi-user.target.wants/app.service").exists())
        self.assertTrue((wants / "timers.target.wants/app.timer").is_symlink())
        self.assertNotIn("app.service", self.box.active())
        self.assertIn("app.timer", self.box.active())

    def test_job_turned_service(self):
        self.converge(self.conf(JOB), deploy=["app"])
        conf = self.conf()
        self.converge(conf)
        self.assertConverged(conf)
        self.assertFalse(self.box.path(layout.unit_path("app.timer")).exists())
        self.assertIn("app.service", self.box.active())
        self.assertNotIn("app.timer", self.box.active())

    def test_a_job_with_a_new_binary_is_not_restarted(self):
        conf = self.conf(JOB)
        self.converge(conf, deploy=["app"])
        self.box.clear_calls()
        write_source(self.dir, "app", ELF + b" v2")
        with contextlib.redirect_stdout(io.StringIO()):
            run_mod.converge(conf, "dev", inputs=deploy_inputs({"app": self.dir / "app"}))
        self.assertFalse([c for c in self.box.calls() if "app." in c])

    def test_an_exe_removed_from_the_conf_is_deleted_but_its_log_kept(self):
        self.converge(self.conf(TWO), deploy=["app", "web"])
        conf = self.conf()
        self.converge(conf)
        self.assertConverged(conf)
        self.assertFalse(self.box.path(layout.exe_home("web")).exists())
        self.assertFalse(self.box.path(layout.unit_path("web.service")).exists())
        self.assertFalse(self.box.path("/etc/systemd/system/multi-user.target.wants/web.service").exists())
        self.assertNotIn("web.service", self.box.active())
        self.assertTrue(self.box.path(layout.log_path("web")).exists())

    def test_ports_shift_the_holder_restarts_first(self):
        self.converge(self.conf(TWO), deploy=["app", "web"])   # app 8000, web 8010
        conf = self.conf(TWO + "\n[exe.aaa]\nsource = './aaa'\n")   # aaa 8000, app 8010, web 8020
        self.box.clear_calls()
        self.converge(conf, deploy=["aaa"])
        self.assertConverged(conf)
        order = [c.split()[-1] for c in self.box.calls() if c.startswith("restart")]
        self.assertLess(order.index("web.service"), order.index("app.service"))
        self.assertLess(order.index("app.service"), order.index("aaa.service"))

    def test_never_deployed_is_left_for_deploy(self):
        self.converge(self.conf(TWO), deploy=["app"])
        self.assertFalse(self.box.path(layout.exe_home("web")).exists())


class TestStatic(ApplyTest):
    STATIC = CONF.replace("[exe.app]\nsource = './app'\n", "[exe.app]\nsource = './app'\nstatic = './public'\n")

    def deploy_static(self, conf):
        from anci import staticfiles
        public = self.dir / "public"
        (public / "css").mkdir(parents=True, exist_ok=True)
        (public / "index.html").write_text("<h1>hi</h1>")
        (public / "css" / "a.css").write_text("h1{}")
        return self.converge(conf, deploy=["app"], statics={"app": staticfiles.scan(public)})

    def test_static_files_are_built_and_linked(self):
        conf = self.conf(self.STATIC)
        self.deploy_static(conf)
        link = self.box.path(layout.static_link("app"))
        self.assertEqual((self.box.path(str(link.resolve()).replace(str(self.box.root), "")) / "index.html")
                         .read_text(), "<h1>hi</h1>")

    def test_an_unchanged_redeploy_ships_nothing(self):
        conf = self.conf(self.STATIC)
        self.deploy_static(conf)
        out = self.deploy_static(conf)
        self.assertIn("nothing new", out)

    def served(self, rel: str) -> Path:
        """Where `rel` resolves to through the served static link (raises when it doesn't)."""
        target = self.box.path(layout.static_link("app")).resolve() / rel
        return target.resolve(strict=True)

    def test_a_deleted_version_is_rebuilt_by_the_next_deploy(self):
        conf = self.conf(self.STATIC)
        self.deploy_static(conf)
        shutil.rmtree(self.box.path(layout.static_link("app")).resolve())
        self.assertIn("exe.app", [c.identifier for c in self.remaining_with_static(conf)])
        self.deploy_static(conf)
        self.assertEqual(self.served("index.html").read_text(), "<h1>hi</h1>")

    def test_a_lost_blob_is_shipped_again_and_relinked(self):
        conf = self.conf(self.STATIC)
        self.deploy_static(conf)
        self.served("css/a.css").unlink()
        self.deploy_static(conf)
        self.assertEqual(self.served("css/a.css").read_text(), "h1{}")

    def remaining_with_static(self, conf) -> list:
        """What a deploy of the same static files still finds to change."""
        from anci import staticfiles
        target = target_state(conf, "dev")
        target["exe.app"]["static"] = staticfiles.scan(self.dir / "public").id
        return diff(observed_state(None), target)

    def test_static_dropped_from_the_conf_is_unlinked(self):
        self.deploy_static(self.conf(self.STATIC))
        conf = self.conf()
        self.converge(conf)
        self.assertConverged(conf)
        self.assertFalse(self.box.path(layout.static_link("app")).is_symlink())


class TestStartup(ApplyTest):
    def test_an_app_that_fails_to_start_fails_the_run_with_its_log(self):
        conf = self.conf()
        log = self.box.path(layout.log_path("app"))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("listening on :8000\nbind: address already in use\n")
        self.box.crash("app.service")
        with self.assertRaises(errors.EnvError) as cm:
            self.converge(conf, deploy=["app"])
        self.assertIn("app failed to start", cm.exception.why)
        self.assertIn("  bind: address already in use", cm.exception.why)
        self.assertConverged(conf)   # everything else was applied

    def test_it_is_checked_again_after_the_fix(self):
        conf = self.conf()
        self.box.crash("app.service")
        with self.assertRaises(errors.EnvError):
            self.converge(conf, deploy=["app"])
        self.box.crash()
        fixed = write_source(self.dir, "app", ELF + b" fixed")
        with contextlib.redirect_stdout(io.StringIO()):
            run_mod.converge(conf, "dev", inputs=deploy_inputs({"app": fixed}))
        self.assertIn("app.service", self.box.active())

    def test_a_run_that_restarts_nothing_checks_nothing(self):
        conf = self.conf()
        self.converge(conf, deploy=["app"])
        self.box.clear_calls()
        self.converge(conf)
        self.assertFalse([c for c in self.box.calls() if c.startswith("show")])


class TestUsers(ApplyTest):
    WITH_AMY = CONF.replace("access-users = ['jack']", "access-users = ['jack', 'amy']") + \
        "\n[user.amy]\nssh-pub-keys = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI4ae0b15cd50d7abcd82c81c07b302cb852fb93487a1 amy'\n"

    def test_a_user_added_then_removed(self):
        conf = self.conf(self.WITH_AMY)
        self.converge(conf)
        self.assertConverged(conf)
        self.assertEqual(self.box.members(), {"jack", "amy"})
        self.assertIn("4ae0b15cd50d", self.box.path("/home/amy/.ssh/authorized_keys").read_text())
        conf = self.conf()
        self.converge(conf)
        self.assertConverged(conf)
        self.assertEqual(self.box.members(), {"jack"})
        self.assertFalse(self.box.path("/etc/sudoers.d/anci-amy").exists())
        self.assertFalse(self.box.path("/home/amy/.ssh/authorized_keys").exists())


class TestSecrets(ApplyTest):
    def secrets(self, text):
        (self.dir / ".anci").mkdir(exist_ok=True)
        (self.dir / ".anci" / "secrets.dev.env").write_text(text)

    def test_added_rotated_removed(self):
        conf = self.conf()
        self.converge(conf, deploy=["app"])
        self.secrets("K=1\n")
        self.box.clear_calls()
        self.converge(conf)
        self.assertConverged(conf)
        f = self.box.path(layout.SECRETS_FILE)
        self.assertEqual(f.read_text(), "K=1\n")
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        self.assertIn("restart app.service", self.box.calls())

        self.secrets("K=2\n")
        self.box.clear_calls()
        self.converge(conf)
        self.assertIn("restart app.service", self.box.calls())

        (self.dir / ".anci" / "secrets.dev.env").unlink()
        self.box.clear_calls()
        self.converge(conf)
        self.assertConverged(conf)
        self.assertFalse(f.exists())
        self.assertIn("restart app.service", self.box.calls())


class TestReplication(ApplyTest):
    def setUp(self):
        super().setUp()
        pin = mock.patch("anci.inspect.singletons._pin",
                         return_value=mock.Mock(caddy_url="u", litestream_url="u"))
        pin.start()
        self.addCleanup(pin.stop)

    def test_on_waits_for_the_db_file_then_off(self):
        conf = self.conf(REPLICATED)
        self.converge(conf)
        self.assertConverged(conf)
        self.assertTrue(self.box.path(layout.LITESTREAM_FILE).exists())
        self.assertNotIn("litestream.service", self.box.active())    # no db file yet
        self.assertEqual(self.box.path(layout.replica_record("main-db")).read_text(),
                         " backups dev/main-db\n")

        self.box.path(layout.db_path("main-db")).write_text("")      # migrate created it
        self.converge(conf)
        self.assertIn("litestream.service", self.box.active())

        conf = self.conf()
        self.converge(conf)
        self.assertConverged(conf)
        self.assertNotIn("litestream.service", self.box.active())
        self.assertFalse(self.box.path(layout.LITESTREAM_FILE).exists())
        self.assertFalse(self.box.path(layout.replica_record("main-db")).exists())
        self.assertFalse(self.box.path("/etc/systemd/system/multi-user.target.wants/litestream.service").exists())

    def test_a_moved_replica_resets_its_state_while_stopped(self):
        conf = self.conf(REPLICATED)
        self.converge(conf)
        self.box.path(layout.db_path("main-db")).write_text("")
        state = self.box.path(layout.litestream_state_dir("main-db"))
        state.mkdir()
        self.converge(conf)
        conf = self.conf(REPLICATED + "path = 'elsewhere'\n")
        self.box.clear_calls()
        self.converge(conf)
        self.assertConverged(conf)
        self.assertFalse(state.exists())
        calls = self.box.calls()
        self.assertLess(calls.index("stop litestream.service"), calls.index("start litestream.service"))


class TestInterrupted(ApplyTest):
    def test_a_failed_restart_is_owed_and_done_next_run(self):
        conf = self.conf()
        self.box.fail_systemctl = "restart app.service"
        with self.assertRaises(errors.EnvError) as cm:
            self.converge(conf, deploy=["app"])
        self.assertIn("exe.app/restart", cm.exception.what)
        self.assertTrue(self.box.path(f"{layout.exe_home('app')}/.restart-pending").exists())
        self.box.fail_systemctl = ""
        self.converge(conf)
        self.assertConverged(conf)
        self.assertIn("app.service", self.box.active())


class TestUnknownChange(ApplyTest):
    def test_fails_before_the_box_is_touched(self):
        from anci.inspect import Action, Change
        from anci.inspect.core import ApplyContext
        from anci.inspect.walk import walk
        ctx = ApplyContext(conf=self.conf(), env_name="dev", user="jack",
                           observed=observed_state(None), target=target_state(self.conf(), "dev"))
        with self.assertRaisesRegex(errors.InternalError, "doesn't know how to apply exe.app: mode"):
            walk([Change("exe.app", Action.UPDATE, {"mode": ("a", "b")})], ctx)



class TestVendored(ApplyTest):
    def binaries(self, data: bytes):
        f = self.dir / "sqlite3-bin"
        f.write_bytes(data)
        b = vendored_mod.Binary(layout.SQLITE_BIN, "u", "sqlite3", "sqlite3")
        fetched = mock.patch.object(vendored_mod.Binary, "fetch", return_value=f)
        fetched.start()
        self.addCleanup(fetched.stop)
        vendored_mod.binaries.return_value = {"sqlite3": b}

    def test_shipped_then_replaced(self):
        conf = self.conf()
        self.binaries(b"sqlite v1")
        self.converge(conf)
        self.assertConverged(conf)
        self.assertEqual(self.box.path(layout.SQLITE_BIN).read_bytes(), b"sqlite v1")
        self.binaries(b"sqlite v2")
        self.converge(conf)
        self.assertConverged(conf)
        self.assertEqual(self.box.path(layout.SQLITE_BIN).read_bytes(), b"sqlite v2")

    def test_a_blob_gone_from_the_store_is_shipped_again(self):
        conf = self.conf()
        self.binaries(b"sqlite v1")
        self.converge(conf)
        self.box.path(layout.SQLITE_BIN).resolve().unlink()     # the link dangles now
        self.assertNotEqual(self.remaining(conf), [])
        self.converge(conf)
        self.assertConverged(conf)
        self.assertEqual(self.box.path(layout.SQLITE_BIN).read_bytes(), b"sqlite v1")



class TestRegenApps(ApplyTest):
    """regen quiets every exe on the box, then starts the conf's again."""

    def test_stop_everything_then_start_the_confs(self):
        from anci.inspect import exe as exe_mod
        self.converge(self.conf(TWO + "\n[exe.job]\nsource = './job'\nschedule = 'daily'\n"),
                      deploy=["app", "web", "job"])
        conf = self.conf(CONF + "\n[exe.job]\nsource = './job'\nschedule = 'daily'\n")   # web removed
        on_box = observed_state(None)
        run_mod.run_steps(None, exe_mod.stop_steps(on_box))
        self.assertFalse({"app.service", "web.service", "job.timer"} & self.box.active())
        run_mod.run_steps(None, exe_mod.start_steps(conf, "dev", on_box))
        self.assertTrue({"app.service", "job.timer"} <= self.box.active())
        self.assertNotIn("web.service", self.box.active())    # removed from the conf: stays down
        self.assertNotIn("job.service", self.box.active())    # a oneshot isn't run by hand



class TestPlan(ApplyTest):
    """`plan` looks at the box the way a run does, with the builds `deploy` would ship."""

    def plan(self, conf, vm=None) -> list[str]:
        from anci.commands import plan as plan_mod
        out = io.StringIO()
        with mock.patch("anci.providers.libvirt.vm_differences", return_value=vm or {}), \
             contextlib.redirect_stdout(out):
            plan_mod.plan(conf, "dev")
        return [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in out.getvalue().splitlines()]

    def test_a_box_in_line_with_the_conf_and_the_build(self):
        conf = self.conf()
        self.converge(conf, deploy=["app"])
        self.assertEqual(self.plan(conf), ["✓ no changes — the box matches anci.toml"])

    def test_a_new_build_is_deploys_to_ship(self):
        conf = self.conf()
        self.converge(conf, deploy=["app"])
        write_source(self.dir, "app", ELF + b" v2")
        self.assertEqual(self.plan(conf), ["~ exe.app  new build  (anci dev deploy)"])

    def test_a_conf_change_is_reconciles(self):
        conf = self.conf()
        self.converge(conf, deploy=["app"])
        conf = self.conf(TestUsers.WITH_AMY)
        self.assertEqual(self.plan(conf), ["+ user.amy"])

    def test_an_exe_not_built_yet_is_just_listed(self):
        self.converge(self.conf())
        self.assertEqual(self.plan(self.conf()), ["+ exe.app"])

    def test_a_db_taken_out_of_the_conf_isnt_listed(self):
        # its data stays on the box on purpose; it's not a change to make
        self.converge(self.conf(CONF + "\n[db.old]\nmigrations = './m'\n"), deploy=["app"])
        conf = self.conf()
        self.assertEqual(self.plan(conf), ["~ exe.app  unit"])     # it loses ANCI_DB_OLD_PATH
        self.assertTrue(self.box.path(layout.db_dir("old")).is_dir())

    def test_the_vm_size_is_its_own_line(self):
        conf = self.conf()
        self.converge(conf, deploy=["app"])
        self.assertEqual(self.plan(conf, vm={"vcpus": ("2", "4")}), ["~ vm  vcpus: 2 → 4"])


if __name__ == "__main__":
    unittest.main()
