"""Scheduled jobs (`exe.schedule` → Type=oneshot + timer).

Covers the whole feature end-to-end without a VM: conf parsing + per-env
override merge, the deploy branch (oneshot + timer artifacts, timer enabled,
no port probe/restart), `app_url` refusing a job, `status` reporting a job, and
`db_regen` quiescing a job's timer.
"""

import contextlib
import hashlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anci.render import caddy
from anci import auth
from anci import box
from anci.providers import adopt as adopt_mod
from anci import apply as apply_mod
from anci import secrets as secrets_mod
from anci import errors, forward, releases, ssh
from anci.commands import dbs, deploy, status
from anci.converge import vendored
from anci.converge.context import DesiredState
from anci.apply import ApplyResult
from anci.conf import (
    Conf, Db, Env, Exe, ExeOverride, User, allocate_ports, load,
    Replication,
)


# these tests drive whole verbs over a mocked transport; the box lock is a real
# ssh session, so it's a no-op here (tests/test_boxlock.py covers it)
_lock_patch = mock.patch("anci.boxlock.hold", lambda *a, **kw: contextlib.nullcontext())


def setUpModule():
    _lock_patch.start()


def tearDownModule():
    _lock_patch.stop()


SCHEDULE_CONF = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.cleanup-job]
source = './cleanup'
schedule = 'daily'

[exe.cleanup-job.env.dev]
"""


CONF_BASE = """
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = { anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }

[exe.cleanup]
source = './bin/cleanup'
schedule = 'daily'

[exe.cleanup.env.dev]

[exe.job]
source = './bin/job'

[exe.job.env.dev]
schedule = 'Mon 03:00'

[exe.replace]
source = './bin/replace'
schedule = 'daily'

[exe.replace.env.dev]
schedule = 'hourly'
"""


def load_conf(directory: str, text: str) -> Conf:
    p = Path(directory) / "anci.toml"
    p.write_text(text)
    return load(str(p))


def _tiny(schedule_line: str) -> str:
    return f"""
[user.jack]
ssh-pub-keys = 'ssh-ed25519 AAAA jack'

[env.dev]
ip = '127.0.0.1'
access-users = ['jack']
vm = {{ anci-version = '0.0.1', vcpus = 2, mem = '1gb', disk = '20gb' }}

[exe.x]
source = './x'
{schedule_line}

