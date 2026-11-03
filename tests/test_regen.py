"""`regen`: confirmation, the restore script, and apps coming back either way."""

import contextlib
import io
import subprocess
import unittest
from unittest import mock

from anci import errors, ssh
from anci.commands import dbs
from anci.conf import Conf, Db, Env, Exe, Replication, User


def _conf(replication=True) -> Conf:
    return Conf(
        users={"jack": User(name="jack", ssh_pub_keys=["k"])},
        envs={"dev": Env(name="dev", ip="127.0.0.1", access_users=["jack"],
                         anci_version="0.0.1")},
        exes={"app": Exe(name="app", source="./s")},
        dbs={"main": Db(name="main", migrations="./m",
                        replication=Replication(bucket="b", endpoint="https://s3.example.com", access_key_env="S3_KEY", secret_key_env="S3_SECRET", path="p") if replication else None)},
    )


class TestRegen(unittest.TestCase):
    def _run(self, *, force=True, tty=True, answer="y", sudo=None):
        calls = []   # sudo scripts, plus "lock"/"unlock" markers in order

        @contextlib.contextmanager
        def _lock(conn, user, what):
            calls.append(f"lock {what}")
            try:
                yield
            finally:
                calls.append("unlock")

        def _record(c, s, **k):
            calls.append(s)
            return ssh.Result("live=10\n" + '{"files": []}', "") if "-dry-run" in s else ssh.Result("", "")

        with mock.patch.object(dbs.sys, "stdin") as stdin, \
             mock.patch("builtins.input", return_value=answer), \
             mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
             mock.patch.object(dbs.ssh, "connect", return_value=ssh.Conn(ip="x")), \
             mock.patch.object(dbs.ssh, "sudo", side_effect=sudo or _record), \
             mock.patch.object(dbs, "observed_state"), \
             mock.patch.object(dbs.exe_mod, "stop_steps", return_value=[],
                               side_effect=lambda *a: calls.append("stop")) as stop, \
             mock.patch.object(dbs.exe_mod, "start_steps", return_value=[],
                               side_effect=lambda *a: calls.append("start")) as start, \
             mock.patch.object(dbs.boxlock, "hold", new=_lock), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            stdin.isatty.return_value = tty
            dbs.db_regen(_conf(), "main", "dev", force=force)
        self.order = calls
        scripts = [c for c in calls if c not in ("stop", "start", "unlock") and not c.startswith("lock ")]
        return scripts, stop, start, out.getvalue()

    def test_no_terminal_without_force_refuses(self):
        with self.assertRaises(errors.EnvError) as cm:
            self._run(force=False, tty=False)
        self.assertIn("--force", cm.exception.try_)

    def test_prompt_names_the_replica_and_no_aborts(self):
        calls, stop, _, out = self._run(force=False, answer="n")
        self.assertEqual(len(calls), 1)   # the read-only plan, nothing else
        self.assertIn("-dry-run", calls[0])
        stop.assert_not_called()
        self.assertIn("aborted", out)

    def test_yes_restores(self):
        calls, stop, start, _ = self._run(force=False, answer="y")
        self.assertEqual(len(calls), 2)   # plan, then the restore
        self.assertIn("-dry-run", calls[0])
        self.assertNotIn("-dry-run", calls[1])
        stop.assert_called_once()
        start.assert_called_once()

    def test_lock_is_taken_after_the_prompt_and_spans_stop_to_start(self):
        self._run(force=False, answer="y")
        order = [c if c in ("stop", "start", "unlock") or c.startswith("lock ")
                 else ("plan" if "-dry-run" in c else "restore") for c in self.order]
        self.assertEqual(order, ["plan", "lock regen main", "stop", "restore", "start", "unlock"])

    def test_no_lock_when_aborted(self):
        self._run(force=False, answer="n")
        self.assertFalse(any(c.startswith("lock ") for c in self.order))

    def test_restore_script(self):
        script = dbs._restore_script("main")
        self.assertIn("set -a; [ -f /etc/anci/secrets.env ] && . /etc/anci/secrets.env; set +a", script)
        self.assertIn("systemctl stop litestream.service", script)
        self.assertIn("trap 'systemctl start litestream.service", script)
        f = "/var/lib/anci/db/main/data.sql"
        # restore beside the live db, drop the old WAL/SHM, then swap in
        self.assertLess(script.index(f"-o {f}.restore"), script.index(f"rm -f {f}-wal {f}-shm"))
        self.assertLess(script.index(f"rm -f {f}-wal {f}-shm"), script.index(f"mv -f {f}.restore {f}"))
        self.assertEqual(subprocess.run(["bash", "-n"], input=script, text=True).returncode, 0)

    def _failing(self, fails_on_plan):
        def boom(conn, script, **k):
            if ("-dry-run" in script) == fails_on_plan:
                raise ssh.SSHError("restore failed", stderr="no credentials")
            return ssh.Result("live=1\n{\"files\": []}", "")
        with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
             mock.patch.object(dbs.ssh, "connect", return_value=ssh.Conn(ip="x")), \
             mock.patch.object(dbs.ssh, "sudo", side_effect=boom), \
             mock.patch.object(dbs, "observed_state"), \
             mock.patch.object(dbs.exe_mod, "stop_steps", return_value=[]) as stop, \
             mock.patch.object(dbs.exe_mod, "start_steps", return_value=[]) as start, \
             mock.patch.object(dbs.boxlock, "hold",
                               new=lambda *a, **kw: contextlib.nullcontext()), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(errors.EnvError) as cm:
                dbs.db_regen(_conf(), "main", "dev", force=True)
        return cm.exception, stop, start

    def test_failed_plan_is_explained_before_anything_is_stopped(self):
        err, stop, start = self._failing(fails_on_plan=True)
        self.assertIn("unchanged", err.why)
        stop.assert_not_called()
        start.assert_not_called()

    def test_failed_restore_is_explained_and_apps_come_back(self):
        err, stop, start = self._failing(fails_on_plan=False)
        self.assertIn("unchanged", err.why)
        stop.assert_called_once()
        start.assert_called_once()

    def test_unreplicated_db_is_refused(self):
        conf = _conf(replication=False)
        with self.assertRaises(errors.ConfError):
            dbs.db_regen(conf, "main", "dev", force=True)


