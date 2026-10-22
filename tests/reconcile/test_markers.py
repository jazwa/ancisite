"""Restart markers: a restart owed by an interrupted run is never lost, and the
release log records a move even when the restart after it fails."""

import contextlib
import io
import tempfile
import unittest
from unittest import mock

from anci import auth, layout, releases, ssh
from anci import apply as apply_mod
from anci import secrets as secrets_mod
from anci.apply import ApplyResult
from anci.converge import caddy, exes, litestream, vendored
from anci.converge import run as run_mod
from anci.converge.context import DesiredState

from .support import load_conf, write_source

MARKER = f"{layout.EXE_DIR}/app/.restart-pending"
RESTART = "systemctl restart app.service"


class TestConvergeMarkers(unittest.TestCase):
    def _run(self, td, *, probe=None, apply_result=None, fail_restart=False):
        conf = load_conf(td)
        src = write_source(td)
        calls = []   # ("sudo", text) and ("apply", None), in order

        def sudo(conn, text, **kw):
            calls.append(("sudo", text))
            if fail_restart and RESTART in text:
                raise ssh.SSHError("boom")
            return ssh.Result("", "")

        def script(conn, text, **kw):
            if "sha256sum" in text:
                return ssh.Result(f"{apply_mod.sha256_file(src)}  /tmp/x\n", "")
            return ssh.Result("CI status: done\n", "")

        def apply(*a, **kw):
            calls.append(("apply", None))
            return apply_result or ApplyResult()

        with mock.patch.object(vendored, "contribute",
                               return_value=DesiredState(name="vendored")), \
             mock.patch.object(auth, "identify_user", return_value="jack"), \
             mock.patch.object(ssh, "connect", return_value=ssh.Conn(ip="127.0.0.1")), \
             mock.patch.object(ssh, "script", side_effect=script), \
             mock.patch.object(ssh, "sudo", side_effect=sudo), \
             mock.patch.object(ssh, "put"), \
             mock.patch.object(secrets_mod, "resolve_secrets", return_value=(None, "")), \
             mock.patch.object(run_mod.forward, "reconcile", return_value={}), \
             mock.patch.object(apply_mod, "probe", return_value=probe or ({}, {}, {})), \
             mock.patch.object(apply_mod, "apply", side_effect=apply), \
             mock.patch.object(releases, "append") as append, \
             mock.patch.object(run_mod, "_gc"), \
             mock.patch.object(run_mod.boxlock, "hold",
                               new=lambda *a, **kw: contextlib.nullcontext()), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                run_mod.converge(conf, "dev", binaries={"app": src})
            except ssh.SSHError:
                if not fail_restart:
                    raise
        return calls, append, out.getvalue()

    def test_marker_is_written_before_apply(self):
        # an empty probe: the unit file differs, so app owes a restart
        with tempfile.TemporaryDirectory() as td:
            calls, _, _ = self._run(td)
        touch = next(i for i, (k, t) in enumerate(calls) if k == "sudo" and t.startswith("touch")
                     and MARKER in t)
        apply_at = calls.index(("apply", None))
        self.assertLess(touch, apply_at)

    def test_pending_marker_restarts_with_nothing_changed_then_clears(self):
        with tempfile.TemporaryDirectory() as td:
            calls, _, out = self._run(td, probe=({}, {}, {MARKER: True}))
        sudos = [t for k, t in calls if k == "sudo"]
        self.assertTrue(any(RESTART in t for t in sudos))
        self.assertIn("systemctl daemon-reload", sudos)      # a reload may be owed too
        self.assertTrue(any(t.startswith("rm -f ") and MARKER in t for t in sudos))
        self.assertFalse(any(t.startswith("touch") and MARKER in t for t in sudos))
        self.assertIn("pending restart", out)

    def test_failed_restart_keeps_its_marker_but_logs_the_release(self):
        moved = ApplyResult(changed_links=[layout.current_link("app")])
        with tempfile.TemporaryDirectory() as td:
            calls, append, _ = self._run(td, apply_result=moved, fail_restart=True)
        sudos = [t for k, t in calls if k == "sudo"]
        self.assertFalse(any(t.startswith("rm -f") and MARKER in t for t in sudos))
        append.assert_called_once()


class TestPendingClosures(unittest.TestCase):
    """`pending=True` acts like "everything this step watches changed"."""

    def _sudos(self, run):
        sudo = mock.MagicMock()
        with mock.patch.object(ssh, "sudo", sudo), mock.patch.object(ssh, "script"):
            status = run(None, ApplyResult(), pending=True)
        return status, " ".join(c.args[1] for c in sudo.call_args_list)

    def test_service(self):
        run = exes._service_restart("app", "app.service", "/u", "/l", "/t", "/c", None, None)
        status, sudo = self._sudos(run)
        self.assertEqual(status, "pending restart")
        self.assertIn(RESTART, sudo)
        self.assertNotIn("daemon-reload", sudo)     # the executor reloads once

    def test_job(self):
        status, sudo = self._sudos(exes._job_restart("job.timer", "/u", "/l", "/t", "/tl"))
        self.assertEqual(status, "pending restart")
        self.assertIn("systemctl enable --now job.timer", sudo)

    def test_litestream(self):
        from anci.conf import Replication
        run = litestream._activate("litestream.service", "/u", "/l",
                                   [type("D", (), {"name": "main-db",
                                                   "replication": Replication(bucket="b", path="p")})()])
        status, sudo = self._sudos(run)
        self.assertEqual(status, "restarted")
        self.assertIn("systemctl start litestream.service", sudo)

    def test_caddy(self):
        status, sudo = self._sudos(caddy._activate("/u", "/l"))
        self.assertEqual(status, "installed")
        self.assertIn("systemctl restart caddy.service", sudo)

    def test_new_static_files_restart_the_service(self):
        run = exes._service_restart("app", "app.service", "/u", "/l", "/t", "/c", None, "/s")
        sudo = mock.MagicMock()
        with mock.patch.object(ssh, "sudo", sudo):
            self.assertEqual(run(None, ApplyResult(changed_links=["/s"])), "new static files")
        self.assertIn(RESTART, sudo.call_args.args[1])

    def test_unpending_unchanged_does_nothing(self):
        run = exes._service_restart("app", "app.service", "/u", "/l", "/t", "/c", None, None)
        sudo = mock.MagicMock()
        with mock.patch.object(ssh, "sudo", sudo):
            self.assertEqual(run(None, ApplyResult()), "unchanged")
        sudo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
