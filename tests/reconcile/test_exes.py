"""`exes` — per-exe units, links, blob, log, activation."""

import tempfile
import unittest

from anci.render import systemd
from anci import box
from anci import apply as apply_mod
from anci import layout, render, ssh
from anci.commands import dbs as dbs_mod
from anci.apply import ApplyResult
from anci.conf import allocate_ports
from anci.converge import exes

from .support import CONF, ELF, load_conf, make_ctx, write_source
from .support import file_at, link_at

SCHED = "\n[exe.job]\nsource = './job'\nschedule = 'daily'\n"


class TestServiceExe(unittest.TestCase):
    def _contribute(self, td):
        conf = load_conf(td)
        src = write_source(td, "app")
        return exes.contribute(make_ctx(conf, binaries={"app": src})), conf

    def test_unit_matches_the_deploy_renderer(self):
        with tempfile.TemporaryDirectory() as td:
            c, conf = self._contribute(td)
            resolved = conf.exes["app"].resolve_for("dev")
            expected = systemd.render_unit(
                exe_name="app", env_name="dev", sysuser=layout.ANCI_USER,
                target=resolved.target, args=resolved.args,
                port=allocate_ports(conf)[("app", "dev")],
                env_vars={**resolved.env_vars, **box.db_env_vars(conf, "dev"),
                          **box.port_env_vars("dev", allocate_ports(conf))},
                rw_paths=[layout.db_dir("main-db")],
                env_file=None, bind="0.0.0.0", url=box.app_url(conf, "app", "dev"),
                log_file=layout.log_path("app"),
            )
            self.assertEqual(
                file_at(c, f"{layout.UNITS_DIR}/app.service").content, expected)

    def test_env_vars_override_anci_vars(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, CONF.replace(
                "source = './app'\n",
                "source = './app'\nenv-vars = { ANCI_DB_MAIN_DB_PATH = '/x', ANCI_PORT_APP = '1', "
                "ANCI_URL = 'https://example.com' }\n", 1))
            c = exes.contribute(make_ctx(conf, binaries={"app": write_source(td, "app")}))
        unit = file_at(c, f"{layout.UNITS_DIR}/app.service").content
        for line in ("ANCI_DB_MAIN_DB_PATH=/x", "ANCI_PORT_APP=1", "ANCI_URL=https://example.com"):
            self.assertIn(f"Environment={line}\n", unit)

    def test_url_is_the_localhost_forward(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            c = exes.contribute(make_ctx(conf, binaries={"app": src}, local_ports={"app": 20003}))
            unit = file_at(c, f"{layout.UNITS_DIR}/app.service").content
            self.assertIn(f"Environment=ANCI_URL=http://{box.localhost_name(conf)}:20003", unit)
            self.assertIn("Environment=ANCI_BIND=0.0.0.0", unit)   # the forward reaches the VM's address

    def test_links_form_target_to_current_to_blob(self):
        with tempfile.TemporaryDirectory() as td:
            c, conf = self._contribute(td)
            target = conf.exes["app"].resolve_for("dev").target
            cur = f"{layout.EXE_DIR}/app/current"
            self.assertEqual(link_at(c, target).target, cur)
            self.assertEqual(link_at(c, cur).target, c.blobs[0].dest)
            self.assertEqual(
                link_at(c, "/etc/systemd/system/app.service").target,
                f"{layout.UNITS_DIR}/app.service")

    def test_blob_is_the_source_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            src = write_source(td, "app")
            c = exes.contribute(make_ctx(conf, binaries={"app": src}))
            self.assertEqual(c.blobs[0].exe_name, "app")
            self.assertEqual(c.blobs[0].dest, layout.blob_path(apply_mod.sha256_file(src)))

    def test_prepare_creates_dirs_and_log(self):
        with tempfile.TemporaryDirectory() as td:
            c, _ = self._contribute(td)
            blob = "\n".join(c.prepare)
            self.assertIn(f"{layout.EXE_DIR}/app", blob)
            self.assertIn(f"touch {layout.log_path('app')}", blob)

    def test_one_service_activation(self):
        with tempfile.TemporaryDirectory() as td:
            c, _ = self._contribute(td)
            self.assertEqual([a.phase for a in c.after_apply], [1])

    def test_no_source_and_not_deployed_is_skipped(self):
        # no bytes and never deployed → nothing to reconcile
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            c = exes.contribute(make_ctx(conf, binaries={}, exe_dirs=[]))
            self.assertEqual(c.files, [])
            self.assertEqual(c.blobs, [])
            self.assertEqual(c.links, [])

    def test_no_source_but_deployed_keeps_its_version(self):
        # already running: reconcile the unit, leave the deployed version alone
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td)
            c = exes.contribute(make_ctx(conf, binaries={}, exe_dirs=["app"]))
            self.assertEqual(c.blobs, [])
            self.assertTrue(any(f.path == f"{layout.UNITS_DIR}/app.service"
                                for f in c.files))
            self.assertFalse(any(l.path == f"{layout.EXE_DIR}/app/current"
                                 for l in c.links))

    def test_per_env_source_override_selects_the_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            conf = load_conf(td, CONF + "\n[exe.app.env.dev]\nsource = './app-dev'\n")
            src = write_source(td, "app-dev", data=ELF + b"dev")
            c = exes.contribute(make_ctx(conf, binaries={"app": src}))
            self.assertEqual(c.blobs[0].dest, layout.blob_path(apply_mod.sha256_file(src)))


class TestScheduledExe(unittest.TestCase):
    def _contribute(self, td):
        conf = load_conf(td, CONF + SCHED)
        binaries = {"app": write_source(td, "app"), "job": write_source(td, "job")}
        return exes.contribute(make_ctx(conf, binaries=binaries)), conf

    def test_oneshot_and_timer_match_the_renderers(self):
        with tempfile.TemporaryDirectory() as td:
            c, conf = self._contribute(td)
            resolved = conf.exes["job"].resolve_for("dev")
            oneshot = systemd.render_oneshot(
                exe_name="job", env_name="dev", sysuser=layout.ANCI_USER,
                target=resolved.target, args=resolved.args,
                env_vars={**resolved.env_vars, **box.db_env_vars(conf, "dev"),
                          **box.port_env_vars("dev", allocate_ports(conf))},
                rw_paths=[layout.db_dir("main-db")],
                env_file=None, log_file=layout.log_path("job"),
            )
            self.assertEqual(
                file_at(c, f"{layout.UNITS_DIR}/job.service").content, oneshot)
            self.assertEqual(
                file_at(c, f"{layout.UNITS_DIR}/job.timer").content,
                systemd.render_timer(exe_name="job", env_name="dev", schedule="daily"))

    def test_timer_link_is_present(self):
        with tempfile.TemporaryDirectory() as td:
            c, _ = self._contribute(td)
            self.assertEqual(
                link_at(c, "/etc/systemd/system/job.timer").target,
                f"{layout.UNITS_DIR}/job.timer")

    def test_a_job_has_no_port_and_a_oneshot_unit(self):
        with tempfile.TemporaryDirectory() as td:
            c, _ = self._contribute(td)
            unit = file_at(c, f"{layout.UNITS_DIR}/job.service").content
            self.assertIn("Type=oneshot", unit)
            self.assertNotIn("ANCI_PORT=", unit)
            self.assertIn("Environment=ANCI_PORT_APP=8000", unit)   # it can still reach the services