if __name__ == "__main__":
    unittest.main()


class TestRegenAll(unittest.TestCase):
    """`regen` with no db: every db the env replicates, under one stop/start."""

    def _conf(self):
        conf = _conf()
        conf.dbs["aux"] = Db(name="aux", migrations="./m", replication=Replication(bucket="b", endpoint="https://s3.example.com", access_key_env="S3_KEY", secret_key_env="S3_SECRET"))
        conf.dbs["cache"] = Db(name="cache", migrations="./m")          # not replicated
        return conf

    def _run(self, conf, *, fail_on=None):
        calls = self.calls = []

        def _sudo(c, s, **k):
            if "-dry-run" in s:
                calls.append("plan")
                return ssh.Result("live=10\n" + '{"files": []}', "")
            db = next(n for n in ("aux", "main", "cache") if f"/db/{n}/" in s)
            if db == fail_on:
                raise ssh.SSHError("restore failed", stderr="litestream: no snapshots")
            calls.append(f"restore {db}")
            return ssh.Result("", "")

        with mock.patch.object(dbs.auth, "authorize", return_value="jack"), \
             mock.patch.object(dbs.ssh, "connect", return_value=ssh.Conn(ip="x")), \
             mock.patch.object(dbs.ssh, "sudo", side_effect=_sudo), \
             mock.patch.object(dbs, "observed_state"), \
             mock.patch.object(dbs.exe_mod, "stop_steps", return_value=[], side_effect=lambda *a: calls.append("stop")), \
             mock.patch.object(dbs.exe_mod, "start_steps", return_value=[], side_effect=lambda *a: calls.append("start")), \
             mock.patch.object(dbs.boxlock, "hold", side_effect=lambda *a: contextlib.nullcontext()), \
             contextlib.redirect_stdout(io.StringIO()):
            dbs.db_regen(conf, None, "dev", force=True)
        return calls

    def test_every_replicated_db_in_name_order(self):
        calls = self._run(self._conf())
        self.assertEqual(calls, ["plan", "plan", "stop", "restore aux", "restore main", "start"])

    def test_a_failed_restore_names_what_was_restored_and_apps_come_back(self):
        with self.assertRaises(errors.EnvError) as cm:
            self._run(self._conf(), fail_on="main")
        self.assertEqual(self.calls, ["plan", "plan", "stop", "restore aux", "start"])
        self.assertIn("couldn't restore main", str(cm.exception))
        self.assertIn("already restored in this run: aux", cm.exception.why)

    def test_no_replicated_db_is_refused(self):
        with self.assertRaises(errors.ConfError) as cm:
            dbs.db_regen(_conf(replication=False), None, "dev", force=True)
        self.assertIn("no db is replicated", str(cm.exception))