[exe.x.env.dev]
"""


class TestScheduleConf(unittest.TestCase):
    def test_base_schedule_parsed_and_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, CONF_BASE)
            self.assertEqual(conf.exes["cleanup"].schedule, "daily")
            self.assertEqual(conf.exes["cleanup"].resolve_for("dev").schedule, "daily")

    def test_override_adds_schedule(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, CONF_BASE)
            self.assertIsNone(conf.exes["job"].schedule)
            self.assertEqual(conf.exes["job"].resolve_for("dev").schedule, "Mon 03:00")

    def test_override_replaces_schedule(self):
        # scalars replace: base 'daily' + override 'hourly' → 'hourly'
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, CONF_BASE)
            self.assertEqual(conf.exes["replace"].schedule, "daily")
            self.assertEqual(conf.exes["replace"].resolve_for("dev").schedule, "hourly")

    def test_empty_schedule_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(errors.ConfError):
                load_conf(td, _tiny("schedule = ''"))

    def test_non_string_schedule_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(errors.ConfError):
                load_conf(td, _tiny("schedule = 5"))


class TestAppUrlJob(unittest.TestCase):
    def test_job_has_no_url(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, SCHEDULE_CONF)
            with self.assertRaises(errors.ConfError) as cm:
                box.app_url(conf, "cleanup-job", "dev")
            self.assertIn("no URL", str(cm.exception))


class TestDeployScheduled(unittest.TestCase):
    def _run(self, td: str, *, apply_result: ApplyResult) -> dict:
        conf = load_conf(td, SCHEDULE_CONF)
        # a minimal x86_64 ELF header (the deploy guard refuses scripts)
        app = b"\x7fELF" + bytes([2, 1]) + b"\x00" * 12 + (62).to_bytes(2, "little")
        (Path(td) / "cleanup").write_bytes(app)
        sha = hashlib.sha256(app).hexdigest()
        captured: dict = {"files": {}, "links": [], "scripts": []}

        def _apply(conn, files, links, probe_result=None, deletes=None):
            for f in files:
                captured["files"].setdefault(f.path, f.content)
            if links:  # the main apply
                captured["links"] = [l.path for l in links]
            return apply_result

        def _script(conn, script_text, *, timeout=120):
            captured["scripts"].append(script_text)
            if "sha256sum" in script_text:
                return ssh.Result(f"{sha}  /tmp/x\n", "")
            return ssh.Result("", "")

        with mock.patch.object(vendored, "contribute",
                               return_value=DesiredState(name="vendored")), \
             mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "wait_ready"), \
             mock.patch.object(secrets_mod, "resolve_secrets", return_value=(None, "")), \
             mock.patch.object(forward, "reconcile", return_value={}), \
             mock.patch.object(apply_mod, "probe", return_value=({}, {}, {})), \
             mock.patch.object(apply_mod, "apply", side_effect=_apply), \
             mock.patch.object(ssh, "sudo",
                               side_effect=lambda c, t, **k: captured["scripts"].append(t) or ssh.Result("", "")), \
             mock.patch.object(ssh, "script", side_effect=_script), \
             mock.patch.object(ssh, "put"), \
             mock.patch.object(releases, "append"), \
             contextlib.redirect_stdout(io.StringIO()):
            deploy.deploy(conf, "cleanup-job", "dev")
        return captured

    def test_renders_oneshot_and_timer(self):
        with tempfile.TemporaryDirectory() as td:
            captured = self._run(
                td,
                apply_result=ApplyResult(
                    changed_files=[
                        "/etc/anci/units/cleanup-job.service",
                        "/etc/anci/units/cleanup-job.timer",
                    ],
                    changed_links=[
                        "/etc/systemd/system/cleanup-job.service",
                        "/etc/systemd/system/cleanup-job.timer",
                    ],
                ),
            )
        files = captured["files"]
        unit = files["/etc/anci/units/cleanup-job.service"]
        timer = files["/etc/anci/units/cleanup-job.timer"]
        self.assertIn("Type=oneshot", unit)
        self.assertIn("ANCI_ENV=dev", unit)
        self.assertNotIn("ANCI_PORT", unit)     # a job serves no HTTP
        self.assertNotIn("Restart=", unit)      # oneshot runs once
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("/etc/systemd/system/cleanup-job.service", captured["links"])
        self.assertIn("/etc/systemd/system/cleanup-job.timer", captured["links"])
        scripts = "\n".join(captured["scripts"])
        self.assertIn("systemctl enable --now cleanup-job.timer", scripts)
        self.assertIn("systemctl is-active --quiet cleanup-job.timer", scripts)
        self.assertNotIn("systemctl restart", scripts)   # never restart a oneshot
        self.assertNotIn("/dev/tcp/", scripts)           # never probe a port

    def test_new_binary_only_does_not_touch_timer(self):
        # the unit + timer are unchanged; a new blob must not re-enable the timer
        with tempfile.TemporaryDirectory() as td:
            captured = self._run(
                td,
                apply_result=ApplyResult(
                    changed_links=["/var/lib/anci/store/exes/cleanup-job/current"],
                ),
            )
        scripts = "\n".join(captured["scripts"])
        self.assertNotIn("systemctl enable --now", scripts)
        self.assertNotIn("systemctl restart", scripts)
        # ...but the timer is still verified armed
        self.assertIn("systemctl is-active --quiet cleanup-job.timer", scripts)


class TestStatusScheduled(unittest.TestCase):
    def test_reads_timer_facts(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, SCHEDULE_CONF)
            found = {"cleanup-job current": ["/var/lib/anci/store/blobs/aaaa"],
                     "cleanup-job timer": ["active"],
                     "cleanup-job next": ["Mon 2026-10-05 03:00:00 UTC"]}
            st = status._read(found, conf, ["cleanup-job"], "dev").state["cleanup-job"]
        self.assertTrue(st.timer)
        self.assertEqual(st.current, "aaaa")
        self.assertEqual(st.next, "2026-10-05T03:00:00Z")

    def test_status_shows_scheduled_and_dash_url(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, SCHEDULE_CONF)
            found = {"cleanup-job current": ["/var/lib/anci/store/blobs/aaaa"],
                     "cleanup-job timer": ["active"],
                     "cleanup-job next": ["Mon 2026-10-05 03:00:00 UTC"]}
            buf = io.StringIO()
            with mock.patch.object(auth, "identify_user", return_value="jack"), \
                 mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
                 mock.patch.object(ssh, "facts", return_value=found), \
                 contextlib.redirect_stdout(buf):
                status.status(conf, "dev")
            text = buf.getvalue()
        self.assertIn("scheduled", text)
        self.assertIn("next 2026-10-05T03:00:00Z", text)
        self.assertNotIn("ran ", text)       # the last-run time is not shown
        self.assertIn("cleanup-job", text)   # the exe column
        self.assertNotIn("http://", text)    # a job has no URL


class TestStatusLastRun(unittest.TestCase):
    def _row(self, run):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, SCHEDULE_CONF)
            found = {"cleanup-job current": ["/var/lib/anci/store/blobs/aaaa"],
                     "cleanup-job timer": ["active"],
                     "cleanup-job next": ["Mon 2026-10-05 03:00:00 UTC"]}
            if run:
                found["cleanup-job run"] = [run]
            buf = io.StringIO()
            with mock.patch.object(auth, "identify_user", return_value="jack"), \
                 mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
                 mock.patch.object(ssh, "facts", return_value=found), \
                 contextlib.redirect_stdout(buf):
                status.status(conf, "dev")
        row = next(line for line in buf.getvalue().splitlines() if "cleanup-job" in line)
        return row

    def test_last_run_replaces_scheduled(self):
        for run in ("failed", "succeeded", "running"):
            row = self._row(run)
            self.assertIn(run, row)
            self.assertNotIn("scheduled", row)
            self.assertIn("next 2026-10-05T03:00:00Z", row)

    def test_never_run_is_scheduled(self):
        self.assertIn("scheduled", self._row(None))


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestLastRunProbe(unittest.TestCase):
    """The probe against a fake systemctl answering like a real one."""

    def _probe(self, active, result, exit_ts):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, SCHEDULE_CONF)
            script = status._probes(conf, ["cleanup-job"], "dev", {})["cleanup-job run"]
            fake = Path(td) / "systemctl"
            fake.write_text("#!/bin/sh\ncase \"$4\" in\n"
                            f"  ActiveState) echo {active};;\n"
                            f"  Result) echo {result};;\n"
                            f"  ExecMainExitTimestampMonotonic) echo {exit_ts};;\n"
                            "esac\n")
            fake.chmod(0o755)
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                  env={"PATH": f"{td}:/usr/bin:/bin"}).stdout.strip()

    def test_states(self):
        self.assertEqual(self._probe("failed", "exit-code", "1605597019"), "failed")
        self.assertEqual(self._probe("inactive", "success", "2434086340"), "succeeded")
        self.assertEqual(self._probe("activating", "success", "0"), "running")
        self.assertEqual(self._probe("inactive", "success", "0"), "")   # never ran


class TestScheduledStatusScript(unittest.TestCase):
    def test_next_falls_back_to_systemd_analyze(self):
        # NextElapseUSecRealtime is empty while the job runs; the script must
        # fall back to computing the next elapse from the schedule
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, SCHEDULE_CONF)
            script = status._probes(conf, ["cleanup-job"], "dev", {})["cleanup-job next"]
        self.assertIn("NextElapseUSecRealtime", script)
        self.assertIn("systemd-analyze calendar", script)
        self.assertIn("daily", script)  # the schedule is embedded


class TestIsoUtc(unittest.TestCase):
    def test_formats_systemd_timestamp(self):
        self.assertEqual(status._iso_utc("Wed 2026-10-07 20:34:49 UTC"),
                         "2026-10-07T20:34:49Z")

    def test_empty_and_na(self):
        self.assertIsNone(status._iso_utc(""))
        self.assertIsNone(status._iso_utc("n/a"))

    def test_unrecognized_left_alone(self):
        self.assertEqual(status._iso_utc("whenever"), "whenever")


class TestDbRegenTimers(unittest.TestCase):
    def _conf(self, tmp: Path) -> Conf:
        mig = tmp / "migrations"
        mig.mkdir(exist_ok=True)
        (mig / "0001.sql").write_text("CREATE TABLE t (id INTEGER PRIMARY KEY);")
        return Conf(
            users={"jack": User(name="jack", ssh_pub_keys=["k"])},
            envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                             anci_version="0.0.1")},
            exes={
                "app": Exe(name="app", source="./bin/app",
                           env_overrides={"dev": ExeOverride(env="dev")}),
                "cleanup": Exe(name="cleanup", source="./bin/cleanup",
                               schedule="daily",
                               env_overrides={"dev": ExeOverride(env="dev")}),
            },
            dbs={"main-db": Db(
                name="main-db", migrations=str(mig),
                replication=Replication(bucket="backups"),
            )},
        )

    def test_stops_and_rearms_timers_but_not_oneshot(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._conf(Path(td))
            # the box also runs a unit the conf no longer declares (a removed exe)
            inventory = (
                "app.service\n"
                "cleanup.service\n"
                "cleanup.timer\n"
                "ghost.service\n"
            )
            stops, starts = [], []

            def _script(conn, s, **k):
                if "/etc/anci/units" in s:   # the lifecycle facts probe
                    return ssh.Result("".join(f"unit\t{u}\n" for u in inventory.split()), "")
                return ssh.Result("", "")

            def _sudo(conn, s, **k):
                if s.startswith("systemctl stop"):
                    stops.append(s)
                elif s.startswith("systemctl start"):
                    starts.append(s)
                return ssh.Result("", "")

            with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
                 mock.patch.object(dbs.ssh, "connect", return_value=mock.Mock()), \
                 mock.patch.object(dbs.ssh, "script", side_effect=_script), \
                 mock.patch.object(dbs.ssh, "sudo", side_effect=_sudo), \
                 contextlib.redirect_stdout(io.StringIO()):
                dbs.db_regen(conf, "main-db", "dev", force=True)

        # stop: EVERY anci-owned app unit on the box, the ghost included
        stop = stops[0]
        for u in ("app.service", "cleanup.service",
                  "cleanup.timer", "ghost.service"):
            self.assertIn(u, stop)
        # start: re-arm the conf's timer + service, never a ghost
        start = starts[0]
        self.assertIn("app.service", start)
        self.assertIn("cleanup.timer", start)
        self.assertNotIn("cleanup.service", start)   # a oneshot isn't run by hand
        self.assertNotIn("ghost.service", start)     # an orphan is dead


class TestJobsGetNoPort(unittest.TestCase):
    def _conf(self) -> Conf:
        return Conf(
            users={},
            envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=[],
                             anci_version="0.0.1")},
            exes={
                "app": Exe(name="app", source="./s",
                           env_overrides={"dev": ExeOverride(env="dev")}),
                "cleanup": Exe(name="cleanup", source="./s",
                               schedule="daily",
                               env_overrides={"dev": ExeOverride(env="dev")}),
                "web": Exe(name="web", source="./s",
                           env_overrides={"dev": ExeOverride(env="dev")}),
            },
        )

    def test_ports_skip_jobs_without_shifting_services(self):
        ports = allocate_ports(self._conf())
        self.assertEqual(ports[("app", "dev")], 8000)
        self.assertEqual(ports[("web", "dev")], 8010)  # the job consumed no slot
        self.assertNotIn(("cleanup", "dev"), ports)


class TestCaddySkipsJobs(unittest.TestCase):
    def test_job_is_not_routed(self):
        conf = Conf(
            users={},
            envs={"dev": Env(name="dev", ip="10.0.0.5",
                             access_users=[], anci_version="0.0.1")},
            exes={
                "app": Exe(name="app", source="./s",
                           env_overrides={"dev": ExeOverride(env="dev", host="dev.example.com")}),
                "cleanup": Exe(name="cleanup", source="./s",
                               schedule="daily",
                               env_overrides={"dev": ExeOverride(env="dev")}),
            },
        )
        caddyfile = caddy.caddyfile_for(conf, "dev", allocate_ports(conf))
        self.assertIn("dev.example.com {", caddyfile)
        self.assertIn("reverse_proxy 127.0.0.1:8000", caddyfile)  # the serving exe
        self.assertEqual(caddyfile.count("reverse_proxy"), 1)     # the job is not routed


if __name__ == "__main__":
    unittest.main()
